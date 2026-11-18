"""Scheduled changes: approve now with /pushok, RUDDER applies at the time you chose.

Flow for a ticket proposed with `at` (or rescheduled with change_schedule):
  1. The approval request names the time. /pushok now approves exactly this change at exactly that time.
  2. NOTICE_SEC before the time (default 30 s) the runner sends a notice with a fresh one-time code:
       /pushno CODE  stops it (nothing is changed),
       /pushok CODE  starts it at once,
       no answer     it runs when the notice time is up (you approved it earlier: default yes).
  3. The runner applies through the same path as change_apply (drift check, commit timer or
     checkpoint, verify, confirm or roll back) and sends the result.
Safety:
  - a scheduled ticket can only be applied by the runner, never by change_apply;
  - not approved by the notice time -> not applied (missed);
  - the runner was down and is more than LATE_MIN late -> not applied (missed);
  - any change of time needs a new /pushok (the time is part of the approved hash).

Unscheduled tickets (AUTO_APPLY, default on): the same runner applies a ticket as soon as the user's
/pushok has approved it and sends the result to the approval channel, so nobody has to go back to the
chat and say "approved". The agent can still call change_apply; the ticket lock makes sure only one runs.

The runner is self-contained: `rudder-scheduler run` (systemd user service rudder-scheduler).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import signal
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import RudderError

log = logging.getLogger("rudder.scheduler")

DEFAULTS = {"TIMEZONE": "America/New_York", "NOTICE_SEC": 30, "LATE_MIN": 10, "MAX_DAYS": 7, "POLL_SEC": 5,
            "AUTO_APPLY": True}

# Abbreviations people type. US ones mean the local wall clock there (EST in July = EDT).
ZONES = {"EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
         "EASTERN": "America/New_York", "CST": "America/Chicago", "CDT": "America/Chicago",
         "CT": "America/Chicago", "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
         "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
         "UTC": "UTC", "GMT": "UTC", "Z": "UTC", "IST": "Asia/Kolkata"}


def _truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "none", "")


def settings(s) -> dict:
    out = dict(DEFAULTS)
    out.update({str(k).upper(): v for k, v in (getattr(s, "schedule", None) or {}).items()})
    return out


def zone(name: str | None, default: str) -> ZoneInfo:
    n = (name or default).strip()
    try:
        return ZoneInfo(ZONES.get(n.upper(), n))
    except (ZoneInfoNotFoundError, ValueError):
        raise RudderError(f"unknown time zone {n!r} (use e.g. America/New_York, EST, UTC)") from None


def parse_at(at: str, tz: str | None, cfg: dict, now: float | None = None) -> tuple[float, str]:
    """'2026-11-13 02:00', '2026-11-13T02:00 EST', '02:00' (next one), ISO with offset.
    Returns (epoch, zone name)."""
    now = time.time() if now is None else now
    text = str(at or "").strip()
    if not text:
        raise RudderError("give the time, e.g. '2026-11-13 02:00' or '02:00'")
    m = re.match(r"^(.*?)\s+([A-Za-z][A-Za-z_/+-]*)$", text)
    if m and not tz and (m.group(2).upper() in ZONES or "/" in m.group(2)):
        text, tz = m.group(1), m.group(2)
    z = zone(tz, str(cfg["TIMEZONE"]))
    if re.match(r"^\d{1,2}:\d\d(:\d\d)?$", text):
        hh, mm, *ss = (int(x) for x in text.split(":"))
        base = dt.datetime.fromtimestamp(now, z)
        when = base.replace(hour=hh, minute=mm, second=ss[0] if ss else 0, microsecond=0)
        if when.timestamp() <= now:
            when = when + dt.timedelta(days=1)
            when = when.replace(tzinfo=None).replace(tzinfo=z)        # keep the wall time across DST
    else:
        try:
            when = dt.datetime.fromisoformat(text.replace(" ", "T", 1))
        except ValueError:
            raise RudderError(f"cannot read the time {at!r}; use 'YYYY-MM-DD HH:MM' or 'HH:MM'") from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=z)
    return when.timestamp(), z.key


def fmt(epoch: float, tzname: str) -> str:
    return dt.datetime.fromtimestamp(epoch, ZoneInfo(tzname)).strftime("%a %Y-%m-%d %H:%M:%S %Z")


def check_window(epoch: float, notice: int, cfg: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if epoch <= now + notice + 5:
        raise RudderError(f"the time must be at least {notice + 5} s from now (the notice comes {notice} s before)")
    if epoch > now + float(cfg["MAX_DAYS"]) * 86400:
        raise RudderError(f"at most {cfg['MAX_DAYS']} days ahead")


# ---------------------------------------------------------------------------------------- runner
class Scheduler:
    """One pass = tick(). Holds no state of its own: everything is in the ticket files."""

    def __init__(self, changes, clock=time.time):
        self.c = changes
        self.cfg = settings(changes.s)
        self.clock = clock

    def due(self) -> list[str]:
        out = []
        for p in sorted(self.c.tdir.glob("C*.json")):
            try:
                t = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            sc = t.get("schedule") or {}
            if sc.get("state") in ("scheduled", "notified") and t.get("consumer") == self.c.s.consumer:
                out.append(t["id"])
        return out

    def waiting(self) -> list[str]:
        """Unscheduled open tickets of this consumer: applied as soon as they are approved."""
        out = []
        for p in sorted(self.c.tdir.glob("C*.json")):
            try:
                t = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if (not t.get("schedule") and t.get("consumer") == self.c.s.consumer
                    and t.get("state") in ("waiting_approval", "approved")):
                out.append(t["id"])
        return out

    def tick(self) -> list[str]:
        events = []
        try:
            for tid in self.c.recover_interrupted():
                self._say(f"⚠️ Change {tid} was INTERRUPTED: RUDDER stopped during the apply. Check the device(s); "
                          f"nothing is retried (change status shows which).", tid)
                events.append(f"{tid}: interrupted")
        except Exception:  # noqa: BLE001
            log.exception("interrupted-apply check failed")
        try:
            for tid, why in self.c.servicenow_watch():
                self._say(f"🛑 Change {tid} CANCELLED: {why}. Nothing was changed; its locks are released.", tid)
                events.append(f"{tid}: cancelled ({why})")
        except Exception:  # noqa: BLE001
            log.exception("ServiceNow watch failed")
        jobs = [(tid, self.step) for tid in self.due()]
        if _truthy(self.cfg.get("AUTO_APPLY")):
            jobs += [(tid, self.on_approval) for tid in self.waiting()]
        for tid, fn in jobs:
            try:
                ev = fn(tid)
            except Exception as e:  # noqa: BLE001   one bad ticket must not stop the others
                log.exception("scheduler step failed for %s", tid)
                ev = f"{tid}: error {type(e).__name__}: {e}"
            if ev:
                log.info(ev)
                events.append(ev)
        ev = self.cleanup()
        if ev:
            log.info(ev)
            events.append(ev)
        return events

    def cleanup(self) -> str | None:
        """The daily retention clean-up (retention.py), at APPROVAL.CLEANUP_AT local time."""
        from . import retention
        appr = self.c.s.approval or {}
        daily = retention.Daily(self.c.s.state_path)
        try:
            day = retention.due(self.clock(), retention.cleanup_at(appr), str(self.cfg["TIMEZONE"]), daily.last())
            if not day:
                return None
            with self.c.locked():
                res = retention.clean(self.c.s.state_path, getattr(self.c.s, "records_path", None),
                                      retention.keep_days(appr), now=self.clock(), audit=self.c._audit)
            daily.done(day, res)
        except Exception as e:  # noqa: BLE001   never stops the scheduler
            log.exception("retention clean-up failed")
            return f"retention clean-up failed: {type(e).__name__}: {e}"
        if not res["removed"]:
            log.info("retention clean-up %s: nothing older than %s days", day, res["days"])
            return None
        return (f"retention clean-up {day}: {len(res['removed'])} ticket(s) older than {res['days']} days removed"
                f" ({res['records']} baseline record(s))")

    def on_approval(self, tid: str) -> str | None:
        """Apply an unscheduled ticket right after /pushok, then report on the approval channel."""
        st = self.c.status(tid)                              # refreshes approval, rejection, expiry
        if st["state"] != "approved":
            return None
        try:
            res = self.c.apply(tid)
        except RudderError as e:
            if type(e).__name__ == "GateWait":                # ServiceNow gate: not yet; try on the next pass
                return None
            now = self.c._load(tid)["state"]
            if now != "approved":                            # the agent's change_apply got there first
                return None
            try:                                             # refused before touching a device: do not retry
                self.c.cancel(tid, note=f"auto-apply refused ({e})")
            except RudderError:
                pass
            res = {"state": "failed", "devices": [], "error": str(e)}
        lines = [f"{d['device']}: {d.get('state')}" + (f" ({d['error']})" if d.get("error") else "")
                 for d in res.get("devices", [])]
        icon = "✅" if res["state"] == "applied" else "❌"
        self._say(f"{icon} Change {tid}: {res['state']}\n{st['description']}\n" + "\n".join(lines)
                  + (f"\n{res['error']}" if res.get("error") else ""), tid)
        return f"{tid}: {res['state']} (after approval)"

    def _say(self, text: str, tid: str | None = None) -> None:
        """To the approval channel, and to the chat the change came from (ticket reply_to), if any."""
        try:
            self.c.channel.notify(text)
        except Exception as e:  # noqa: BLE001
            log.warning("could not send: %s", e)
        if not tid:
            return
        try:
            target = self.c._load(tid).get("reply_to")
            if target and hasattr(self.c.channel, "reply"):
                self.c.channel.reply(target, text)
        except Exception as e:  # noqa: BLE001
            log.warning("could not send the reply for %s: %s", tid, e)

    def _missed(self, tid: str, why: str) -> str:
        with self.c.locked():
            t = self.c._load(tid)
            sc = t["schedule"]
            if sc.get("cancel"):
                self.c.channel.close(sc["cancel"])
            sc["state"] = "missed"
            sc["why"] = why
            self.c._save(t)
            if t["state"] in ("waiting_approval", "approved"):
                self.c.cancel(tid, note=f"missed: {why}")
        self._say(f"⏭ Change {tid} was NOT applied: {why}. Nothing was changed.", tid)
        return f"{tid}: missed ({why})"

    def step(self, tid: str) -> str | None:
        now = self.clock()
        with self.c.locked():
            st = self.c.status(tid)                          # refreshes approval, rejection, expiry
            t = self.c._load(tid)
            sc = t["schedule"]
            at, notice = sc["at"], int(sc["notice_sec"])
            late = float(self.cfg["LATE_MIN"]) * 60
            if t["state"] not in ("waiting_approval", "approved"):
                sc["state"] = t["state"] if t["state"] in ("cancelled", "rejected", "expired") else "done"
                if sc.get("cancel"):
                    self.c.channel.close(sc["cancel"])
                self.c._save(t)
                return f"{tid}: {t['state']}, not scheduled any more"
            if sc["state"] == "scheduled":
                if now < at - notice:
                    return None
                if t["state"] != "approved":
                    return self._missed(tid, f"not approved by {fmt(at - notice, sc['tz'])}")
                if now > at + late:
                    return self._missed(tid, f"the scheduler was {round((now - at) / 60)} min late "
                                             f"(more than LATE_MIN {self.cfg['LATE_MIN']})")
                go_at = max(at, now + notice)
                devs = ", ".join(d["device"] for d in t["devices"])

                def message(code: str) -> str:
                    return (f"⏰ Change {tid} starts in {round(go_at - now)} s ({fmt(go_at, sc['tz'])})\n"
                            f"{t['description']}\nDevices: {devs}\n\n"
                            f"To STOP it: /pushno {code}\n"
                            f"To start it now: /pushok {code}\n"
                            f"No answer = it runs (you approved it earlier).")

                try:
                    rq = (t.get("requested_by") or {}).get("id")
                    sc["cancel"] = self.c.channel.request(f"RUDDER {tid} starts at {fmt(go_at, sc['tz'])}", message,
                                                          **({"requester": rq} if rq else {}))
                except Exception as e:  # noqa: BLE001   retried next tick; missed after LATE_MIN
                    return f"{tid}: notice not sent yet ({e})"
                sc.update(state="notified", notified=now, go_at=go_at)
                self.c._save(t)
                self.c._audit(t, f"schedule notice sent; starts {fmt(go_at, sc['tz'])}")
                return f"{tid}: notice sent, starts {fmt(go_at, sc['tz'])}"
            # notified
            answer = self.c.channel.state(sc["cancel"])
            if answer == "rejected":
                self.c.channel.close(sc["cancel"])
                sc["state"] = "cancelled"
                self.c._save(t)
                self.c.cancel(tid, note="stopped from the schedule notice (/pushno)")
                stop = True
            else:
                stop = False
                if answer != "approved" and now < sc["go_at"]:
                    return None
                if now > sc["go_at"] + late:
                    return self._missed(tid, "the scheduler was late after the notice")
                self.c.channel.close(sc["cancel"])
                sc["state"] = "running"
                sc["started"] = now
                self.c._save(t)
        if stop:
            self._say(f"🛑 Change {tid} stopped. Nothing was changed.", tid)
            return f"{tid}: stopped by the user"
        try:
            res = self.c.apply(tid, by_scheduler=True)
        except RudderError as e:                         # refused before touching a device
            res = {"state": "failed", "devices": [], "error": str(e)}
            try:
                self.c.cancel(tid, note=f"scheduler: apply refused ({e})")
            except RudderError:
                pass
        with self.c.locked():
            t = self.c._load(tid)
            t["schedule"]["state"] = "done"
            t["schedule"]["result"] = res["state"]
            self.c._save(t)
        lines = [f"{d['device']}: {d.get('state')}" + (f" ({d['error']})" if d.get("error") else "")
                 for d in res.get("devices", [])]
        icon = "✅" if res["state"] == "applied" else "❌"
        self._say(f"{icon} Scheduled change {tid}: {res['state']}\n" + "\n".join(lines)
                  + (f"\n{res['error']}" if res.get("error") else ""), tid)
        return f"{tid}: {res['state']}"

    def run(self, poll: float | None = None) -> None:
        poll = float(poll or self.cfg["POLL_SEC"])
        stop = {"now": False}

        def _term(signum, frame):
            stop["now"] = True

        signal.signal(signal.SIGTERM, _term)
        log.info("rudder-scheduler running for consumer %s, every %.0f s", self.c.s.consumer, poll)
        srv = None
        try:
            from . import web
            ch = self.c.channel
            srv = web.serve(self.c.s.state_path, self.c.s.approval or {},
                            getattr(ch, "how", lambda: "/pushok <code>")(), getattr(ch, "where", "the approval channel"))
        except Exception as e:  # noqa: BLE001   pages are a convenience: the scheduler runs without them
            log.warning("change pages not started: %s", e)
        while not stop["now"]:
            self.tick()
            for _ in range(int(poll * 10)):
                if stop["now"]:
                    break
                time.sleep(0.1)
        if srv:
            srv.shutdown()
        log.info("rudder-scheduler stopped")


def main(argv: list[str] | None = None) -> int:
    from . import config
    from .changes import Changes
    ap = argparse.ArgumentParser(prog="rudder-scheduler", description="Applies scheduled RUDDER changes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="keep running (the systemd service)")
    r.add_argument("--poll", type=float)
    sub.add_parser("once", help="one pass, then exit")
    sub.add_parser("list", help="scheduled changes")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    sch = Scheduler(Changes(config.load()))
    if a.cmd == "run":
        sch.run(a.poll)
    elif a.cmd == "once":
        for ev in sch.tick():
            print(ev)
    else:
        for row in sch.c.list():
            if row.get("at"):
                print(f"{row['ticket']}  {row['state']:<17} {row.get('schedule'):<10} {row['at']}  "
                      f"{', '.join(row['devices'])}  {row['description']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
