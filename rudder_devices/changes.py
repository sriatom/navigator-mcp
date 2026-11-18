"""Changes: propose -> human /pushok out of band -> apply -> verify -> confirm, or roll back.

propose   checks every line against the deny list, refuses PRODUCTION devices, locks each device,
          saves the running config as baseline, makes a dry-run diff per device on the device itself,
          binds devices + lines + diffs into one hash, and sends the approval request (code only to
          the human's channel).
apply     needs the approval decision for this ticket, re-checks that each device's dry-run diff is
          unchanged (nothing drifted since the approval), then per device:
          commit with the switch's rollback timer -> verify -> confirm, or abort (= roll back).
          Stops at the first device that fails; devices not yet touched stay untouched.
cancel    releases the locks and closes the approval.
Tickets expire (TTL_MIN, default 30); an expired ticket cannot be applied.
validate  with VALIDATION on (validation.py, decision #158): propose checks the baselines against the
          candidate configs before any approval is asked (a fail blocks the change); apply checks the live
          config again after each commit, inside the rollback timer (a fail rolls the device back).
schedule  a ticket proposed with `at` (or moved with reschedule) is approved for that time and is
          applied only by the scheduler (schedule.py), never by apply() from a tool.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

try:
    import fcntl
except ImportError:                      # not on Windows; RUDDER runs on Linux hosts
    fcntl = None

from . import adapters, approval, servicenow, validation
from . import schedule as sched_mod
from .atlas import Atlas, AtlasError
from .config import Settings, inv_key
from .core import RudderError, resolve_commands
from .records import Run

# Lines that are never pushed through a change ticket (a destructive-command filter).
DENY = [r"^\s*reload\b", r"\berase\b", r"^\s*delete\b", r"^\s*format\b", r"^\s*boot\b",
        r"^\s*(no\s+)?username\b", r"^\s*(no\s+)?aaa\b", r"^\s*(no\s+)?enable\s+(password|secret)\b",
        r"^\s*(no\s+)?management\s+api\b", r"^\s*no\s+ip\s+routing\b", r"^\s*(write|copy)\b",
        r"^\s*configure\b", r"^\s*(end|exit)\s*$", r"^\s*no\s+interface\s+management",
        r"^\s*(no\s+)?(ip\s+)?ssh\b", r"^\s*(no\s+)?management\s+ssh\b", r"^\s*bash\b", r"^\s*run\b",
        r"^\s*do\b", r"^\s*(no\s+)?checkpoint\b", r"^\s*rollback\b", r"^\s*(no\s+)?archive\b",
        r"^\s*(no\s+)?line\s+vty\b", r"^\s*(no\s+)?feature\s+(ssh|nxapi)\b"]
OPEN_STATES = ("waiting_approval", "approved")
REFUSED_MIN = 60             # a line a device refused is remembered this long (left_out check)
INTERRUPTED_SEC = 1200       # a ticket 'applying' with no update this long was cut off by a crash / restart
REFUSED_LINE_RE = re.compile(r"'([^'\n]{1,200})' failed")


class GateWait(RudderError):
    """ServiceNow gate: not yet (the change request is not in Implement, or its window has not started).
    The ticket stays approved; the scheduler tries again on its next pass."""


def _norm(line: str) -> str:
    return " ".join(str(line).split()).lower()
SHORT_DIFF = 1200            # with a web page, a longer diff is only summarised in the approval message
TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")    # an outside ticket: CHG0012345, INC123...


def _now() -> float:
    return time.time()


def web_base(appr: dict) -> str | None:
    """Base URL of the change pages: APPROVAL.WEB_URL, else https:// (local certificate, web.scheme) or
    http://<APPROVAL.WEB_BIND>; None = pages off."""
    from . import web
    url = str(appr.get("WEB_URL") or "").strip().rstrip("/")
    bind = str(appr.get("WEB_BIND") or "").strip()
    sch = web.scheme(appr) if bind else None
    if not bind or not sch:
        return None
    return url or f"{sch}://{bind}"


def _person(v) -> dict:
    """requested_by as the host gives it: "ID|Name", "ID" or "Name" -> {"id", "name"} (cleaned)."""
    s = re.sub(r"[^\w .@|-]", "", str(v or ""))[:120].strip()
    if not s:
        return {}
    pid, _, name = s.partition("|")
    pid, name = pid.strip(), name.strip()
    if not name and not re.match(r"^[A-Z0-9]{6,}$", pid):
        pid, name = "", pid                              # a plain name, not an ID
    return {k: v for k, v in (("id", pid), ("name", name)) if v}


def _name(who: dict | None) -> str:
    who = who or {}
    if who.get("name") and who.get("id"):
        return f"{who['name']} ({who['id']})"
    return who.get("name") or who.get("id") or "unknown"


def _day(text: str | None, end: bool = False) -> float | None:
    """'2026-11-14' (local day; end=True -> the end of that day) or '2026-11-14 18:00' -> epoch."""
    if not text:
        return None
    for fmt_ in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            tm = time.strptime(str(text).strip(), fmt_)
        except ValueError:
            continue
        e = time.mktime(tm)
        return e + 86399 if end and fmt_ == "%Y-%m-%d" else e
    raise RudderError(f"cannot read the date {text!r}: use 2026-11-14 or 2026-11-14 18:00")


def _dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def _write(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.replace(tmp, p)


class Changes:
    def __init__(self, s: Settings, atlas: Atlas | None = None, factories: dict | None = None,
                 channel=None):
        self.s = s
        self.atlas = atlas or Atlas(s)
        self.factories = factories or {}
        self.channel = channel or approval.channel(s.approval, s.consumer)
        self.ttl = int((s.approval or {}).get("TTL_MIN", 30)) * 60
        self.tdir = _dir(s.state_path / "tickets")
        self.ldir = _dir(s.state_path / "locks")
        self.sched = sched_mod.settings(s)
        self._held = 0
        self.sn_transport = None              # tests: the mocked ServiceNow Table API

    # ------------------------------------------------------------------ ServiceNow (servicenow.py)
    def _servicenow(self):
        """None when SERVICENOW.MODE is off. A broken setting raises ServiceNowError (never silently off)."""
        if not servicenow.enabled(self.s):
            return None
        return servicenow.ServiceNow(self.s, self.atlas, self.sn_transport)

    def _sn_proposed(self, t: dict) -> None:
        """Read the ticket and write the diff, hash and validation result back. Link: never blocks.
        Gate: this must work BEFORE the approval request goes out (raises RudderError, locks released)."""
        gate = (t.get("servicenow") or {}).get("mode") == "gate"
        try:
            sn = self._servicenow()
            if not sn or not t.get("ticket_id"):
                return
            tk = sn.read(t["ticket_id"])
            t["servicenow"] = {**(t.get("servicenow") or {}), "mode": sn.mode, "number": tk["number"],
                               "state": tk["state"], "short_description": tk["short_description"]}
            sn.proposed(tk, t)
            t["servicenow"]["written"] = ["diff", "hash"] + (["validation"] if t.get("validation") else [])
        except servicenow.ServiceNowError as e:
            if gate:
                self._unlock(t)
                raise RudderError(f"ServiceNow gate: the diff and hash could not be attached to "
                                  f"{t.get('ticket_id')} ({e}); no approval was asked, nothing was changed") from None
            t["servicenow"] = {**(t.get("servicenow") or {"mode": "link", "number": t.get("ticket_id")}),
                               "error": str(e)}

    def _sn_gate_open(self, ticket_id: str | None, at: str | None, timezone: str | None):
        """Gate, before any device is touched: the change request must exist and be able to pass.
        Returns (info for the ticket or None, at, timezone); its planned start becomes `at` when ahead."""
        try:
            sn = self._servicenow()
        except servicenow.ServiceNowError as e:
            raise RudderError(f"ServiceNow settings: {e}") from None
        if not sn or sn.mode != "gate":
            return None, at, timezone
        if not ticket_id:
            raise RudderError("ServiceNow gate is on: give ticket_id = the change request (CHG...) this change is for")
        try:
            tk = sn.read(ticket_id)
        except servicenow.ServiceNowError as e:
            raise RudderError(f"ServiceNow gate: {e}") from None
        why = sn.refuse_at_propose(tk)
        if why:
            raise RudderError(f"ServiceNow gate: {why}; nothing was changed")
        info = {"mode": "gate", "number": tk["number"], "emergency": sn.is_emergency(tk),
                "window": [tk["start"], tk["end"]]}
        if not info["emergency"] and not at and tk["start"] and tk["start"] > _now() + 60:
            at, timezone = servicenow.at_text(tk["start"]), "UTC"
            info["scheduled_from_window"] = True
        return info, at, timezone

    def _sn_gate_check(self, t: dict) -> None:
        """Gate, right before the push. GateWait = not yet (Implement / window); RudderError = never."""
        info = t.get("servicenow") or {}
        if info.get("mode") != "gate":
            return
        try:
            sn = self._servicenow()
            if not sn or sn.mode != "gate":
                raise servicenow.ServiceNowError("SERVICENOW.MODE is no longer gate; propose the change again")
            tk = sn.read(info["number"])
        except servicenow.ServiceNowError as e:
            raise GateWait(f"ServiceNow gate: {e}; nothing was changed") from None
        why = sn.check_apply(tk)
        if why:
            ended = sn.refuse_at_propose(tk)
            raise (RudderError if ended else GateWait)(f"ServiceNow gate: {why}; nothing was changed")

    def servicenow_watch(self, min_gap: float = 60.0) -> list[tuple[str, str]]:
        """rudder-scheduler: a gate ticket whose change request was cancelled or rejected is cancelled here
        (locks released, approval closed). At most one ServiceNow read per ticket per min_gap seconds."""
        out = []
        last = self.__dict__.setdefault("_sn_last", {})
        for p in sorted(self.tdir.glob("C*.json")):
            try:
                t = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            info = t.get("servicenow") or {}
            if info.get("mode") != "gate" or t.get("state") not in OPEN_STATES or t.get("consumer") != self.s.consumer:
                continue
            if _now() - last.get(t["id"], 0) < min_gap:
                continue
            last[t["id"]] = _now()
            try:
                sn = self._servicenow()
                tk = sn.read(info["number"]) if sn else None
            except servicenow.ServiceNowError:
                continue                                   # unreachable: the apply-time check still holds
            gone = sn.gone(tk) if tk else None
            if not gone:
                continue
            why = f"ServiceNow {info['number']} was {gone}"
            try:
                self.cancel(t["id"], note=why)
            except RudderError:
                continue
            try:
                sn.cancelled(tk, t, why)
            except servicenow.ServiceNowError:
                pass
            out.append((t["id"], why))
        return out

    def _sn_finished(self, t: dict) -> None:
        """Link and gate: the apply result and the close code on the ticket. Never changes our result."""
        info = t.get("servicenow")
        if not info or not info.get("number"):
            return
        try:
            sn = self._servicenow()
            if not sn:
                return
            sn.finished(sn.read(info["number"]), t)
            info["closed"] = {"close_code": "successful" if t["state"] == "applied" else "unsuccessful"}
        except servicenow.ServiceNowError as e:
            info["writeback_error"] = str(e)
        self._save(t)

    @contextlib.contextmanager
    def locked(self):
        """Ticket read-modify-write is serialised between the MCP server and the scheduler.
        Re-entrant inside one Changes object; never held while talking to a device."""
        if self._held or fcntl is None:
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
            return
        fd = os.open(self.tdir / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._held = 1
            yield
        finally:
            self._held = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _schedule_for(self, at: str, timezone: str | None, notice_sec: int | None) -> dict:
        notice = int(self.sched["NOTICE_SEC"] if notice_sec is None else notice_sec)
        if not 10 <= notice <= 600:
            raise RudderError("notice_sec must be between 10 and 600")
        epoch, tz = sched_mod.parse_at(at, timezone, self.sched)
        sched_mod.check_window(epoch, notice, self.sched)
        return {"at": epoch, "tz": tz, "notice_sec": notice, "state": "scheduled",
                "at_text": sched_mod.fmt(epoch, tz)}

    def _sched_expires(self, sc: dict) -> float:
        return sc["at"] + float(self.sched["LATE_MIN"]) * 60 + 300

    def page_url(self, t: dict) -> str | None:
        """The ticket's read-only web page (APPROVAL.WEB_BIND / WEB_URL), or None when pages are off."""
        base = web_base(self.s.approval or {})
        return f"{base}/c/{t['web_token']}" if base and t.get("web_token") else None

    def _message(self, t: dict, code: str) -> str:
        diffs = "\n".join(f"[{d['device']}]\n{d['diff'].strip()}" for d in t["devices"])
        url = self.page_url(t)
        n = sum(1 for ln in diffs.splitlines() if re.match(r"^[+~-]", ln))
        if url and len(diffs) > SHORT_DIFF:
            diffs = (f"{n} changed line(s) on {len(t['devices'])} device(s): too long for this message, "
                     f"see the full change on the page below.")
        elif len(diffs) > 2500:
            diffs = diffs[:2500] + "\n... (truncated; full diff in the ticket)"
        if getattr(self.channel, "method", "") == "slack" and diffs.startswith("["):
            diffs = f"```\n{diffs}\n```"                     # Slack: the diff as a code block
        page = f"\n🔗 Full change and its progress: {url}" if url else ""
        sc = t.get("schedule")
        if sc:
            when = (f"⏰ SCHEDULED for {sc['at_text']} ({sc['tz']}).\n"
                    f"Approving now lets RUDDER apply exactly this change at that time. {sc['notice_sec']} s "
                    f"before, you get a notice with a code to stop it; no answer = it runs.\n"
                    f"(The bot's reply may say 'will run now': it runs at the time above.)")
        else:
            when = f"Expires in {self.ttl // 60} min. Approving lets the agent apply exactly this change."
        by = f"{_name(t['requested_by'])} via {self.s.consumer}" if t.get("requested_by") else self.s.consumer
        ref = "".join(f"\n{k}: {t[f]}" for k, f in (("Ticket ID", "ticket_id"), ("Project", "project")) if t.get(f))
        sn = t.get("servicenow") or {}
        if sn.get("number"):
            ref += (f"\nServiceNow {sn['number']}: " + (f"NOT updated ({sn['error']})" if sn.get("error") else
                    "diff and hash attached" + (" (gate: pushed only in Implement, inside its window)"
                                                if sn.get("mode") == "gate" else "")))
            if sn.get("emergency"):
                ref += ("\n🚨 EMERGENCY change: approve it now with the code below; the change request must be "
                        "approved afterwards")
        left = ""
        if t.get("left_out"):
            left = ("\n⚠️ NOT in this change (the device refused these lines in an earlier proposal):\n"
                    + "\n".join(f"  {x['line']}  ({x['why'][:120]})" for x in t["left_out"]))
        return (f"🔐 Change {t['id']} needs approval\nRequested by {by}{ref}\n{t['description']}\n"
                f"Devices: {', '.join(d['device'] for d in t['devices'])}\n"
                f"Diff:\n{diffs}{left}"
                + (f"\n{validation.line(t['validation'])}" if t.get("validation") else "")
                + f"{page}\n\n"
                f"Approve:  /pushok {code}\nReject:   /pushno {code}\n{when}")

    # ------------------------------------------------------------------ storage
    def _path(self, tid: str) -> Path:
        if not re.match(r"^C[0-9]{8}-[0-9a-f]{6}$", tid or ""):
            raise RudderError(f"no ticket {tid!r}")
        return self.tdir / f"{tid}.json"

    def _load(self, tid: str) -> dict:
        p = self._path(tid)
        if not p.is_file():
            raise RudderError(f"no ticket {tid}")
        return json.loads(p.read_text())

    def _save(self, t: dict) -> None:
        _write(self._path(t["id"]), json.dumps(t, indent=2))

    def _audit(self, t: dict, what: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{self.s.consumer}\t{t['id']}\t{what}\n"
        fd = os.open(_dir(self.s.state_path) / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ locks
    def _lock_path(self, dev: str) -> Path:
        return self.ldir / (re.sub(r"[^A-Za-z0-9._-]", "_", dev.upper()) + ".lock")

    def _lock(self, dev: str, tid: str) -> None:
        p = self._lock_path(dev)
        for _ in range(2):
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, tid.encode())
                os.close(fd)
                return
            except FileExistsError:
                holder = p.read_text().strip()
                try:
                    other = self._load(holder)
                    alive = other["state"] in OPEN_STATES + ("applying",) and other["expires"] > _now()
                except RudderError:
                    alive = False
                if alive:
                    raise RudderError(f"{dev} is locked by change {holder} ({other['state']}); "
                                      f"apply or cancel that one first") from None
                p.unlink(missing_ok=True)                 # stale lock
        raise RudderError(f"could not lock {dev}")

    def _unlock(self, t: dict) -> None:
        for d in t["devices"]:
            p = self._lock_path(d["device"])
            if p.is_file() and p.read_text().strip() == t["id"]:
                p.unlink(missing_ok=True)

    # ------------------------------------------------------------------ checks
    def _check_lines(self, lines: list[str], deny: list[str] | None = None) -> list[str]:
        clean = [ln.rstrip() for ln in lines if ln.strip()]
        if not clean:
            raise RudderError("no configuration lines")
        pats = [re.compile(p, re.I) for p in (DENY if deny is None else deny) + list(self.s.deny_lines)]
        bad = [ln for ln in clean if any(p.search(ln) for p in pats)]
        if bad:
            raise RudderError("refused: these lines are on the deny list (they can cut access, wipe or "
                              "reboot a device, or leave config mode): " + "; ".join(bad))
        return clean

    def _expire(self, t: dict) -> dict:
        if t["state"] in OPEN_STATES and t["expires"] <= _now():
            self.channel.close(t["approval"])
            self._unlock(t)
            t["state"] = "expired"
            self._save(t)
            self._audit(t, "expired")
        return t

    # ------------------------------------------------------------------ a line the device refused = a failed change
    def _alert(self, t: dict, text: str) -> list[str]:
        """To the approval channel and to the chat the change came from (reply_to). Returns where it went."""
        sent = []
        try:
            self.channel.notify(text)
            sent.append("approval channel")
        except approval.ApprovalError:
            pass
        if t.get("reply_to") and hasattr(self.channel, "reply"):
            try:
                self.channel.reply(t["reply_to"], text)
                sent.append("chat thread")
            except approval.ApprovalError:
                pass
        return sent

    @staticmethod
    def _candidate(writer, dev, cred, lines, session) -> str | None:
        """The device's full config with the lines in it, for the validator; None if this writer cannot say."""
        fn = getattr(writer, "candidate_config", None)
        if fn is None:
            return None
        try:
            return fn(dev, cred, lines, session + "-val")
        except adapters.DeviceError:
            return None

    def _validation_failure(self, t: dict, v: dict) -> dict:
        """The validator found a problem before the push: the change FAILED, nothing was changed, no approval
        was opened, the locks are released and both channels are told."""
        for d in t["devices"]:
            d["state"] = "not_applied"
        self._unlock(t)
        t["state"] = "failed"
        t["attention"] = True
        t["failure"] = {"kind": "validation", "summary": v["summary"], "details": v["details"]}
        self._save(t)
        self._audit(t, f"FAILED validation ({v['validator']}) before the push: {v['summary'][:300]}; nothing changed")
        url = self.page_url(t)
        text = (f"❌ Change {t['id']} FAILED validation ({v['validator']}): nothing was changed and no approval "
                f"was opened\n{t['description']}\nDevices: {', '.join(d['device'] for d in t['devices'])}\n"
                + "\n".join(f"  {x}" for x in v["details"][:8]) + (f"\n🔗 {url}" if url else ""))
        t["failure"]["alerted"] = self._alert(t, text)
        self._save(t)
        return self.status(t["id"])

    def _refused_failure(self, t: dict, devices: list[str], dev: str | None, error: str) -> dict:
        """The device rejected a line in the dry run: the change FAILED and needs a person. A ticket is kept
        (state failed, attention), nothing was changed on any device, no approval is opened, and both
        channels are told. The agent must find out why and come back with lines that work (a new ticket)."""
        m = REFUSED_LINE_RE.search(error)
        line = m.group(1).strip() if m else ""
        done = {d["device"] for d in t["devices"]}
        for d in t["devices"]:
            d["state"] = "not_applied"
        t["devices"].append({"device": dev or "?", "state": "refused", "error": error, "diff": ""})
        for name in devices:
            if name.upper() not in {x.upper() for x in done} and name.upper() != str(dev or "").upper():
                t["devices"].append({"device": name, "state": "not_checked", "diff": ""})
        t["state"] = "failed"
        t["attention"] = True
        t["failure"] = {"kind": "refused_by_device", "device": dev, "line": line, "error": error[:500],
                        "investigation": "open"}
        self._save(t)
        self._audit(t, f"FAILED: {dev} refused the line '{line}' in the dry run; nothing changed; "
                       f"needs human attention" + (f" (requested by {_name(t['requested_by'])})" if t.get("requested_by") else ""))
        url = self.page_url(t)
        text = (f"❌ Change {t['id']} FAILED: needs attention\n{t['description']}\n"
                f"{dev} refused: {line}\n({error[:300]})\n"
                f"Nothing was changed on any device and no approval was opened. The agent is looking for a "
                f"command that works; any new lines come as a new change with its own approval."
                + (f"\n🔗 {url}" if url else ""))
        t["failure"]["alerted"] = self._alert(t, text)
        self._save(t)
        return self.status(t["id"])

    def investigation(self, tid: str, finding: str, alternative: list[str] | None = None,
                      new_ticket: str | None = None) -> dict:
        """Record what the investigation of a refused line found (shown on the page, sent to both channels)."""
        with self.locked():
            t = self._load(tid)
            if not (t.get("failure") or {}).get("kind") == "refused_by_device":
                raise RudderError(f"change {tid} is not a refused change")
            f = t["failure"]
            f.update(investigation="done", finding=str(finding)[:1500],
                     alternative=[str(x)[:200] for x in (alternative or [])][:50])
            if new_ticket:
                self._path(new_ticket)                      # a valid id
                f["new_ticket"] = new_ticket
            t["log"].append(f"{time.strftime('%H:%M:%S')} investigation: {str(finding)[:300]}")
            self._save(t)
            self._audit(t, "investigation: " + " ".join(str(finding).split())[:300]
                        + (f"; alternative proposed as {new_ticket}" if new_ticket else ""))
        alt = ("\nInstead: " + " / ".join(f["alternative"])) if f["alternative"] else ""
        self._alert(t, f"🔎 Change {tid} (failed): {f['finding'][:600]}{alt}"
                       + (f"\nNew change for approval: {new_ticket}" if new_ticket else ""))
        return self.status(tid)

    # ------------------------------------------------------------------ refused lines (left_out)
    def _refused_path(self) -> Path:
        return _dir(self.s.state_path) / "refused.json"

    def _refused_load(self) -> list[dict]:
        try:
            rows = json.loads(self._refused_path().read_text())
        except (OSError, ValueError):
            return []
        keep = _now() - REFUSED_MIN * 60
        return [r for r in rows if isinstance(r, dict) and r.get("at", 0) >= keep][-50:]

    def _refused_note(self, devices: list[str], lines: list[str], error: str) -> None:
        """A dry run failed on a line: remember it, so a later proposal that silently drops it is flagged."""
        m = REFUSED_LINE_RE.search(error or "")
        if not m:
            return
        line = m.group(1).strip()
        if _norm(line) not in {_norm(x) for x in lines}:
            return
        rows = self._refused_load() + [{"at": _now(), "consumer": self.s.consumer, "line": line,
                                        "devices": sorted(d.upper() for d in devices), "why": str(error)[:300]}]
        try:
            _write(self._refused_path(), json.dumps(rows[-50:], indent=1))
        except OSError:
            pass                                          # a convenience check; never blocks a proposal

    def _left_out(self, devices: list[str], lines: list[str]) -> list[dict]:
        """Lines a device of this proposal refused recently that are not in this proposal."""
        have, devs, out, seen = {_norm(x) for x in lines}, {d.upper() for d in devices}, [], set()
        for r in reversed(self._refused_load()):
            n = _norm(r.get("line", ""))
            if (r.get("consumer") == self.s.consumer and devs & set(r.get("devices") or [])
                    and n not in have and n not in seen):
                seen.add(n)
                out.append({"line": r["line"], "why": r.get("why", "")})
        return out

    # ------------------------------------------------------------------ propose
    def propose(self, inventory: str, devices: list[str], lines: list[str], description: str,
                verify_commands: list[str] | None = None, expect: dict[str, list[str]] | None = None,
                save: bool = False, at: str | None = None, timezone: str | None = None,
                notice_sec: int | None = None, reply_to: str | None = None, requested_by: str | None = None,
                project: str | None = None, ticket_id: str | None = None) -> dict:
        if not devices:
            raise RudderError("name the devices to change")
        ok = getattr(self.channel, "reply_ok", None)
        reply_to = reply_to if (reply_to and ok and ok(reply_to)) else None   # host-filled; junk is dropped
        who = _person(requested_by)
        if ticket_id and not TICKET_ID_RE.match(str(ticket_id)):
            raise RudderError("ticket_id: letters, digits and . _ - only (up to 40), e.g. CHG0012345")
        gate, at, timezone = self._sn_gate_open(ticket_id, at, timezone)
        sc = self._schedule_for(at, timezone, notice_sec) if at else None
        if gate and not gate["emergency"] and sc:
            start, end = gate["window"]
            if not start <= sc["at"] <= end:
                raise RudderError(f"ServiceNow gate: {sc['at_text']} is outside the planned window of "
                                  f"{gate['number']} ({servicenow._fmt(start)} - {servicenow._fmt(end)} UTC)")
        self._check_lines(lines, [])                      # not empty; the real check needs the platform
        try:
            recs = self.atlas.records(inventory, names=devices)
        except AtlasError as e:
            raise RudderError(str(e)) from None
        found = {r["DEVICE_NAME"].upper(): r for r in recs}
        missing = [d for d in devices if d.upper() not in found]
        if missing:
            raise RudderError(f"not found (or disabled) in {inventory}: {', '.join(missing)}")
        kinds = set()
        for name in devices:
            dev = found[name.upper()]
            try:
                w, why = adapters.choose_writer(self.atlas.platform(dev.get("OS")), dev, self.s, self.factories)
            except AtlasError as e:
                raise RudderError(str(e)) from None
            if w is None:
                raise RudderError(f"{dev['DEVICE_NAME']}: {why}")
            kinds.add(w.name)
        if len(kinds) > 1:
            raise RudderError(f"one change ticket holds one kind of device (the lines are platform syntax); "
                              f"these need {', '.join(sorted(kinds))}: propose them separately")
        if kinds == {"native:panos-api"}:
            from navigator_packs.paloalto.writer import DENY as PANOS_DENY
            lines = self._check_lines(lines, PANOS_DENY)
        else:
            lines = self._check_lines(lines)
        tid = time.strftime("C%Y%m%d-") + secrets.token_hex(3)
        session = f"rudder-{tid.lower()}"
        t = {"id": tid, "consumer": self.s.consumer, "inventory": inventory, "description": description,
             "created": _now(), "expires": _now() + self.ttl, "state": "proposing", "session": session,
             "lines": lines, "verify_commands": verify_commands or [], "expect": expect or {},
             "save": bool(save), "devices": [], "approval": {}, "log": [],
             "web_token": secrets.token_urlsafe(24)}            # the unguessable part of the page link
        if reply_to:
            t["reply_to"] = reply_to
        if who:
            t["requested_by"] = who
        if project:
            t["project"] = str(project)[:100]
        if ticket_id:
            t["ticket_id"] = str(ticket_id)
        if sc:
            t["schedule"] = sc
            t["expires"] = self._sched_expires(sc)
        if gate:
            t["servicenow"] = gate
            if not sc and not gate["emergency"] and gate["window"][1]:
                t["expires"] = gate["window"][1]        # an approved gate change waits for Implement in its window
        locked: list[str] = []
        cur = None
        val_on = validation.enabled(self.s)
        before_cfg: dict[str, str] = {}
        after_cfg: dict[str, str | None] = {}
        plat_ids: dict[str, str] = {}
        try:
            for name in devices:
                dev = found[name.upper()]
                cur = dev["DEVICE_NAME"]
                if dev.get("PRODUCTION") and not self.s.allow_production:
                    raise RudderError(f"{dev['DEVICE_NAME']} is PRODUCTION; changes to production devices are "
                                      f"refused (ALLOW_PRODUCTION is off)")
                plat = self.atlas.platform(dev.get("OS"))
                writer, why = adapters.choose_writer(plat, dev, self.s, self.factories)
                if writer is None:
                    raise RudderError(f"{dev['DEVICE_NAME']}: {why}")
                if not dev.get("CREDENTIAL"):
                    raise RudderError(f"{dev['DEVICE_NAME']}: no credential tag this consumer may use")
                if verify_commands:
                    resolve_commands(self.atlas.commands(plat["ID"]), None, verify_commands, plat["ID"])
                self._lock(dev["DEVICE_NAME"], tid)
                locked.append(dev["DEVICE_NAME"])
                cred = self.atlas.credential(dev["CREDENTIAL"])
                try:
                    baseline = writer.running_config(dev, cred)
                    diff = writer.dry_run(dev, cred, lines, session + "-dry")
                    if val_on:
                        before_cfg[dev["DEVICE_NAME"]] = baseline
                        plat_ids[dev["DEVICE_NAME"]] = plat["ID"]
                        after_cfg[dev["DEVICE_NAME"]] = self._candidate(writer, dev, cred, lines, session)
                finally:
                    cred.clear()
                if not re.search(r"^[+~-]", diff, re.M):
                    raise RudderError(f"{dev['DEVICE_NAME']}: these lines change nothing (the device already has them)")
                t["devices"].append({"device": dev["DEVICE_NAME"], "platform": plat["ID"], "adapter": writer.name,
                                     "baseline_sha": hashlib.sha256(baseline.encode()).hexdigest(),
                                     "diff": diff, "state": "pending"})
                run = Run(self.s.records_path, {"consumer": self.s.consumer, "ticket": tid, "kind": "baseline"})
                run.save(dev["DEVICE_NAME"], [{"command": "show running-config", "format": "text",
                                               "output": baseline, "ok": True, "error": None}])
                run.finish({dev["DEVICE_NAME"]: {"status": "ok"}})
                t["devices"][-1]["baseline_record"] = str(run.path)
        except (RudderError, AtlasError, adapters.DeviceError) as e:
            for d in locked:
                p = self._lock_path(d)
                if p.is_file() and p.read_text().strip() == tid:
                    p.unlink(missing_ok=True)
            msg = str(e)
            if isinstance(e, adapters.DeviceError) and REFUSED_LINE_RE.search(msg):
                self._refused_note(devices, lines, msg)
                return self._refused_failure(t, devices, cur, msg)
            raise RudderError(msg) from None
        if val_on:
            v = validation.run(self.s, "before", before_cfg, after_cfg, plat_ids, list(before_cfg), tid)
            if v:
                t["validation"] = v
                if v["status"] == "fail":
                    return self._validation_failure(t, v)
        left = self._left_out(devices, lines)
        if left:
            t["left_out"] = left
        t["hash"] = self._hash(t)
        self._sn_proposed(t)                     # ServiceNow link: diff + hash on the ticket first
        platforms = sorted({d["platform"] for d in t["devices"]})
        if not gate and self._ship_bypass(t, who):          # the gate always needs /pushok
            t["state"] = "approved"
            t["approval"] = {}
            t["bypass"] = {"by": who, "why": "SHIP engineer, lab inventory"}
            self._save(t)
            self._audit(t, f"proposed {', '.join(devices)} hash {t['hash'][:12]} by {_name(who)}")
            self._audit(t, f"approved without /pushok: SHIP engineer {_name(who)} (lab inventory {inventory})")
            try:
                self.channel.notify(f"ℹ️ Change {tid} by SHIP engineer {_name(who)} runs WITHOUT approval "
                                    f"(lab inventory {inventory})\n{description}\n"
                                    f"Devices: {', '.join(devices)} ({', '.join(platforms)})\n"
                                    + (validation.line(t.get("validation")) + "\n" if t.get("validation") else "")
                                    + "\n".join(f"[{d['device']}]\n{d['diff'].strip()}" for d in t["devices"])[:2500])
            except approval.ApprovalError:
                pass                                  # the audit log has it; the result goes there too
            return self.status(tid)
        t["state"] = "waiting_approval"
        try:
            t["approval"] = self.channel.request(f"RUDDER {tid}: {', '.join(devices)}",
                                                 lambda code: self._message(t, code),
                                                 **({"requester": who["id"]} if who.get("id") else {}))
        except approval.ApprovalError as e:
            self._unlock(t)
            raise RudderError(str(e)) from None
        self._save(t)
        self._audit(t, f"proposed {', '.join(devices)} hash {t['hash'][:12]}"
                       + (f" by {_name(who)}" if who else "")
                       + (f" scheduled {sc['at_text']}" if sc else ""))
        return self.status(tid)

    def _ship_bypass(self, t: dict, who: dict) -> bool:
        """APPROVAL.SHIP_ENGINEERS: user IDs (filled by the MCP host, never the model) whose changes run
        without /pushok, only in lab inventories (LAB_INVENTORIES), never scheduled, never PRODUCTION
        (refused earlier anyway). Everything is still a ticket: audited, reported, searchable."""
        ids = {str(x).strip() for x in ((self.s.approval or {}).get("SHIP_ENGINEERS") or []) if str(x).strip()}
        return bool(ids and who.get("id") in ids and not t.get("schedule")
                    and inv_key(t["inventory"]) in set(self.s.lab_inventories))

    def reschedule(self, tid: str, at: str, timezone: str | None = None, notice_sec: int | None = None) -> dict:
        """Set or move the time of a change that is not applied yet. Always needs a new /pushok:
        the time is part of what the user approves."""
        with self.locked():
            t = self._expire(self._load(tid))
            if t["state"] not in OPEN_STATES:
                raise RudderError(f"change {tid} is {t['state']}; only an open change can be scheduled")
            old = t.get("schedule") or {}
            if old.get("state") in ("notified", "running"):
                raise RudderError(f"change {tid} is starting now; stop it with /pushno from the notice, or change_cancel")
            sc = self._schedule_for(at, timezone, notice_sec)
            self.channel.close(t["approval"])
            t["schedule"] = sc
            t["expires"] = self._sched_expires(sc)
            t["hash"] = self._hash(t)
            t["state"] = "waiting_approval"
            try:
                rq = (t.get("requested_by") or {}).get("id")
                t["approval"] = self.channel.request(f"RUDDER {tid}: {', '.join(d['device'] for d in t['devices'])}",
                                                     lambda code: self._message(t, code),
                                                     **({"requester": rq} if rq else {}))
                t.pop("bypass", None)
            except approval.ApprovalError as e:
                t["approval"] = {}
                t["state"] = "cancelled"
                self._unlock(t)
                self._save(t)
                self._audit(t, "cancelled: could not send the approval request for the new time")
                raise RudderError(f"{e}; the change was cancelled") from None
            self._save(t)
            self._audit(t, f"scheduled {sc['at_text']}; new approval requested")
        return self.status(tid)

    @staticmethod
    def _hash(t: dict) -> str:
        body = {"inventory": t["inventory"], "lines": t["lines"],
                "devices": [(d["device"], d["baseline_sha"], d["diff"]) for d in t["devices"]],
                "verify": t["verify_commands"], "expect": t["expect"], "save": t["save"]}
        if t.get("schedule"):
            body["at"] = round(t["schedule"]["at"])
        if t.get("validation"):                    # the approval covers the validated result too
            body["validation"] = [t["validation"]["validator"], t["validation"]["status"], t["validation"]["summary"]]
        sn = t.get("servicenow") or {}
        if sn.get("mode") == "gate":                # ...and the change request it is for
            body["change_request"] = [sn.get("number"), bool(sn.get("emergency"))]
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    # ------------------------------------------------------------------ status / list / cancel
    def status(self, tid: str) -> dict:
        with self.locked():
            t = self._expire(self._load(tid))
            appr = self.channel.state(t["approval"]) if t["state"] in OPEN_STATES and t.get("approval") else None
            if appr in ("approved", "rejected") and t["state"] in OPEN_STATES:
                who = getattr(self.channel, "who", lambda h: "")(t["approval"])
                if who:
                    t["decided_by"] = who
            if appr == "approved" and t["state"] == "waiting_approval":
                t["state"] = "approved"
                self._save(t)
                self._audit(t, "approved by " + (t.get("decided_by") or "the approver"))
            if appr == "rejected" and t["state"] in OPEN_STATES:
                t["state"] = "rejected"
                self.channel.close(t["approval"])
                self._unlock(t)
                self._save(t)
                self._audit(t, "rejected by " + (t.get("decided_by") or "the approver"))
        sc = t.get("schedule")
        where = t["approval"].get("where", "the approval channel") if t.get("approval") else "the approval channel"
        auto = sched_mod._truthy(sched_mod.settings(self.s).get("AUTO_APPLY"))
        nxt = {"waiting_approval": f"Ask the user to approve on {where} "
                                   f"(/pushok with the code sent there). Never ask for the code in chat."
                                   + (" RUDDER applies it by itself as soon as it is approved and sends the "
                                      "result there; check change status for the result." if auto else ""),
               "approved": (f"Approved. RUDDER is applying it by itself (within seconds): check change status "
                            f"again for the result. Call change_apply(ticket='{tid}') only if it still says "
                            f"approved after 2 minutes." if auto else
                            f"Approved. Apply with change_apply(ticket='{tid}')."),
               "applying": "Being applied now. Check change status again in a few seconds.",
               "interrupted": "The apply was cut off (RUDDER stopped). Tell the user to check the devices marked "
                              "check_device; nothing is retried. Propose again if still needed.",
               "applied": "Applied and verified.",
               "failed": "Failed; see devices and log (rolled back where possible).",
               "rejected": "Rejected by the user. Nothing was changed.",
               "expired": "Expired before it was applied. Nothing was changed; propose again if still needed.",
               "cancelled": "Cancelled. Nothing was changed."}.get(t["state"], "")
        if sc and t["state"] in OPEN_STATES:
            nxt = (f"Scheduled for {sc['at_text']}. " + (nxt if t["state"] == "waiting_approval" else
                   "Approved: the scheduler applies it then (do NOT call change_apply). ")
                   + f"{sc['notice_sec']} s before, the user gets a notice with a code to stop it. "
                     f"To move it: change_schedule; to drop it: change_cancel.")
        out = {"ticket": tid, "state": t["state"], "description": t["description"], "inventory": t["inventory"],
               "expires_in_min": max(0, round((t["expires"] - _now()) / 60)) if t["state"] in OPEN_STATES else None,
               "devices": [{k: d.get(k) for k in ("device", "platform", "adapter", "state", "diff", "error",
                                                   "checkpoint", "manual_rollback") if d.get(k) is not None}
                           for d in t["devices"]],
               "lines": t["lines"], "save": t["save"], "verify_commands": t["verify_commands"],
               "log": t["log"][-20:], "next": nxt,
               "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created"]))}
        for k in ("ticket_id", "project", "decided_by"):
            if t.get(k):
                out[k] = t[k]
        if t.get("validation"):
            out["validation"] = {k: t["validation"][k] for k in ("validator", "status", "summary", "details")}
        if t.get("servicenow"):
            out["servicenow"] = dict(t["servicenow"])
            g = t["servicenow"]
            if g.get("mode") == "gate" and t["state"] == "approved" and not sc and not g.get("emergency"):
                out["next"] = (f"Approved. ServiceNow gate: RUDDER pushes it only while {g['number']} is in "
                               f"Implement inside its planned window; the scheduler tries on each pass until "
                               f"{servicenow._fmt(g['window'][1])} UTC. Do not call change_apply again.")
        after =[d["validation"] for d in t["devices"] if d.get("validation")]
        if after:
            out["validation_after_push"] = [{k: r.get(k) for k in ("device", "status", "summary")} for r in after]
        f = t.get("failure") or {}
        if f.get("kind") == "validation":
            out["attention"] = True
            out["failure"] = {"kind": "validation", "summary": f.get("summary"), "details": f.get("details")}
            out["next"] = ("FAILED validation before the push: nothing was changed and no approval was opened "
                           "(RUDDER already told the approval channel). Tell the user each finding in "
                           "validation.details, fix the lines, and propose again as a new change.")
        if f.get("kind") == "refused_by_device":
            out["attention"] = True
            out["failure"] = {k: f.get(k) for k in ("device", "line", "error", "investigation", "finding",
                                                    "alternative", "new_ticket", "alerted") if f.get(k)}
            if f.get("investigation") != "done":
                out["next"] = (
                    f"FAILED, needs human attention: {f.get('device')} refused '{f.get('line')}' in the dry run. "
                    f"Nothing was changed and no approval was opened; RUDDER already alerted the approval channel "
                    f"and this chat. Now INVESTIGATE before anything else: (1) why the device refused it (platform, "
                    f"interface type, software version, mode; read the device with `device` and look it up with "
                    f"`research`, generic words only); (2) find lines that do what the user asked and that this "
                    f"device accepts; (3) record it: change action=investigate ticket={tid} finding=<why> "
                    f"alternative=[<lines>] (+ new_ticket= if you proposed it); (4) tell the user the refused line, "
                    f"the reason and the alternative, and propose the alternative as a NEW change only with all the "
                    f"other requested items (it needs its own approval). Never drop the item silently, and say "
                    f"when the syntax was your own choice rather than the user's.")
            else:
                out["next"] = ("FAILED (investigated). Tell the user the finding and the alternative"
                               + (f"; the new change is {f['new_ticket']}" if f.get("new_ticket") else
                                  "; propose the alternative as a new change if the user agrees") + ".")
        if t.get("left_out"):
            out["left_out"] = t["left_out"]
            if t["state"] in OPEN_STATES:
                out["next"] = ("Tell the user these requested lines are NOT in this change (the device refused them "
                               "earlier): " + "; ".join(x["line"] for x in t["left_out"]) + ". " + out["next"])
        if t.get("requested_by"):
            out["requested_by"] = _name(t["requested_by"])
        if t.get("bypass"):
            out["approval"] = f"none needed: {t['bypass']['why']} ({_name(t['bypass']['by'])})"
        if self.page_url(t):
            out["page"] = self.page_url(t)
        if sc:
            out["schedule"] = {k: sc.get(k) for k in ("at_text", "tz", "notice_sec", "state", "why", "result")
                               if sc.get(k) is not None}
        return out

    def list(self, since: str | None = None, until: str | None = None, device: str | None = None,
             inventory: str | None = None, requested_by: str | None = None, platform: str | None = None,
             state: str | None = None, ticket_id: str | None = None, project: str | None = None,
             limit: int = 50) -> list[dict]:
        """Tickets, newest first, filtered by any mix of: created since / until (a day or a time), device,
        inventory, who asked (ID or name, part of it is enough), platform / device type (EOS, ARISTA_EOS,
        PANOS...), state, outside ticket ID, project."""
        lo, hi = _day(since), _day(until, end=True)
        limit = max(1, min(int(limit or 50), 500))

        def has(val, want) -> bool:
            return str(want).strip().lower() in str(val or "").lower()

        tickets = []
        for p in self.tdir.glob("C*.json"):
            try:
                tickets.append(json.loads(p.read_text()))
            except ValueError:
                continue
        out = []
        for t in sorted(tickets, key=lambda x: x.get("created", 0), reverse=True):
            created = t.get("created", 0)
            plats = sorted({d.get("platform", "") for d in t["devices"]})
            if (lo and created < lo) or (hi and created > hi):
                continue
            if device and not any(d["device"].upper() == device.strip().upper() for d in t["devices"]):
                continue
            if inventory and inv_key(t.get("inventory")) != inv_key(inventory):
                continue
            if requested_by and not has(_name(t.get("requested_by")), requested_by):
                continue
            if platform and not any(has(x, platform) or has(platform, x) for x in plats if x):
                continue
            if ticket_id and str(t.get("ticket_id", "")).lower() != str(ticket_id).strip().lower():
                continue
            if project and str(t.get("project", "")).lower() != str(project).strip().lower():
                continue
            t = self._expire(t)
            if state and t["state"] != state.strip().lower():
                continue
            row = {"ticket": t["id"], "state": t["state"],
                   "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(created)),
                   "inventory": t.get("inventory"), "devices": [d["device"] for d in t["devices"]],
                   "platform": plats, "description": t["description"]}
            for k in ("ticket_id", "project", "decided_by"):
                if t.get(k):
                    row[k] = t[k]
            if t.get("requested_by"):
                row["requested_by"] = _name(t["requested_by"])
            if t.get("bypass"):
                row["approval"] = "bypass (SHIP engineer)"
            if t.get("schedule"):
                row["at"] = t["schedule"]["at_text"]
                row["schedule"] = t["schedule"]["state"]
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def cancel(self, tid: str, note: str | None = None) -> dict:
        with self.locked():
            t = self._expire(self._load(tid))
            if t["state"] not in OPEN_STATES:
                raise RudderError(f"change {tid} is {t['state']}; nothing to cancel")
            self.channel.close(t["approval"])
            sc = t.get("schedule")
            if sc and sc.get("state") in ("scheduled", "notified"):
                if sc.get("cancel"):
                    self.channel.close(sc["cancel"])
                sc["state"] = "cancelled"
            self._unlock(t)
            t["state"] = "cancelled"
            self._save(t)
            self._audit(t, "cancelled" + (f" ({note})" if note else ""))
        return self.status(tid)

    # ------------------------------------------------------------------ apply
    def apply(self, tid: str, by_scheduler: bool = False) -> dict:
        with self.locked():
            st = self.status(tid)
            t = self._load(tid)
            sc = t.get("schedule")
            if sc and not by_scheduler:
                raise RudderError(f"change {tid} is scheduled for {sc['at_text']}: the scheduler applies it then. "
                                  f"To run it now instead, change_schedule it to a near time (needs a new /pushok).")
            if t["state"] != "approved":
                raise RudderError(f"change {tid} is {t['state']}: {st['next']}")
            if self._hash(t) != t["hash"]:
                raise RudderError("the ticket was altered after the approval request; refused")
            self._sn_gate_check(t)                    # gate: change request in Implement, inside its window
            t["state"] = "applying"
            self.channel.close(t["approval"])            # an approval is used once
            self._save(t)
            self._audit(t, "apply started" + (" (scheduler)" if by_scheduler else ""))
        failed = False
        for d in t["devices"]:
            if failed:
                d["state"] = "not_touched"
                continue
            try:
                self._apply_one(t, d)
            except (RudderError, AtlasError, adapters.DeviceError) as e:
                d["error"] = str(e)
                failed = True
            self._save(t)
        t["state"] = "failed" if failed else "applied"
        self._unlock(t)
        self._save(t)
        self._audit(t, t["state"] + " " + ", ".join(f"{d['device']}={d['state']}" for d in t["devices"]))
        self._sn_finished(t)
        return self.status(tid)

    def recover_interrupted(self, older_than_sec: float = INTERRUPTED_SEC) -> list[str]:
        """Tickets left in 'applying' by a crash or restart (no update for older_than_sec): mark them
        'interrupted', mark the devices that were in the middle 'check_device', release the locks.
        Nothing is retried: an EOS commit timer has rolled an unconfirmed change back by now, a checkpoint
        writer may need the manual rollback the ticket shows; a person checks the device."""
        out = []
        with self.locked():
            for p in sorted(self.tdir.glob("C*.json")):
                try:
                    if _now() - p.stat().st_mtime < older_than_sec:
                        continue
                    t = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                if t.get("state") != "applying" or t.get("consumer") != self.s.consumer:
                    continue
                for d in t["devices"]:
                    if d.get("state") in ("applying", "committed_pending_confirm"):
                        was = d["state"]
                        d["state"] = "check_device"
                        self._log(t, d, f"apply interrupted while {was} (RUDDER stopped); check the device"
                                  + (f"; manual rollback: {d['manual_rollback']}" if d.get("manual_rollback") else
                                     "; an unconfirmed EOS commit rolls back by its timer"))
                    elif d.get("state") == "pending":
                        d["state"] = "not_touched"
                t["state"] = "interrupted"
                self._unlock(t)
                self._save(t)
                self._audit(t, "interrupted: the apply did not finish (RUDDER stopped); devices to check: "
                            + (", ".join(d["device"] for d in t["devices"] if d["state"] == "check_device") or "none"))
                out.append(t["id"])
        return out

    def _log(self, t, d, msg):
        t["log"].append(f"{time.strftime('%H:%M:%S')} {d['device']}: {msg}")

    def _apply_one(self, t: dict, d: dict) -> None:
        dev = next(iter(self.atlas.records(t["inventory"], names=[d["device"]])), None)
        if dev is None:
            raise RudderError(f"{d['device']} is no longer in {t['inventory']}")
        plat = self.atlas.platform(dev.get("OS"))
        writer, why = adapters.choose_writer(plat, dev, self.s, self.factories)
        if writer is None:
            raise RudderError(why)
        cred = self.atlas.credential(dev["CREDENTIAL"])
        try:
            now = writer.dry_run(dev, cred, t["lines"], t["session"] + "-chk")
            if now != d["diff"]:
                d["state"] = "drifted"
                raise RudderError(f"{d['device']}: the device changed since the approval (the diff is "
                                  f"different now); nothing applied. Propose again.")
            post = validation.post_push(self.s) and hasattr(writer, "candidate_config")
            pre_running = writer.running_config(dev, cred) if post else None
            timer = getattr(writer, "timer_rollback", True)
            if not timer:
                d["checkpoint"] = writer.checkpoint_name(t["session"])
                d["manual_rollback"] = writer.manual_rollback(t["session"])
            d["state"] = "applying"
            self._save(t)
            writer.apply(dev, cred, t["lines"], t["session"], self.s.commit_timer)
            d["state"] = "committed_pending_confirm"
            self._log(t, d, f"committed with rollback timer {self.s.commit_timer}" if timer else
                      f"committed; rollback point {d['checkpoint']} kept until verified")
            ok, why = self._verify(t, d, dev, plat, writer, cred)
            if ok and post:
                r = validation.run(self.s, "after", {d["device"]: pre_running},
                                   {d["device"]: writer.running_config(dev, cred)}, {d["device"]: plat["ID"]},
                                   [d["device"]], f"{t['id']}-{d['device']}")
                if r:
                    d["validation"] = {"device": d["device"], **r}
                    self._log(t, d, validation.line(r))
                    if r["status"] == "fail":
                        ok, why = False, f"validation after the push: {r['summary']}"
            if not ok:
                writer.abort(dev, cred, t["session"])
                d["state"] = "rolled_back"
                self._log(t, d, f"verify failed ({why}); rolled back")
                raise RudderError(f"{d['device']}: verify failed ({why}); rolled back")
            writer.confirm(dev, cred, t["session"])
            d["state"] = "applied"
            self._log(t, d, "verified and confirmed")
            if t["save"]:
                writer.save(dev, cred)
                self._log(t, d, "saved to startup-config")
        except adapters.DeviceError as e:
            if d["state"] == "committed_pending_confirm":
                try:
                    writer.abort(dev, cred, t["session"])
                    d["state"] = "rolled_back"
                    self._log(t, d, f"error after commit ({e.kind}); rolled back")
                except adapters.DeviceError:
                    if getattr(writer, "timer_rollback", True):
                        d["state"] = "rollback_by_timer"
                        self._log(t, d, f"error after commit and the abort failed; the switch rolls back "
                                        f"by itself when the {self.s.commit_timer} timer runs out")
                    else:
                        d["state"] = "needs_manual_rollback"
                        self._log(t, d, f"error after commit and the rollback failed; roll back by hand on "
                                        f"the device: {d.get('manual_rollback')}")
            elif d["state"] == "applying":
                d["state"] = "failed"                  # the writer refused or rolled back by itself
                self._log(t, d, f"apply failed: {e}")
            raise
        finally:
            cred.clear()

    def _verify(self, t, d, dev, plat, writer, cred) -> tuple[bool, str]:
        # 1. the change is fully in place: the same lines now change nothing. If the device will not
        #    open a second session while the commit timer runs, check the running config instead.
        try:
            rest = writer.dry_run(dev, cred, t["lines"], t["session"] + "-ver")
            if re.search(r"^[+-]", rest, re.M):
                return False, "the running config does not hold all the lines"
        except adapters.DeviceError:
            running = writer.running_config(dev, cred)
            have = {ln.strip() for ln in running.splitlines()}
            missing = [ln.strip() for ln in t["lines"]
                       if not ln.strip().lower().startswith("no ") and ln.strip() not in have]
            if missing:
                return False, f"not in the running config: {'; '.join(missing[:3])}"
        # 2. the user's verify commands run and contain what they expect
        if t["verify_commands"]:
            res = writer.run(dev, cred, t["verify_commands"])
            for r in res:
                if not r["ok"]:
                    return False, f"'{r['command']}' failed"
                text = r["output"] if isinstance(r["output"], str) else json.dumps(r["output"])
                for want in (t["expect"] or {}).get(r["command"], []):
                    if str(want) not in text:
                        return False, f"'{r['command']}' does not show {want!r}"
        return True, "ok"
