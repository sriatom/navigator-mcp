"""Palo Alto PAN-OS firewall changes: CLI "set" / "delete" lines into the candidate config over SSH,
then diff, commit and rollback through the XML API (decision #41: API commits are the safe path).

    dry run   refuse if the candidate already holds uncommitted changes (someone else's work would be
              committed with ours); push the lines into the candidate; diff candidate against running
              (both read over the API, canonical XML, secrets shown only as a short hash); then load
              running-config.xml back into the candidate, so nothing is left behind.
    apply     save the running config as rudder-<ticket>.xml on the firewall (the rollback point),
              push the lines, commit, wait for the commit job.
    confirm   delete the saved rollback file (best effort).
    rollback  load rudder-<ticket>.xml and commit it.

PAN-OS has no commit timer, so RUDDER verifies straight after the commit and rolls back itself.
The API key comes from the credential: API_KEY / TOKEN, or a keygen call with USERNAME / PASSWORD
(sent in the POST body, never in a URL). API_PORT on the record overrides 443.
"""
from __future__ import annotations

import difflib
from contextlib import closing
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable
from xml.sax.saxutils import escape

import httpx

from rudder_devices.adapter_base import _http
from rudder_devices.adapters import DeviceError, NetmikoText, _netmiko_connect
from rudder_devices.config import Settings

# Only set / delete lines. These are refused even so: they can cut management access, change who may
# log in, wipe a whole branch, or are not configuration at all.
DENY = [r"^(?!\s*(set|delete)\s)",
        r"^\s*(set|delete)\s+mgt-config\b", r"^\s*(set|delete)\s+deviceconfig\s+system\b",
        r"^\s*(set|delete)\s+deviceconfig\s+setting\s+management\b",
        r"^\s*(set|delete)\s+deviceconfig\s+high-availability\b",
        r"^\s*(set|delete)\s+network\s+profiles\s+interface-management-profile\b",
        r"^\s*(set|delete)\s+(shared\s+)?(admin-role|authentication-profile|authentication-sequence)\b",
        r"^\s*delete\s+\S+\s*$", r"^\s*delete\s+(rulebase|network|shared|vsys|zone)(\s+\S+)?\s*$",
        r"^\s*delete\s+network\s+interface(\s+\S+)?\s*$"]
REFUSED = re.compile(r"Invalid syntax|Unknown command|Server error|Validation Error|is not a valid|is invalid|"
                     r"^\s*Error:|Unrecognized command", re.I | re.M)
NOT_THERE = re.compile(r"(does not|doesn't) exist|No such node|Object doesn't exist", re.I)
VOLATILE_ATTRS = {"admin", "dirtyId", "time", "loc"}
SECRET_TAGS = {"phash", "password", "key", "secret", "pre-shared-key", "auth-key", "private-key",
               "passphrase", "authpwd", "privpwd", "community"}


def _canon(el: ET.Element, depth: int = 0, out: list[str] | None = None) -> list[str]:
    """Indented, attribute-sorted text of an XML tree; volatile attributes dropped, secrets hashed."""
    out = [] if out is None else out
    attrs = "".join(f' {k}="{v}"' for k, v in sorted(el.attrib.items()) if k not in VOLATILE_ATTRS)
    text = (el.text or "").strip()
    pad = "  " * depth
    if el.tag in SECRET_TAGS and text:
        text = "hidden:" + hashlib.sha256(text.encode()).hexdigest()[:10]
    kids = list(el)
    if not kids:
        out.append(f"{pad}<{el.tag}{attrs}>{escape(text)}</{el.tag}>" if text else f"{pad}<{el.tag}{attrs}/>")
        return out
    out.append(f"{pad}<{el.tag}{attrs}>" + (escape(text) if text else ""))
    for k in kids:
        _canon(k, depth + 1, out)
    out.append(f"{pad}</{el.tag}>")
    return out


class PanosWriter:
    name = "native:panos-api"
    reads, writes = True, True
    timer_rollback = False

    def __init__(self, s: Settings, client: Callable[..., httpx.Client] | None = None, connect=None,
                 sleep: Callable[[float], None] = time.sleep):
        self.s = s
        self._client = client or (lambda **kw: httpx.Client(**kw))
        self._connect = connect or _netmiko_connect
        self._sleep = sleep

    # ------------------------------------------------------------------ XML API
    def _open(self, dev, cred):
        http = self._client(verify=self.s.api_verify_tls, timeout=max(self.s.timeout, 60))
        api_port = dev.get('API_PORT') or (dev.get('PORT') if str(dev.get('CONN_TYPE') or '').upper() == 'XML_API' else None)
        url = f"https://{dev['DEVICE_ADDR']}:{api_port or 443}/api/"
        key = cred.get("API_KEY") or cred.get("TOKEN")
        if not key:
            el = self._call(http, url, {"type": "keygen", "user": cred.get("USERNAME", ""),
                                        "password": cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD") or ""},
                            auth=True)
            key = (el.findtext("./result/key") or "").strip()
            if not key:
                http.close()
                raise DeviceError("auth_failed", "PAN-OS API returned no key")
        return http, url, key

    def _call(self, http, url, data: dict, auth=False) -> ET.Element:
        r = _http(lambda: http.post(url, data=data), "PAN-OS XML API")
        if r.status_code in (401, 403):
            raise DeviceError("auth_failed", "PAN-OS API refused the credential")
        try:
            el = ET.fromstring(r.text)
        except ET.ParseError:
            raise DeviceError("error", f"PAN-OS API reply is not XML (HTTP {r.status_code})") from None
        if el.get("status") != "success":
            msg = " ".join(t.strip() for t in el.itertext() if t.strip())[:300]
            if auth or "invalid credential" in msg.lower():
                raise DeviceError("auth_failed", "PAN-OS API refused the credential")
            raise DeviceError("rejected", f"PAN-OS API: {msg or 'error'}")
        return el

    def _op(self, http, url, key, cmd: str) -> ET.Element:
        return self._call(http, url, {"type": "op", "cmd": cmd, "key": key})

    def _config_text(self, http, url, key, which: str) -> str:
        """which = running | candidate, through "show config running|candidate": the same view for both.
        (type=config action=get returns the candidate merged with platform defaults, which differ.)"""
        el = self._op(http, url, key, f"<show><config><{which}></{which}></config></show>")
        cfg = el.find("./result/config")
        if cfg is None:
            cfg = el.find(".//config")
        if cfg is None:
            raise DeviceError("error", f"PAN-OS API gave no config for show config {which}")
        return "\n".join(_canon(cfg)) + "\n"

    def _pending(self, http, url, key) -> bool:
        el = self._op(http, url, key, "<check><pending-changes></pending-changes></check>")
        return (el.findtext("./result") or "").strip().lower() == "yes"

    def _revert(self, http, url, key) -> None:
        self._op(http, url, key, "<load><config><from>running-config.xml</from></config></load>")

    def _commit(self, http, url, key, description: str) -> None:
        el = self._call(http, url, {"type": "commit", "key": key,
                                    "cmd": f"<commit><description>{escape(description)}</description></commit>"})
        job = (el.findtext("./result/job") or "").strip()
        if not job:
            msg = " ".join(t.strip() for t in el.itertext() if t.strip())
            if "no changes" in msg.lower():
                return
            raise DeviceError("error", f"commit did not start a job: {msg[:200]}")
        deadline = time.monotonic() + self.s.commit_wait
        while True:
            j = self._op(http, url, key, f"<show><jobs><id>{job}</id></jobs></show>").find("./result/job")
            if j is not None and (j.findtext("status") or "").upper() == "FIN":
                if (j.findtext("result") or "").upper() != "OK":
                    det = j.find("details")
                    detail = " ".join(t.strip() for t in det.itertext() if t.strip()) if det is not None else ""
                    raise DeviceError("rejected", f"commit job {job} failed: {detail[:300] or 'no details'}")
                return
            if time.monotonic() > deadline:
                raise DeviceError("timeout", f"commit job {job} still running after {self.s.commit_wait}s")
            self._sleep(2)

    # ------------------------------------------------------------------ SSH (candidate edits)
    def _push(self, dev, cred, lines) -> None:
        conn = self._connect(dev, cred, "paloalto_panos", self.s)
        try:
            bad = []
            conn.config_mode()
            for ln in lines:
                out = conn.send_command_timing(ln, read_timeout=max(30, self.s.timeout))
                if REFUSED.search(out or "") or (NOT_THERE.search(out or "") and not ln.strip().startswith("delete")):
                    bad.append(f"{ln.strip()}: {' '.join((out or '').split())[:160]}")
            conn.exit_config_mode()
        finally:
            conn.disconnect()
        if bad:
            raise DeviceError("rejected", "the firewall refused: " + " | ".join(bad[:3]))

    # ------------------------------------------------------------------ writer interface
    def run(self, dev, cred, commands):
        return NetmikoText(self.s, "paloalto_panos", connect=self._connect).run(dev, cred, commands)

    def checkpoint_name(self, session: str) -> str:
        return f"{session}.xml"

    def manual_rollback(self, session: str) -> str:
        return f"load config from {session}.xml, then commit"

    def running_config(self, dev, cred) -> str:
        http, url, key = self._open(dev, cred)
        with closing(http):
            return self._config_text(http, url, key, "running")

    def dry_run(self, dev, cred, lines, session) -> str:
        http, url, key = self._open(dev, cred)
        with closing(http):
            if self._pending(http, url, key):
                raise DeviceError("busy", "the firewall already has uncommitted changes; commit or revert "
                                          "them on the firewall first (they would be committed with ours)")
            running = self._config_text(http, url, key, "running")
            try:
                self._push(dev, cred, lines)
                cand = self._config_text(http, url, key, "candidate")
            finally:
                self._revert(http, url, key)
        diff = difflib.unified_diff(running.splitlines(), cand.splitlines(), lineterm="", n=2)
        body = [ln for ln in diff if not ln.startswith(("--- ", "+++ "))]
        return "\n".join(body) + ("\n" if body else "")

    def apply(self, dev, cred, lines, session, timer=None) -> None:
        http, url, key = self._open(dev, cred)
        with closing(http):
            if self._pending(http, url, key):
                raise DeviceError("busy", "the firewall has uncommitted changes; nothing applied")
            self._op(http, url, key, f"<save><config><to>{escape(session)}.xml</to></config></save>")
            try:
                self._push(dev, cred, lines)
                self._commit(http, url, key, f"RUDDER {session}")
            except DeviceError as e:
                try:
                    self._revert(http, url, key)          # the commit did not happen: clean the candidate
                except DeviceError:
                    pass
                raise DeviceError(e.kind, f"{e}; candidate reverted, nothing committed") from None

    def confirm(self, dev, cred, session) -> None:
        try:
            http, url, key = self._open(dev, cred)
            with closing(http):
                self._op(http, url, key, f"<delete><config><saved>{escape(session)}.xml</saved></config></delete>")
        except DeviceError:
            pass

    def abort(self, dev, cred, session) -> None:
        http, url, key = self._open(dev, cred)
        with closing(http):
            self._op(http, url, key, f"<load><config><from>{escape(session)}.xml</from></config></load>")
            self._commit(http, url, key, f"RUDDER rollback {session}")
        self.confirm(dev, cred, session)

    def save(self, dev, cred) -> None:
        """A PAN-OS commit is already persistent."""
