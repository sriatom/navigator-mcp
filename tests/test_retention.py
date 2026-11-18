"""Retention: finished tickets, their pages and baseline records go after PAGES_KEEP_DAYS, daily at CLEANUP_AT."""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import pytest

from rudder_devices import retention as R

TZ = "America/New_York"


def _at(day: str, hm: str) -> float:
    return dt.datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=ZoneInfo(TZ)).timestamp()


def test_settings_defaults_and_limits():
    assert R.keep_days({}) == 90 and R.keep_days({"PAGES_KEEP_DAYS": "30"}) == 30
    assert R.keep_days({"PAGES_KEEP_DAYS": 0}) == 1 and R.keep_days({"PAGES_KEEP_DAYS": "x"}) == 90
    assert R.cleanup_at({}) == (2, 0) and R.cleanup_at({"CLEANUP_AT": "3:30"}) == (3, 30)
    assert R.cleanup_at({"CLEANUP_AT": "off"}) is None and R.cleanup_at({"CLEANUP_AT": "junk"}) == (2, 0)


def test_due_once_a_day_after_the_time():
    at = (2, 0)
    assert R.due(_at("2026-11-16", "01:59"), at, TZ, "2026-11-15") is None
    assert R.due(_at("2026-11-16", "02:00"), at, TZ, "2026-11-15") == "2026-11-16"
    assert R.due(_at("2026-11-16", "09:00"), at, TZ, "2026-11-16") is None        # already done today
    assert R.due(_at("2026-11-16", "09:00"), at, TZ, None) == "2026-11-16"        # scheduler was down at 02:00
    assert R.due(_at("2026-11-16", "09:00"), None, TZ, None) is None              # off


def _ticket(state_path, tid, state, age_days, now, records=None, schedule=None):
    tdir = state_path / "tickets"
    tdir.mkdir(parents=True, exist_ok=True)
    t = {"id": tid, "state": state, "devices": [{"device": "D1", "baseline_record": str(records) if records else None}]}
    if schedule:
        t["schedule"] = schedule
    p = tdir / f"{tid}.json"
    p.write_text(json.dumps(t))
    os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    return p


def test_clean_removes_only_old_finished_tickets(tmp_path):
    now = _at("2026-11-16", "02:00")
    st, recs = tmp_path / "state", tmp_path / "records"
    old_rec = recs / "20260801-0000-aaaa"
    old_rec.mkdir(parents=True)
    (old_rec / "D1.txt").write_text("running config")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    old = _ticket(st, "C20260801-aaaaaa", "applied", 100, now, old_rec)
    odd = _ticket(st, "C20260801-bbbbbb", "failed", 100, now, outside)           # record outside: kept
    young = _ticket(st, "C20261101-cccccc", "applied", 10, now)
    waiting = _ticket(st, "C20260801-dddddd", "waiting_approval", 100, now)
    sched = _ticket(st, "C20260801-eeeeee", "cancelled", 100, now, schedule={"state": "notified"})
    seen = []
    res = R.clean(st, recs, 90, now=now, audit=lambda t, what: seen.append((t["id"], what)))
    assert sorted(res["removed"]) == ["C20260801-aaaaaa", "C20260801-bbbbbb"] and res["records"] == 1
    assert not old.exists() and not odd.exists() and not old_rec.exists() and outside.exists()
    assert young.exists() and waiting.exists() and sched.exists()
    assert all("retention" in w for _, w in seen)


def test_daily_marker(tmp_path):
    d = R.Daily(tmp_path)
    assert d.last() is None
    d.done("2026-11-16", {"removed": ["x"], "records": 0, "days": 90})
    assert d.last() == "2026-11-16"


def test_scheduler_runs_the_cleanup_once(tmp_path):
    from types import SimpleNamespace
    from rudder_devices.schedule import Scheduler
    st = tmp_path / "state"
    now = _at("2026-11-16", "02:01")
    _ticket(st, "C20260801-aaaaaa", "applied", 100, now)
    audit = []

    class C:
        s = SimpleNamespace(approval={"PAGES_KEEP_DAYS": 90, "CLEANUP_AT": "02:00"}, state_path=st,
                            records_path=tmp_path / "records", consumer="NAVI", schedule={"TIMEZONE": TZ})
        tdir = st / "tickets"

        def locked(self):
            import contextlib
            return contextlib.nullcontext()

        def _audit(self, t, what):
            audit.append(what)

    sch = Scheduler(C(), clock=lambda: now)
    ev = sch.tick()
    assert any("1 ticket(s) older than 90 days removed" in e for e in ev)
    assert sch.tick() == [] and len(audit) == 1


def test_setup_checks_the_new_keys(monkeypatch):
    from navigator_engineer import setup as S
    monkeypatch.setattr(S, "hermes_home", lambda: __import__("pathlib").Path("/nonexistent"))
    base = {"METHOD": "telegram", "BOT": "default"}
    out = S.normalize_approval({**base, "PAGES_KEEP_DAYS": "30", "CLEANUP_AT": "3:05"})
    assert out["PAGES_KEEP_DAYS"] == 30 and out["CLEANUP_AT"] == "03:05"
    assert S.normalize_approval(base)["PAGES_KEEP_DAYS"] == 90
    assert S.normalize_approval({**base, "CLEANUP_AT": "off"})["CLEANUP_AT"] == "off"
    for bad in ({"PAGES_KEEP_DAYS": 0}, {"PAGES_KEEP_DAYS": "x"}, {"CLEANUP_AT": "25:00"}):
        with pytest.raises(S.SetupError):
            S.normalize_approval({**base, **bad})
