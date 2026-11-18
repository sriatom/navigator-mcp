"""Shared plumbing for every adapter, in RUDDER and in the vendor packs.

Every adapter returns one result per command:
    {"command", "ok", "format": "json" | "text", "output", "error"}
and raises DeviceError(kind, message) when it cannot connect at all. kind is one of
auth_failed, unreachable, timeout, tls_error, host_key, error. Messages never contain a secret.
"""
from __future__ import annotations

import inspect
import io
import json
import re
import socket
import time
from typing import Callable

import httpx

from .config import Settings

INVALID = re.compile(r"^\s*% ?(Invalid|Incomplete|Ambiguous)|^\s*Invalid (input|syntax)|^\s*Unknown command|"
                     r"^\s*Error:", re.I | re.M)


class DeviceError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _result(cmd, ok, fmt, output, error=None) -> dict:
    return {"command": cmd, "ok": ok, "format": fmt, "output": output, "error": error}


class _Base:
    """Options every read adapter understands (set by the caller per call):
    raw   = True: the device's own text, no JSON conversion;
    debug = a list: the adapter appends its connection log lines (the caller scrubs them)."""
    raw: bool = False
    debug: list | None = None

    def _log(self, line: str) -> None:
        if self.debug is not None:
            self.debug.append(line)


# --------------------------------------------------------------------------- netmiko (SSH)
def _netmiko_connect(dev: dict, cred: dict, device_type: str, s: Settings, log: io.BytesIO | None = None):
    """log: a buffer for netmiko's session log (debug). netmiko masks the password and enable secret in it;
    the caller scrubs it again before it leaves RUDDER."""
    from netmiko import ConnectHandler
    from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
    from paramiko.ssh_exception import SSHException
    params = {"device_type": device_type, "host": dev["DEVICE_ADDR"], "port": int(dev.get("PORT") or 22),
              "username": cred.get("USERNAME", ""),
              "password": cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or "",
              "secret": cred.get("ENABLE_PASSWORD") or "", "timeout": s.timeout, "conn_timeout": min(s.timeout, 15),
              "ssh_strict": s.ssh_strict, "system_host_keys": s.ssh_strict,
              "fast_cli": False}
    if s.known_hosts:
        params["alt_host_keys"], params["alt_key_file"] = True, str(s.known_hosts)
    if log is not None:
        params["session_log"] = log
    try:
        conn = ConnectHandler(**params)
    except NetmikoAuthenticationException:
        raise DeviceError("auth_failed", "SSH login refused") from None
    except NetmikoTimeoutException:
        raise DeviceError("timeout", "SSH connection timed out") from None
    except SSHException as e:
        if "host key" in str(e).lower() or "not found in known_hosts" in str(e).lower():
            raise DeviceError("host_key", "unknown or changed SSH host key") from None
        raise DeviceError("error", f"SSH error: {type(e).__name__}") from None
    except (socket.error, OSError):
        raise DeviceError("unreachable", "cannot connect over SSH") from None
    finally:
        params.clear()
    if cred.get("ENABLE_PASSWORD"):
        try:
            conn.enable()
        except Exception:  # noqa: BLE001
            conn.disconnect()
            raise DeviceError("auth_failed", "enable password refused") from None
    return conn


class _Ssh(_Base):
    """Shared SSH plumbing: connect (with the session log when debugging), time each command."""
    device_type = ""

    def _open(self, dev, cred):
        self._buf = io.BytesIO() if self.debug is not None else None
        self._log(f"SSH connect {dev.get('DEVICE_ADDR')}:{dev.get('PORT') or 22} as netmiko {self.device_type}")
        t0 = time.time()
        if self._buf is not None and "log" not in inspect.signature(self._connect).parameters:
            self._log("(this connector keeps no session log)")
            self._buf = None
        try:
            conn = (self._connect(dev, cred, self.device_type, self.s, log=self._buf) if self._buf is not None
                    else self._connect(dev, cred, self.device_type, self.s))
        except DeviceError as e:
            self._log(f"SSH connect failed after {round(time.time() - t0, 1)} s: {e.kind}: {e}")
            self._flush()
            raise
        self._log(f"SSH connected in {round(time.time() - t0, 1)} s")
        return conn

    def _send(self, conn, cmd):
        t0 = time.time()
        out = conn.send_command(cmd, read_timeout=self.s.timeout)
        self._log(f"sent {cmd!r}: {len(out or '')} characters in {round(time.time() - t0, 1)} s")
        return out

    def _flush(self):
        buf = getattr(self, "_buf", None)
        if buf is not None and self.debug is not None:
            text = buf.getvalue().decode("utf-8", "replace")
            if text:
                self.debug.append("--- netmiko session log (last 8000 characters) ---\n" + text[-8000:])
            self._buf = None


def _http(call, what: str):
    try:
        return call()
    except httpx.ConnectTimeout:
        raise DeviceError("timeout", f"{what}: connect timed out") from None
    except httpx.ConnectError as e:
        msg = str(e).lower()
        if "ssl" in msg or "certificate" in msg:
            raise DeviceError("tls_error", f"{what}: TLS failed (certificate not trusted? API_VERIFY_TLS)") from None
        raise DeviceError("unreachable", f"cannot connect to {what}") from None
    except httpx.TimeoutException:
        raise DeviceError("timeout", f"{what} timed out") from None
