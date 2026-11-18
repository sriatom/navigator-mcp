"""Cisco Catalyst SD-WAN (Viptela) reads through SD-WAN Manager (vManage), moved from NAVI's nv-collect.

Inventory fields: CONN_TYPE VMANAGE_API; VMANAGE = Manager address (default DEVICE_ADDR);
VMANAGE_PORT (default PORT, then 443); SYSTEM_IP = the device's system-ip (per-device commands).
Session: POST /j_security_check (form), then GET /dataservice/client/token for the XSRF token,
GET only, logout at the end. Command names are the VIPTELA section of COMMANDS.yml; each maps to one
read-only /dataservice path below. A command without a mapping here is refused.
"""
from __future__ import annotations

from typing import Callable

import httpx

from rudder_devices.adapter_base import _http  # noqa: F401  (shared HTTP error mapping)
from rudder_devices.adapters import DeviceError, _result
from rudder_devices.config import Settings

PATHS = {
    "show devices":                    "/dataservice/device",
    "show controllers":                "/dataservice/system/device/controllers",
    "show wan edge list":              "/dataservice/system/device/vedges",
    "show system status":              "/dataservice/device/system/status?deviceId={SYSTEM_IP}",
    "show version":                    "/dataservice/device/system/status?deviceId={SYSTEM_IP}",
    "show control local-properties":   "/dataservice/device/control/localproperties?deviceId={SYSTEM_IP}",
    "show control connections":        "/dataservice/device/control/connections?deviceId={SYSTEM_IP}",
    "show control connection-history": "/dataservice/device/control/connectionshistory?deviceId={SYSTEM_IP}",
    "show orchestrator connections":   "/dataservice/device/orchestrator/connections?deviceId={SYSTEM_IP}",
    "show omp peers":                  "/dataservice/device/omp/peers?deviceId={SYSTEM_IP}",
    "show omp routes":                 "/dataservice/device/omp/routes/received?deviceId={SYSTEM_IP}",
    "show omp tlocs":                  "/dataservice/device/omp/tlocs/received?deviceId={SYSTEM_IP}",
    "show bfd sessions":               "/dataservice/device/bfd/sessions?deviceId={SYSTEM_IP}",
    "show interface":                  "/dataservice/device/interface?deviceId={SYSTEM_IP}",
    "show ip route":                   "/dataservice/device/ip/routetable?deviceId={SYSTEM_IP}",
    "show arp":                        "/dataservice/device/arp?deviceId={SYSTEM_IP}",
    "show ntp associations":           "/dataservice/device/ntp/associations?deviceId={SYSTEM_IP}",
    "show running-config":             "/dataservice/device/config?deviceId={SYSTEM_IP}",
    "show certificates":               "/dataservice/certificate/record",
    "show alarms":                     "/dataservice/alarms",
}




class Vmanage:
    name = "native:vmanage"
    reads, writes = True, False

    def __init__(self, s: Settings, client: Callable[..., httpx.Client] | None = None):
        self.s = s
        self._client = client or (lambda **kw: httpx.Client(**kw))

    def run(self, dev: dict, cred: dict, commands: list[str]) -> list[dict]:
        host = dev.get("VMANAGE") or dev["DEVICE_ADDR"]
        port = dev.get("VMANAGE_PORT") or dev.get("PORT") or 443
        base = f"https://{host}:{port}"
        with self._client(verify=self.s.api_verify_tls, timeout=self.s.timeout, follow_redirects=False) as http:
            r = _http(lambda: http.post(base + "/j_security_check",
                                        data={"j_username": cred.get("USERNAME", ""),
                                              "j_password": cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""}),
                      "SD-WAN Manager")
            # a wrong login answers 200 with the login page again (HTML), not 401
            if r.status_code in (401, 403) or "<html" in r.text[:2048].lower():
                raise DeviceError("auth_failed", "SD-WAN Manager refused the credential")
            if r.status_code >= 400:
                raise DeviceError("error", f"SD-WAN Manager login HTTP {r.status_code}")
            tok = _http(lambda: http.get(base + "/dataservice/client/token"), "SD-WAN Manager")
            headers = {"X-XSRF-TOKEN": tok.text.strip()} if tok.status_code == 200 and tok.text.strip() else {}
            out = []
            try:
                for cmd in commands:
                    path = PATHS.get(" ".join(cmd.lower().split()))
                    if path is None:
                        out.append(_result(cmd, False, "text", None, "no SD-WAN Manager path for this command"))
                        continue
                    if "{SYSTEM_IP}" in path:
                        if not dev.get("SYSTEM_IP"):
                            out.append(_result(cmd, False, "text", None, "the record has no SYSTEM_IP"))
                            continue
                        path = path.replace("{SYSTEM_IP}", str(dev["SYSTEM_IP"]))
                    rep = _http(lambda: http.get(base + path, headers=headers), "SD-WAN Manager")
                    if rep.status_code >= 400:
                        out.append(_result(cmd, False, "text", None, f"SD-WAN Manager HTTP {rep.status_code}"))
                        continue
                    try:
                        data = rep.json()
                    except ValueError:
                        out.append(_result(cmd, True, "text", rep.text))        # running-config is plain text
                        continue
                    out.append(_result(cmd, True, "json", data.get("data", data) if isinstance(data, dict) else data))
            finally:
                try:
                    http.get(base + "/logout?nocache=1", headers=headers)
                except httpx.HTTPError:
                    pass
        return out
