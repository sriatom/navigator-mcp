"""RUDDER adapters: how one platform is reached, and which adapter a device gets.

The shared plumbing is in adapter_base.py; vendor-specific adapters live in their vendor pack
(navigator_packs/<vendor>/adapters.py) and are re-exported here, so callers keep using
rudder_devices.adapters.<Name>.
"""
from __future__ import annotations

from navigator_packs.arista.adapters import EosEapi, EosEapiWriter, EosSshJson  # noqa: F401  (Arista pack)
# Cisco pack: navigator_packs.cisco.cli (CiscoCliWriter) and .vmanage (Vmanage), imported where used

from .adapter_base import (INVALID, DeviceError, _Base, _netmiko_connect, _result,  # noqa: F401
                           _Ssh)
from .config import Settings


class NetmikoText(_Ssh):
    """Any platform netmiko knows: allowlisted commands, raw text (no parsing)."""
    name = "native:netmiko-text"
    reads, writes = True, False

    def __init__(self, s: Settings, device_type: str, connect=None):
        self.s, self.device_type = s, device_type
        self._connect = connect or _netmiko_connect

    def run(self, dev, cred, commands):
        conn = self._open(dev, cred)
        out = []
        try:
            for cmd in commands:
                raw = self._send(conn, cmd)
                bad = INVALID.search(raw or "")
                out.append(_result(cmd, not bad, "text", raw, "device rejected the command" if bad else None))
        finally:
            conn.disconnect()
            self._flush()
        return out


def choose_writer(platform: dict, dev: dict, s: Settings, factories: dict | None = None):
    """The write adapter for a device, or (None, reason)."""
    f = factories or {}
    writes = str(platform.get("writes") or "none")
    conn = str(dev.get("CONN_TYPE") or "").upper()
    if writes == "native:eapi":
        if conn == "SSH":
            return None, "Arista changes over SSH are not built yet; use a CONN_TYPE EAPI record"
        return f.get("eapi-writer", lambda: EosEapiWriter(s))(), None
    if writes == "native:netmiko":
        if platform.get("ID") == "IOSXR":
            return None, "IOS-XR changes (commit confirmed) are not built yet"
        if conn not in ("SSH", ""):
            return None, f"{platform.get('ID')} changes go over SSH; this record is CONN_TYPE {conn}"
        from navigator_packs.cisco.cli import CiscoCliWriter
        return f.get("cisco-writer", lambda: CiscoCliWriter(s, platform["netmiko"]))(), None
    if writes == "native:xml-api":
        from navigator_packs.paloalto.writer import PanosWriter
        return f.get("panos-writer", lambda: PanosWriter(s))(), None
    if writes == "none":
        return None, f"{platform.get('ID')} has no write path"
    if writes.startswith("delegated:"):
        return None, (f"{platform.get('ID')} changes go through the {writes.split(':', 1)[1]} MCP server; "
                      f"delegated writes through tickets are not built yet")
    return None, f"no write adapter for {platform.get('ID')} ({writes})"


# Connection methods a caller (or an inventory record's ADAPTER field) may ask for instead of the platform default.
VIAS = {
    "eapi": "Arista eAPI (JSON over HTTPS)",
    "ssh": "SSH: Arista with | json, other platforms as text (netmiko)",
    "netmiko": "SSH through netmiko, plain text (no JSON), any platform netmiko knows",
    "pyats": "the pyATS MCP server (Cisco and others with a pyATS os name; Genie-parsed where possible)",
    "multivendor": "the multivendor-cli MCP server (netmiko, text)",
    "fortinet": "the Fortinet MCP server (FortiGate REST)",
    "vmanage": "SD-WAN Manager REST API (read-only)",
    "xml-api": "PAN-OS XML API (op commands, JSON replies)",
}
_SSH_VIAS = ("ssh", "netmiko", "pyats", "multivendor")


def via_name(via) -> str | None:
    """Normalise a connection-method name; None for 'default' / empty. Unknown names raise ValueError."""
    v = str(via or "").strip().lower()
    if v in ("", "default", "none", "auto"):
        return None
    if v not in VIAS:
        raise ValueError(f"unknown connection method {via!r}; one of: {', '.join(VIAS)} (or default)")
    return v


def effective_dev(dev: dict, via: str | None) -> dict:
    """The record as the chosen method needs it: an SSH method on an eAPI record uses port 22 (or SSH_PORT),
    eAPI on an SSH record uses 443 (or EAPI_PORT). The inventory itself is not changed."""
    if not via:
        return dev
    conn = str(dev.get("CONN_TYPE") or "").upper()
    if via in _SSH_VIAS and conn not in ("SSH", ""):
        return {**dev, "PORT": dev.get("SSH_PORT") or 22, "CONN_TYPE": "SSH"}
    if via == "eapi" and conn != "EAPI":
        return {**dev, "PORT": dev.get("EAPI_PORT") or 443, "CONN_TYPE": "EAPI"}
    if via == "xml-api" and conn != "XML_API":
        return {**dev, "PORT": dev.get("API_PORT") or 443, "CONN_TYPE": "XML_API"}
    return dev


def _by_via(via: str, platform: dict, s: Settings, f: dict, factories):
    pid = platform.get("ID")
    reads = str(platform.get("reads") or "none")
    if via == "eapi":
        if reads != "native:eapi":
            return None, f"{pid} has no eAPI (that is Arista only)"
        return f.get("eapi", lambda: EosEapi(s))(), None
    if via == "ssh" and reads == "native:eapi":
        return f.get("ssh-json", lambda: EosSshJson(s))(), None
    if via in ("ssh", "netmiko"):
        if not platform.get("netmiko"):
            return None, f"{pid} has no netmiko name in the catalogue, so RUDDER cannot reach it over SSH"
        return f.get("netmiko-text", lambda: NetmikoText(s, platform["netmiko"]))(), None
    if via in ("pyats", "multivendor", "fortinet"):
        from .delegated import choose_delegated
        return choose_delegated(via, platform, s, factories)
    if via == "xml-api":
        if "XML_API" not in (platform.get("conn") or []):
            return None, f"{pid} has no XML API (that is PAN-OS)"
        from navigator_packs.paloalto.xmlapi import PanosXmlApi
        return f.get("panos-xml-api", lambda: PanosXmlApi(s))(), None
    if via == "vmanage":
        if reads != "native:vmanage":
            return None, f"{pid} is not read through SD-WAN Manager"
        from navigator_packs.cisco.vmanage import Vmanage
        return f.get("vmanage", lambda: Vmanage(s))(), None
    return None, f"unknown connection method {via}"


def choose(platform: dict, dev: dict, s: Settings, factories: dict | None = None, via: str | None = None):
    """The read adapter for a device, or (None, reason) when it is not built yet.

    via: a connection method for this call only (VIAS); without it the record's ADAPTER field, then the
    platform's default from the catalogue."""
    f = factories or {}
    try:
        v = via_name(via) or via_name(dev.get("ADAPTER"))
    except ValueError as e:
        return None, str(e)
    if v:
        return _by_via(v, platform, s, f, factories)
    reads = str(platform.get("reads") or "none")
    conn = str(dev.get("CONN_TYPE") or "").upper()
    if reads == "native:eapi":
        if conn == "SSH":
            return f.get("ssh-json", lambda: EosSshJson(s))(), None
        return f.get("eapi", lambda: EosEapi(s))(), None
    if conn == "XML_API" and "XML_API" in (platform.get("conn") or []):      # PAN-OS over the XML API (Palo Alto pack)
        from navigator_packs.paloalto.xmlapi import PanosXmlApi
        return f.get("panos-xml-api", lambda: PanosXmlApi(s))(), None
    if reads in ("native:ssh", "native:generic-netmiko") and platform.get("netmiko"):
        return f.get("netmiko-text", lambda: NetmikoText(s, platform["netmiko"]))(), None
    if reads.startswith("delegated:"):
        from .delegated import choose_delegated
        return choose_delegated(reads.split(":", 1)[1], platform, s, factories)
    if reads == "native:vmanage":
        if conn == "SSH" and platform.get("netmiko"):         # the Manager's own CLI over SSH (Cisco pack)
            return f.get("netmiko-text", lambda: NetmikoText(s, platform["netmiko"]))(), None
        from navigator_packs.cisco.vmanage import Vmanage
        return f.get("vmanage", lambda: Vmanage(s))(), None
    return None, f"no read adapter for {platform.get('ID')} ({reads})"
