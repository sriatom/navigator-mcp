"""ServiceNow for RUDDER changes: link only, or gate (decision #159, tasks 18 + 19).

The ServiceNow ticket is the client's request (the requirement): `ticket_id` (CHG..., or INC/RITM in link
mode) carries its number. `/pushok` stays the peer review of the exact config, always.

SERVICENOW.MODE (rudder.local.yml, set from the shell with navigator-setup; default off):
  off   nothing is read or written.
  link  RUDDER reads the ticket by ticket_id and writes back: the diff and its hash (work note + attachment)
        and the validation result when the change is proposed; the apply result and the close code when it
        ends. It gates nothing: an unreachable ServiceNow is noted on our ticket and the change goes on.
  gate  the push also needs the change request (table change_request) in Implement and inside its planned
        window, PLUS /pushok. The diff and hash are attached to the change request before the approval
        request goes out, so its approval covers this exact change. Its planned start becomes the
        scheduled time when it is still ahead; a time outside the window is refused. A change request that
        is cancelled or rejected cancels our pending change and releases its locks (rudder-scheduler
        watches it). Emergency changes (type emergency): /pushok now, no Implement / window check; the
        change request must be approved afterwards (a work note says so).

Settings:
    SERVICENOW:
      MODE: off | link | gate
      URL: https://<instance>.service-now.com
      CREDENTIAL: SNOW_API        # an ATLAS credential tag: LOGIN (USERNAME + LOGIN_PASSWORD) or API_TOKEN (API_KEY)
      VERIFY_TLS: true
      CLOSE_STATE: ""             # optional: the state to set when our change ends (e.g. 0 = Review); empty = leave it
Credentials go through the ATLAS link like a device's: never in this file, never returned, never logged.
Times from the Table API are read with sysparm_display_value=false, i.e. UTC.
"""
from __future__ import annotations

import datetime as dt
import re
import time

import httpx

MODES = ("off", "link", "gate")
TICKET_RE = re.compile(r"^(CHG|INC|RITM|PRB|CTASK|REQ)\d{4,12}$", re.I)
TABLES = {"CHG": "change_request", "INC": "incident", "RITM": "sc_req_item", "PRB": "problem",
          "CTASK": "change_task", "REQ": "sc_request"}
# change_request states (ServiceNow's defaults): -5 New, -4 Assess, -3 Authorize, -2 Scheduled, -1 Implement,
# 0 Review, 3 Closed, 4 Canceled. The display names are accepted too.
IMPLEMENT = {"-1", "implement"}
CANCELLED = {"4", "canceled", "cancelled"}
CLOSED = {"3", "closed"}
FIELDS = ("sys_id,number,short_description,state,type,approval,start_date,end_date,assignment_group,"
          "close_code")
TRANSPORT: httpx.BaseTransport | None = None        # tests: an httpx.MockTransport (the mocked Table API)


class ServiceNowError(Exception):
    pass


def settings(s) -> dict:
    raw = {str(k).upper(): v for k, v in (getattr(s, "servicenow", None) or {}).items()}
    mode = str(raw.get("MODE") or "off").strip().lower()
    if mode in ("no", "none", "false", ""):
        mode = "off"
    if mode not in MODES:
        raise ServiceNowError("SERVICENOW.MODE: off, link or gate")
    url = str(raw.get("URL") or "").strip().rstrip("/")
    if mode != "off":
        if not re.match(r"^https://[A-Za-z0-9.-]+(:\d{2,5})?$", url):
            raise ServiceNowError("SERVICENOW.URL: https://<instance>.service-now.com (https, no path)")
        if not re.match(r"^[A-Z0-9][A-Z0-9_-]{0,60}$", str(raw.get("CREDENTIAL") or "")):
            raise ServiceNowError("SERVICENOW.CREDENTIAL: the ATLAS credential tag of the ServiceNow account")
    return {"MODE": mode, "URL": url, "CREDENTIAL": str(raw.get("CREDENTIAL") or ""),
            "VERIFY_TLS": str(raw.get("VERIFY_TLS", True)).lower() not in ("0", "false", "no", "off"),
            "CLOSE_STATE": str(raw.get("CLOSE_STATE") or "").strip()}


def enabled(s) -> bool:
    try:
        return settings(s)["MODE"] != "off"
    except ServiceNowError:
        return True                 # a broken setting must not silently turn a gate off: calls will fail


def _utc(text) -> float | None:
    v = str(text or "").strip()
    if not v:
        return None
    try:
        return dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _val(field) -> str:
    """A Table API field: a plain value, or {"value": ..., "display_value": ...}."""
    if isinstance(field, dict):
        return str(field.get("value") or field.get("display_value") or "")
    return str(field or "")


class Client:
    """The few Table API / Attachment API calls RUDDER needs."""

    def __init__(self, url: str, cred: dict, verify: bool = True, transport=None):
        headers = {"Accept": "application/json"}
        auth = None
        if cred.get("API_KEY"):
            headers["x-sn-apikey"] = str(cred["API_KEY"])
        elif cred.get("USERNAME") and (cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD")):
            auth = (str(cred["USERNAME"]), str(cred.get("LOGIN_PASSWORD") or cred.get("PASSWORD")))
        else:
            raise ServiceNowError("the ServiceNow credential needs USERNAME + LOGIN_PASSWORD, or API_KEY")
        self.http = httpx.Client(base_url=url + "/api/now/", timeout=30, verify=verify, auth=auth,
                                 headers=headers, transport=transport if transport is not None else TRANSPORT)

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ServiceNowError(f"ServiceNow not reachable ({type(e).__name__})") from None
        if r.status_code in (401, 403):
            raise ServiceNowError(f"ServiceNow refused the login (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ServiceNowError(f"ServiceNow {method} {path.split('?')[0]}: HTTP {r.status_code}")
        return r.json() if r.content else {}

    def ticket(self, number: str) -> dict:
        n = str(number or "").strip().upper()
        m = TICKET_RE.match(n)
        if not m:
            raise ServiceNowError(f"ticket_id {number!r} is not a ServiceNow number (CHG..., INC..., RITM...)")
        table = TABLES[m.group(1)]
        res = self._call("GET", f"table/{table}", params={"sysparm_query": f"number={n}", "sysparm_limit": 2,
                                                          "sysparm_fields": FIELDS,
                                                          "sysparm_display_value": "false"}).get("result") or []
        if len(res) != 1:
            raise ServiceNowError(f"{n} not found in ServiceNow" if not res else f"{n}: more than one match")
        r = res[0]
        return {"number": n, "table": table, "sys_id": _val(r.get("sys_id")),
                "short_description": _val(r.get("short_description"))[:200],
                "state": _val(r.get("state")), "type": _val(r.get("type")).lower(),
                "approval": _val(r.get("approval")).lower(),
                "start": _utc(_val(r.get("start_date"))), "end": _utc(_val(r.get("end_date")))}

    def ping(self) -> None:
        """One read of change_request (setup's test): the URL answers and the login may read it."""
        self._call("GET", "table/change_request", params={"sysparm_limit": 1, "sysparm_fields": "sys_id"})

    def update(self, tk: dict, fields: dict) -> None:
        self._call("PATCH", f"table/{tk['table']}/{tk['sys_id']}", json=fields)

    def attach(self, tk: dict, name: str, text: str) -> None:
        self._call("POST", "attachment/file", params={"table_name": tk["table"], "table_sys_id": tk["sys_id"],
                                                      "file_name": name},
                   content=text.encode(), headers={"Content-Type": "text/plain"})


class ServiceNow:
    """What changes.py and the scheduler call. Built per use; holds no secrets after a call."""

    def __init__(self, s, atlas, transport=None):
        self.s, self.atlas, self.transport = s, atlas, transport
        self.cfg = settings(s)

    @property
    def mode(self) -> str:
        return self.cfg["MODE"]

    def _client(self) -> Client:
        try:
            cred = self.atlas.credential(self.cfg["CREDENTIAL"])
        except Exception as e:  # noqa: BLE001   AtlasError, PermissionError...: no detail, no secret
            raise ServiceNowError(f"the ServiceNow credential {self.cfg['CREDENTIAL']} is not available "
                                  f"({type(e).__name__})") from None
        try:
            return Client(self.cfg["URL"], dict(cred), self.cfg["VERIFY_TLS"], self.transport)
        finally:
            if hasattr(cred, "clear"):
                cred.clear()

    def read(self, number: str) -> dict:
        return self._client().ticket(number)

    # ------------------------------------------------------------------ gate rules
    def is_emergency(self, tk: dict) -> bool:
        return tk.get("type") == "emergency"

    def refuse_at_propose(self, tk: dict, now: float | None = None) -> str | None:
        """Gate: a change request that can never pass is refused before any device is touched."""
        now = time.time() if now is None else now
        if tk["table"] != "change_request":
            return f"{tk['number']} is not a change request (the gate needs a CHG number)"
        if tk["state"].lower() in CANCELLED or tk["approval"] == "rejected":
            return f"{tk['number']} is cancelled or rejected"
        if tk["state"].lower() in CLOSED:
            return f"{tk['number']} is closed"
        if self.is_emergency(tk):
            return None
        if not tk.get("start") or not tk.get("end"):
            return f"{tk['number']} has no planned start and end"
        if tk["end"] <= now:
            return f"the planned window of {tk['number']} has ended ({_fmt(tk['end'])})"
        return None

    def check_apply(self, tk: dict, now: float | None = None) -> str | None:
        """Gate, right before the push: None = go; else why not (nothing is touched)."""
        now = time.time() if now is None else now
        why = self.refuse_at_propose(tk, now)
        if why or self.is_emergency(tk):
            return why
        if tk["state"].lower() not in IMPLEMENT:
            return f"{tk['number']} is not in Implement (state {tk['state']})"
        if not tk["start"] <= now <= tk["end"]:
            return (f"now is outside the planned window of {tk['number']} "
                    f"({_fmt(tk['start'])} - {_fmt(tk['end'])} UTC)")
        return None

    def gone(self, tk: dict) -> str | None:
        """Gate watch: 'cancelled' / 'rejected' when the change request says so, else None."""
        if tk["state"].lower() in CANCELLED:
            return "cancelled"
        if tk["approval"] == "rejected":
            return "rejected"
        return None

    # ------------------------------------------------------------------ write back
    def proposed(self, tk: dict, t: dict) -> None:
        """The diff and hash (and the validation result) on the ticket, before the approval request."""
        cl = self._client()
        diff = "\n".join(f"[{d['device']}] ({d['platform']})\n{d['diff'].rstrip()}" for d in t["devices"])
        head = (f"RUDDER change {t['id']} proposed: {t['description']}\n"
                f"Devices: {', '.join(d['device'] for d in t['devices'])}\n"
                f"Hash (devices + lines + diffs): {t['hash']}\n")
        v = t.get("validation")
        if v:
            head += f"Validation ({v.get('validator')}): {v.get('status')}: {v.get('summary')}\n"
        if self.mode == "gate":
            head += ("Gate: RUDDER pushes only while this change request is in Implement inside its planned "
                     "window, and only after /pushok of exactly this diff.\n")
        cl.attach(tk, f"{t['id']}-diff.txt", head + "\n" + diff + "\n")
        cl.update(tk, {"work_notes": head + ("\n" + diff[:3000] if len(diff) <= 3000 else
                                             "\n(full diff in the attachment " + f"{t['id']}-diff.txt)")})

    def finished(self, tk: dict, t: dict) -> None:
        """The apply result and the close code."""
        cl = self._client()
        ok = t["state"] == "applied"
        res = "\n".join(f"{d['device']}: {d.get('state')}" + (f" ({d['error']})" if d.get("error") else "")
                        for d in t["devices"])
        after = [d["validation"] for d in t["devices"] if d.get("validation")]
        note = (f"RUDDER change {t['id']}: {t['state'].upper()}" + (f" (approved by {t['decided_by']})"
                                                                    if t.get("decided_by") else "") + f"\n{res}")
        if after:
            note += "\nValidation after the push: " + "; ".join(f"{r.get('device')} {r.get('status')}" for r in after)
        if self.mode == "gate" and self.is_emergency(tk):
            note += "\nEmergency change: applied on /pushok; this change request must be approved afterwards."
        fields = {"work_notes": note, "close_code": "successful" if ok else "unsuccessful",
                  "close_notes": f"RUDDER change {t['id']}: {t['state']}"}
        if self.cfg["CLOSE_STATE"]:
            fields["state"] = self.cfg["CLOSE_STATE"]
        cl.update(tk, fields)

    def cancelled(self, tk: dict, t: dict, why: str) -> None:
        self._client().update(tk, {"work_notes": f"RUDDER change {t['id']} cancelled: {why}. Nothing was changed."})


def test(s, atlas=None, transport=None) -> str:
    """navigator-setup: the settings work (the login reads change_request). Raises ServiceNowError."""
    if settings(s)["MODE"] == "off":
        return "SERVICENOW off"
    if atlas is None:
        from .atlas import Atlas
        atlas = Atlas(s)
    sn = ServiceNow(s, atlas, transport)
    sn._client().ping()
    return f"SERVICENOW {sn.mode}: {sn.cfg['URL']} answered, credential {sn.cfg['CREDENTIAL']} may read change_request"


def _fmt(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch)) if epoch else "-"


def at_text(epoch: float) -> str:
    """A planned start as RUDDER's `at` (UTC)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
