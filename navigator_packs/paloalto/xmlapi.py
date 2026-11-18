"""Palo Alto pack, RUDDER side: PAN-OS reads over the XML API (type=op), replies as JSON.

A CLI command from the allowed list is turned into PAN-OS's XML form: every keyword becomes a nested element
and a value (an interface name, an address, a quoted string) becomes the text of the last element, e.g.
    show system info              -> <show><system><info></info></system></show>
    show interface ethernet1/1    -> <show><interface>ethernet1/1</interface></show>
Commands whose XML differs are listed in pack.yml (xml_api_commands). The API key comes from the credential
(API_KEY / TOKEN, or keygen with the username and password in the POST body), as for changes.
"""
from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from contextlib import closing
from xml.sax.saxutils import escape

from rudder_devices.adapter_base import DeviceError, _result

from .writer import PanosWriter

_KEYWORD = re.compile(r"^[a-z][a-z0-9-]*$")


def _overrides() -> dict:
    try:
        import navigator_packs
        return {" ".join(k.split()): v for k, v in (navigator_packs.pack("paloalto").get("xml_api_commands") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def cli_to_xml(cmd: str, overrides: dict | None = None) -> str:
    norm = " ".join(cmd.split())
    o = (overrides if overrides is not None else _overrides()).get(norm)
    if o:
        return o
    try:
        toks = shlex.split(norm)
    except ValueError:
        toks = norm.split()
    names, value = [], None
    for t in toks:
        if value is None and _KEYWORD.match(t):
            names.append(t)
        else:
            value = t if value is None else f"{value} {t}"
    if not names:
        raise ValueError(f"cannot turn {cmd!r} into a PAN-OS XML command")
    return "".join(f"<{n}>" for n in names) + (escape(value) if value is not None else "") + \
        "".join(f"</{n}>" for n in reversed(names))


def to_dict(el: ET.Element):
    """An XML element as plain JSON: attributes and children as keys, repeated children as lists, text leaves."""
    kids = list(el)
    d = dict(el.attrib)
    if not kids:
        text = (el.text or "").strip()
        if not d:
            return text
        if text:
            d["_text"] = text
        return d
    for k in kids:
        v = to_dict(k)
        if k.tag in d:
            if not isinstance(d[k.tag], list):
                d[k.tag] = [d[k.tag]]
            d[k.tag].append(v)
        else:
            d[k.tag] = v
    return d


class PanosXmlApi(PanosWriter):
    """Read-only: op commands only (the writer's config paths are never called from here)."""
    name = "native:panos-xml-api"
    reads, writes = True, False
    raw = False
    debug: list | None = None

    def _log(self, line: str) -> None:
        if self.debug is not None:
            self.debug.append(line)

    def run(self, dev, cred, commands):
        http, url, key = self._open(dev, cred)
        self._log(f"PAN-OS XML API {url} (key from {'the credential' if cred.get('API_KEY') or cred.get('TOKEN') else 'keygen'})")
        out = []
        with closing(http):
            for cmd in commands:
                try:
                    xml = cli_to_xml(cmd)
                except ValueError as e:
                    out.append(_result(cmd, False, "text", None, str(e)))
                    continue
                try:
                    el = self._op(http, url, key, xml)
                except DeviceError as e:
                    if e.kind in ("auth_failed", "unreachable", "timeout", "tls_error"):
                        raise
                    out.append(_result(cmd, False, "text", None, f"device rejected the command: {str(e)[:200]}"))
                    continue
                self._log(f"op {xml} -> {el.get('status')}")
                res = el.find("./result")
                if res is None:
                    out.append(_result(cmd, True, "text", ""))
                elif self.raw:
                    out.append(_result(cmd, True, "text", ET.tostring(res, encoding="unicode")))
                else:
                    data = to_dict(res)
                    out.append(_result(cmd, True, "json" if isinstance(data, dict) else "text", data))
        return out
