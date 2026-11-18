"""Arista pack, RUDDER side: eAPI reads and writes (configuration sessions), SSH reads with | json.

Moved unchanged from rudder_devices/adapters.py (vendor packs, step 2).
"""
from __future__ import annotations

import json
import time
from typing import Callable

import httpx

from rudder_devices.adapter_base import INVALID, DeviceError, _Base, _netmiko_connect, _result, _Ssh
from rudder_devices.config import Settings


# --------------------------------------------------------------------------- Arista eAPI
class EosEapi(_Base):
    name = "native:eapi"
    reads, writes = True, False

    def __init__(self, s: Settings, client: Callable[..., httpx.Client] | None = None):
        self.s = s
        self._client = client or (lambda **kw: httpx.Client(**kw))

    def _post(self, http: httpx.Client, url: str, cmds: list, fmt: str) -> dict:
        body = {"jsonrpc": "2.0", "method": "runCmds", "id": "rudder",
                "params": {"version": 1, "cmds": cmds, "format": fmt, "timestamps": False}}
        shown = [c if isinstance(c, str) else c.get("cmd") for c in cmds]     # never the enable input
        t0 = time.time()
        try:
            r = http.post(url, json=body)
            self._log(f"eAPI POST {url} format={fmt} cmds={shown} -> HTTP {r.status_code} "
                      f"in {int((time.time() - t0) * 1000)} ms")
        except httpx.ConnectTimeout:
            self._log(f"eAPI POST {url} -> connect timeout after {int(time.time() - t0)} s")
            raise DeviceError("timeout", "eAPI connect timed out") from None
        except httpx.ConnectError as e:
            msg = str(e).lower()
            self._log(f"eAPI POST {url} -> connect error: {str(e)[:200]}")
            if "ssl" in msg or "certificate" in msg:
                raise DeviceError("tls_error", "TLS failed (certificate not trusted? EAPI_VERIFY_TLS)") from None
            raise DeviceError("unreachable", "cannot connect to eAPI") from None
        except httpx.TimeoutException:
            self._log(f"eAPI POST {url} -> read timeout after {int(time.time() - t0)} s")
            raise DeviceError("timeout", "eAPI timed out") from None
        if r.status_code == 401:
            raise DeviceError("auth_failed", "eAPI refused the credential (HTTP 401)")
        if r.status_code >= 400:
            raise DeviceError("error", f"eAPI HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise DeviceError("error", "eAPI reply is not JSON (is eAPI enabled?)") from None

    def run(self, dev: dict, cred: dict, commands: list[str]) -> list[dict]:
        url = f"https://{dev['DEVICE_ADDR']}:{dev.get('PORT') or 443}/command-api"
        user = cred.get("USERNAME", "")
        pw = cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""
        enable = cred.get("ENABLE_PASSWORD")
        # Always enter privileged mode first (as pyeapi does): many show commands need it.
        pre = [{"cmd": "enable", "input": enable}] if enable else ["enable"]
        out = []
        with self._client(auth=(user, pw), verify=self.s.eapi_verify_tls, timeout=self.s.timeout) as http:
            del pw, enable
            for cmd in commands:
                if self.raw:
                    rep = self._post(http, url, pre + [cmd], "text")
                    if "error" not in rep:
                        out.append(_result(cmd, True, "text", rep["result"][-1].get("output", "")))
                        continue
                    msg = str(rep["error"].get("message", "error"))
                    out.append(_result(cmd, False, "text", None, f"eAPI error {rep['error'].get('code')}: {msg[:200]}"))
                    continue
                rep = self._post(http, url, pre + [cmd], "json")
                if "error" not in rep:
                    out.append(_result(cmd, True, "json", rep["result"][-1]))
                    continue
                err = rep["error"]
                if err.get("code") == 1003 or "convert" in str(err.get("message", "")).lower():
                    rep = self._post(http, url, pre + [cmd], "text")       # no JSON for this command
                    if "error" not in rep:
                        out.append(_result(cmd, True, "text", rep["result"][-1].get("output", "")))
                        continue
                    err = rep["error"]
                msg = str(err.get("message", "error"))
                if err.get("code") in (1002,) or "invalid" in msg.lower():
                    out.append(_result(cmd, False, "text", None, f"device rejected the command: {msg[:200]}"))
                else:
                    out.append(_result(cmd, False, "text", None, f"eAPI error {err.get('code')}: {msg[:200]}"))
        return out


class EosEapiWriter(EosEapi):
    """Arista changes through a named configuration session (checked live on cEOS 4.35, 2026-11-11):

        dry run   configure session S / lines / end; show session-config named S diffs; abort
        apply     configure session S / lines / commit timer HH:MM:SS   (the switch rolls back by
                  itself if the timer runs out, so a crash mid-change is safe)
        confirm   configure session S commit
        rollback  configure session S abort
    """
    name = "native:eapi"
    reads, writes = True, True

    def _session(self, dev: dict, cred: dict):
        url = f"https://{dev['DEVICE_ADDR']}:{dev.get('PORT') or 443}/command-api"
        enable = cred.get("ENABLE_PASSWORD")
        pre = [{"cmd": "enable", "input": enable}] if enable else ["enable"]
        http = self._client(auth=(cred.get("USERNAME", ""), cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""),
                            verify=self.s.eapi_verify_tls, timeout=self.s.timeout)
        return http, url, pre

    def _do(self, dev, cred, cmds, fmt="json") -> list:
        http, url, pre = self._session(dev, cred)
        with http:
            rep = self._post(http, url, pre + cmds, fmt)
        if "error" in rep:
            err = rep["error"]
            data = err.get("data") or []
            detail = ""
            for d in data:
                if isinstance(d, dict) and d.get("errors"):
                    detail = "; ".join(map(str, d["errors"]))[:300]
            raise DeviceError("rejected", f"{str(err.get('message', 'eAPI error'))[:300]}"
                                          + (f" ({detail})" if detail else ""))
        return rep["result"][len(pre):]

    def dry_run(self, dev, cred, lines: list[str], session: str) -> str:
        try:
            self._do(dev, cred, [f"configure session {session}", *lines, "end"])
            diff = self._do(dev, cred, [f"show session-config named {session} diffs"], "text")[0].get("output", "")
        finally:
            try:
                self._do(dev, cred, [f"configure session {session}", "abort"])
            except DeviceError:
                pass
        # drop the "--- system:/running-config" / "+++ session:/<name>" header: it names the session
        body = [ln for ln in diff.splitlines() if not ln.startswith(("--- ", "+++ "))]
        return "\n".join(body) + ("\n" if body else "")

    def running_config(self, dev, cred) -> str:
        return self._do(dev, cred, ["show running-config"], "text")[0].get("output", "")

    def candidate_config(self, dev, cred, lines: list[str], session: str) -> str:
        """The whole config with the lines in it (the session's view), for the validator; nothing is committed."""
        try:
            self._do(dev, cred, [f"configure session {session}", *lines, "end"])
            return self._do(dev, cred, [f"show session-config named {session}"], "text")[0].get("output", "")
        finally:
            try:
                self._do(dev, cred, [f"configure session {session}", "abort"])
            except DeviceError:
                pass

    def apply(self, dev, cred, lines: list[str], session: str, timer: str) -> None:
        self._do(dev, cred, [f"configure session {session}", *lines, f"commit timer {timer}"])

    def confirm(self, dev, cred, session: str) -> None:
        self._do(dev, cred, [f"configure session {session} commit"])

    def abort(self, dev, cred, session: str) -> None:
        self._do(dev, cred, [f"configure session {session} abort"])

    def save(self, dev, cred) -> None:
        self._do(dev, cred, ["copy running-config startup-config"])


class EosSshJson(_Ssh):
    """Arista over SSH, asking for "| json" so the output matches eAPI (decision #36)."""
    name = "native:ssh-json"
    reads, writes = True, False
    device_type = "arista_eos"

    def __init__(self, s: Settings, connect=None):
        self.s = s
        self._connect = connect or _netmiko_connect

    def run(self, dev, cred, commands):
        conn = self._open(dev, cred)
        out = []
        try:
            for cmd in commands:
                if not self.raw:
                    raw = self._send(conn, f"{cmd} | json")
                    try:
                        out.append(_result(cmd, True, "json", json.loads(raw)))
                        continue
                    except ValueError:
                        pass
                raw = self._send(conn, cmd)      # raw text, or no JSON for this one
                bad = INVALID.search(raw or "")
                out.append(_result(cmd, not bad, "text", raw, "device rejected the command" if bad else None))
        finally:
            conn.disconnect()
            self._flush()
        return out
