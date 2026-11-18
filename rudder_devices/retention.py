"""Retention for change tickets and their pages: a daily clean-up inside rudder-scheduler.

  APPROVAL.PAGES_KEEP_DAYS  90 (default): a finished ticket, its change page and its baseline records are
                            kept this long after the ticket's last update, then removed.
  APPROVAL.CLEANUP_AT       "02:00" (default): local time of the daily clean-up, in SCHEDULE.TIMEZONE;
                            "off" = never clean up.

Open tickets (waiting, approved, applying, scheduled) are never removed. The audit log is never trimmed:
it keeps one line per step of every ticket, including "removed after N days (retention)".
If the scheduler is down at the clean-up time, the clean-up runs at its next pass that day or later.
The old APPROVAL.WEB_KEEP_MIN (minutes a page stayed readable) is replaced by PAGES_KEEP_DAYS.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_DAYS = 90
DEFAULT_AT = "02:00"
AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
KEEP_STATES = ("waiting_approval", "approved", "applying", "proposing")


def keep_days(appr: dict | None) -> int:
    try:
        d = int((appr or {}).get("PAGES_KEEP_DAYS", DEFAULT_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(d, 3650))


def cleanup_at(appr: dict | None) -> tuple[int, int] | None:
    """(hour, minute), or None when the clean-up is off."""
    v = str((appr or {}).get("CLEANUP_AT", DEFAULT_AT) or "").strip().lower()
    if v in ("off", "none", "false", "no", "0", "-"):
        return None
    m = AT_RE.match(v)
    if not m:
        h, mi = (int(x) for x in DEFAULT_AT.split(":"))
        return h, mi
    return int(m.group(1)), int(m.group(2))


def due(now: float, at: tuple[int, int] | None, tz: str, last_day: str | None) -> str | None:
    """The local day to clean up for, if the clean-up is due now; else None."""
    if at is None:
        return None
    local = dt.datetime.fromtimestamp(now, ZoneInfo(tz))
    day = local.strftime("%Y-%m-%d")
    if last_day == day or (local.hour, local.minute) < at:
        return None
    return day


def _finished(t: dict) -> bool:
    if t.get("state") in KEEP_STATES:
        return False
    return (t.get("schedule") or {}).get("state") not in ("scheduled", "notified", "running")


def clean(state_path: Path, records_path: Path | None, days: int, now: float | None = None,
          audit=None) -> dict:
    """Remove finished tickets (and their baseline records) not updated for `days` days."""
    now = time.time() if now is None else now
    limit = now - days * 86400
    tdir = state_path / "tickets"
    removed, records = [], 0
    rec_root = records_path.resolve() if records_path else None
    for p in sorted(tdir.glob("C*.json")):
        try:
            if p.stat().st_mtime > limit:
                continue
            t = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not _finished(t):
            continue
        for d in t.get("devices") or []:
            rec = d.get("baseline_record")
            if not rec or rec_root is None:
                continue
            rp = Path(rec).resolve()
            if rp.is_dir() and rec_root in rp.parents:            # only inside RUDDER's own records
                shutil.rmtree(rp, ignore_errors=True)
                records += 1
        p.unlink(missing_ok=True)
        removed.append(t.get("id", p.stem))
        if audit:
            audit(t, f"removed after {days} days (retention): ticket, change page, baseline records")
    return {"removed": removed, "records": records, "days": days}


class Daily:
    """Remembers the last clean-up day in <STATE_PATH>/cleanup.json."""

    def __init__(self, state_path: Path):
        self.path = state_path / "cleanup.json"

    def last(self) -> str | None:
        try:
            return json.loads(self.path.read_text()).get("last")
        except (OSError, ValueError, AttributeError):
            return None

    def done(self, day: str, result: dict) -> None:
        self.path.write_text(json.dumps({"last": day, "removed": len(result["removed"]),
                                         "records": result["records"], "days": result["days"]}))
