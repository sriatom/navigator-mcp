"""Scheduled changes: approve in advance, notice with a stop code, default yes, apply by the runner."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client

from rudder_devices import approval
from rudder_devices.core import RudderError
from rudder_devices.schedule import DEFAULTS, Scheduler, check_window, parse_at
from test_p3 import rig  # noqa: F401
from test_p4 import LINES, ch, propose  # noqa: F401

NY = ZoneInfo("America/New_York")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, NY).isoformat(timespec="seconds")


def _code(ch, i=-1):  # noqa: F811
    return re.search(r"/push(?:ok|no) (\w+)", ch["sent"][i]["message"]).group(1)


def _decide(ch, verdict="approved", i=-1):  # noqa: F811
    code = _code(ch, i)
    pending = ch["dir"] / approval.code_hash("pending", code)
    assert pending.exists()
    (ch["dir"] / approval.code_hash("decision", code)).write_text(verdict)
    pending.unlink()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def scheduled(ch, ahead=120, **kw):  # noqa: F811
    at = time.time() + ahead
    st = propose(ch, at=_iso(at), **kw)
    return st["ticket"], round(at)


# ------------------------------------------------------------------------------- time parsing
def test_parse_formats():
    cfg = dict(DEFAULTS)
    now = dt.datetime(2026, 11, 12, 15, 0, tzinfo=NY).timestamp()
    e, z = parse_at("02:00", None, cfg, now)
    assert z == "America/New_York" and dt.datetime.fromtimestamp(e, NY) == dt.datetime(2026, 11, 13, 2, 0, tzinfo=NY)
    e, _ = parse_at("16:30", None, cfg, now)                    # later today
    assert dt.datetime.fromtimestamp(e, NY).day == 12
    e, z = parse_at("2026-11-13 02:00 EST", None, cfg, now)
    assert z == "America/New_York" and dt.datetime.fromtimestamp(e, NY).hour == 2
    e, z = parse_at("2026-11-13 07:00", "UTC", cfg, now)
    assert dt.datetime.fromtimestamp(e, NY).hour == 2
    e, _ = parse_at("2026-11-13T02:00:00-05:00", None, cfg, now)
    assert dt.datetime.fromtimestamp(e, NY).hour == 2
    with pytest.raises(RudderError, match="time zone"):
        parse_at("02:00", "Mars/Base", cfg, now)
    with pytest.raises(RudderError, match="cannot read"):
        parse_at("tomorrow-ish", None, cfg, now)
    with pytest.raises(RudderError, match="at least"):
        check_window(now + 20, 30, cfg, now)
    with pytest.raises(RudderError, match="days ahead"):
        check_window(now + 8 * 86400, 30, cfg, now)


# ------------------------------------------------------------------------------- the main flow
def test_default_yes_flow(ch):  # noqa: F811
    tid, at = scheduled(ch, verify_commands=["show interfaces Loopback10"],
                        expect={"show interfaces Loopback10": ["rudder-test"]})
    msg = ch["sent"][-1]["message"]
    assert "SCHEDULED for" in msg and "/pushok" in msg
    st = ch["c"].status(tid)
    assert st["schedule"]["state"] == "scheduled" and "Scheduled for" in st["next"]
    _decide(ch)
    assert ch["c"].status(tid)["state"] == "approved"
    with pytest.raises(RudderError, match="scheduler applies it"):
        ch["c"].apply(tid)                                   # the agent cannot run it early
    clock = Clock(at - 60)
    sch = Scheduler(ch["c"], clock=clock)
    assert sch.tick() == []                                  # too early: nothing
    clock.t = at - 30
    ev = sch.tick()
    assert "notice sent" in ev[0]
    notice = ch["sent"][-1]["message"]
    assert "To STOP it: /pushno" in notice and "No answer = it runs" in notice
    clock.t = at - 10
    assert sch.tick() == []                                  # waiting out the notice
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]
    clock.t = at + 1
    ev = sch.tick()
    assert ev == [f"{tid}: applied"]
    assert "interface Loopback10" in ch["sw"].running
    assert ch["sent"][-1]["message"].startswith(f"✅ Scheduled change {tid}: applied")
    st = ch["c"].status(tid)
    assert st["state"] == "applied" and st["schedule"]["state"] == "done"
    assert list(ch["dir"].iterdir()) == []                   # both codes used up
    assert sch.tick() == []                                  # done: not picked up again
    audit = (ch["s"].state_path / "audit.log").read_text()
    assert "scheduled" in audit and "apply started (scheduler)" in audit


def test_stop_from_notice(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    sch = Scheduler(ch["c"], clock=Clock(at - 30))
    sch.tick()
    _decide(ch, "rejected")                                  # /pushno CODE on the notice
    sch.clock = Clock(at - 20)
    assert sch.tick() == [f"{tid}: stopped by the user"]
    assert ch["sent"][-1]["message"].startswith(f"🛑 Change {tid} stopped")
    st = ch["c"].status(tid)
    assert st["state"] == "cancelled" and st["schedule"]["state"] == "cancelled"
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]
    assert not list((ch["s"].state_path / "locks").iterdir()) and list(ch["dir"].iterdir()) == []


def test_start_now_from_notice(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    sch = Scheduler(ch["c"], clock=Clock(at - 30))
    sch.tick()
    _decide(ch, "approved")                                  # /pushok CODE on the notice
    sch.clock = Clock(at - 25)
    assert sch.tick() == [f"{tid}: applied"]


def test_not_approved_in_time_is_missed(ch):  # noqa: F811
    tid, at = scheduled(ch)
    sch = Scheduler(ch["c"], clock=Clock(at - 30))
    ev = sch.tick()
    assert "missed (not approved" in ev[0]
    assert "NOT applied" in ch["sent"][-1]["message"]
    st = ch["c"].status(tid)
    assert st["state"] == "cancelled" and st["schedule"]["state"] == "missed"
    assert not list((ch["s"].state_path / "locks").iterdir())


def test_scheduler_too_late_is_missed(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    ev = Scheduler(ch["c"], clock=Clock(at + 11 * 60)).tick()
    assert "missed" in ev[0] and "late" in ev[0]
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]


def test_notice_send_failure_retries(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    good = ch["chan"].run
    ch["chan"].run = lambda *a, **k: type("R", (), {"returncode": 1})()
    sch = Scheduler(ch["c"], clock=Clock(at - 30))
    assert "notice not sent yet" in sch.tick()[0]
    assert ch["c"].status(tid)["schedule"]["state"] == "scheduled"
    ch["chan"].run = good
    assert "notice sent" in sch.tick()[0]


def test_reschedule_needs_new_approval(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    assert ch["c"].status(tid)["state"] == "approved"
    st = ch["c"].reschedule(tid, _iso(at + 600))
    assert st["state"] == "waiting_approval"
    assert "SCHEDULED for" in ch["sent"][-1]["message"]
    assert len(list(ch["dir"].iterdir())) == 1               # only the new pending code
    sch = Scheduler(ch["c"], clock=Clock(at - 30))
    assert sch.tick() == []                                   # the old time no longer counts


def test_unscheduled_ticket_can_be_scheduled(ch):  # noqa: F811
    tid = propose(ch)["ticket"]
    _decide(ch)
    st = ch["c"].reschedule(tid, _iso(time.time() + 300))
    assert st["state"] == "waiting_approval" and st["schedule"]["state"] == "scheduled"
    with pytest.raises(RudderError, match="scheduler applies it"):
        ch["c"].apply(tid)


def test_moved_time_after_approval_is_refused(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    p = ch["s"].state_path / "tickets" / f"{tid}.json"
    t = json.loads(p.read_text())
    t["schedule"]["at"] = at - 60                            # someone edits the file to run it earlier
    p.write_text(json.dumps(t))
    sch = Scheduler(ch["c"], clock=Clock(at - 90))
    sch.tick()
    _decide(ch, "approved")
    sch.clock = Clock(at - 59)
    ev = sch.tick()
    assert ev == [f"{tid}: failed"] and "altered" in ch["sent"][-1]["message"]
    assert "interface Loopback10" not in ch["sw"].running


def test_cancel_scheduled(ch):  # noqa: F811
    tid, at = scheduled(ch)
    _decide(ch)
    st = ch["c"].cancel(tid)
    assert st["state"] == "cancelled" and st["schedule"]["state"] == "cancelled"
    assert Scheduler(ch["c"], clock=Clock(at)).tick() == []


def test_list_shows_time(ch):  # noqa: F811
    tid, _ = scheduled(ch)
    row = next(r for r in ch["c"].list() if r["ticket"] == tid)
    assert row["schedule"] == "scheduled" and row["at"]


# ------------------------------------------------------------------- apply right after /pushok
def test_auto_apply_after_approval(ch):  # noqa: F811
    tid = propose(ch)["ticket"]
    assert "applies it by itself" in ch["c"].status(tid)["next"]
    sch = Scheduler(ch["c"])
    assert sch.tick() == []                                  # waiting for the /pushok: nothing
    _decide(ch)                                              # /pushok CODE on the approval bot
    assert sch.tick() == [f"{tid}: applied (after approval)"]
    assert "interface Loopback10" in ch["sw"].running
    assert ch["sent"][-1]["message"].startswith(f"✅ Change {tid}: applied")
    st = ch["c"].status(tid)
    assert st["state"] == "applied" and st["next"] == "Applied and verified."
    assert sch.tick() == []                                  # done: not picked up again
    assert not list((ch["s"].state_path / "locks").iterdir()) and list(ch["dir"].iterdir()) == []


def test_auto_apply_rejected_is_not_applied(ch):  # noqa: F811
    tid = propose(ch)["ticket"]
    _decide(ch, "rejected")
    assert Scheduler(ch["c"]).tick() == []
    assert ch["c"].status(tid)["state"] == "rejected"
    assert "interface Loopback10" not in ch["sw"].running


def test_agent_apply_first_wins(ch):  # noqa: F811
    tid = propose(ch)["ticket"]
    _decide(ch)
    assert ch["c"].apply(tid)["state"] == "applied"          # the agent's change_apply ran first
    sent = len(ch["sent"])
    assert Scheduler(ch["c"]).tick() == []                   # nothing applied twice, nothing sent
    assert len(ch["sent"]) == sent


def test_auto_apply_refused_is_cancelled_once(ch):  # noqa: F811
    tid = propose(ch)["ticket"]
    p = ch["s"].state_path / "tickets" / f"{tid}.json"
    t = json.loads(p.read_text())
    t["lines"] = ["interface Loopback11"]                    # someone edits the file after the request
    p.write_text(json.dumps(t))
    _decide(ch)
    sch = Scheduler(ch["c"])
    assert sch.tick() == [f"{tid}: failed (after approval)"]
    assert "altered" in ch["sent"][-1]["message"]
    assert ch["c"].status(tid)["state"] == "cancelled"
    assert sch.tick() == []                                  # not retried every poll
    assert "interface Loopback11" not in ch["sw"].running


def test_auto_apply_off(ch, monkeypatch):  # noqa: F811
    import dataclasses
    monkeypatch.setattr(ch["c"], "s", dataclasses.replace(ch["c"].s, schedule={"AUTO_APPLY": "false"}))
    tid = propose(ch)["ticket"]
    _decide(ch)
    assert Scheduler(ch["c"]).tick() == []
    st = ch["c"].status(tid)
    assert st["state"] == "approved" and "Apply with change_apply" in st["next"]


# ------------------------------------------------------------- result back in the originating chat
REPLY = "chatbot/slack:C0123ABCD:1794700444.511999"


def test_result_posted_to_reply_to(ch):  # noqa: F811
    ch["chan"].reply_profiles = {"chatbot"}
    tid = propose(ch, reply_to=REPLY)["ticket"]
    _decide(ch)
    assert Scheduler(ch["c"]).tick() == [f"{tid}: applied (after approval)"]
    approver, chat = ch["sent"][-2], ch["sent"][-1]
    assert approver["argv"][-4:] == ["send", "-t", "telegram", "-f", "-"][-4:] and approver["argv"][2] == "navi"
    assert chat["argv"][1:6] == ["-p", "chatbot", "send", "-t", "slack:C0123ABCD:1794700444.511999"]
    assert chat["message"] == approver["message"] and chat["message"].startswith(f"✅ Change {tid}: applied")
    assert "/push" not in chat["message"]                    # a reply never carries a code


def test_reply_to_needs_an_allowed_profile(ch):  # noqa: F811
    tid = propose(ch, reply_to=REPLY)["ticket"]               # REPLY_PROFILES empty: off
    assert "reply_to" not in ch["c"]._load(tid)
    ch["chan"].reply_profiles = {"chatbot"}
    for bad in ("other/slack:C1:2", "chatbot/email:x@y", "chatbot/slack:C1;rm -rf", "chatbot/slack:C1:2:3"):
        ch["c"].cancel(tid)
        tid = propose(ch, reply_to=bad)["ticket"]
        assert "reply_to" not in ch["c"]._load(tid), bad


def test_missed_scheduled_change_is_posted_to_reply_to(ch):  # noqa: F811
    ch["chan"].reply_profiles = {"chatbot"}
    tid, at = scheduled(ch, reply_to=REPLY)
    Scheduler(ch["c"], clock=Clock(at - 30)).tick()
    assert ch["sent"][-1]["argv"][2] == "chatbot" and "NOT applied" in ch["sent"][-1]["message"]


def test_reply_failure_does_not_stop_the_result(ch):  # noqa: F811
    ch["chan"].reply_profiles = {"chatbot"}
    tid = propose(ch, reply_to=REPLY)["ticket"]
    _decide(ch)
    good = ch["chan"].run

    def flaky(argv, **kw):
        if argv[2] == "chatbot":
            return type("R", (), {"returncode": 1})()
        return good(argv, **kw)

    ch["chan"].run = flaky
    assert Scheduler(ch["c"]).tick() == [f"{tid}: applied (after approval)"]
    assert ch["sent"][-1]["message"].startswith(f"✅ Change {tid}: applied")   # approver still told


def test_local_override_merges_sections(tmp_path, monkeypatch):
    from rudder_devices import config
    base = tmp_path / "rudder.yml"
    base.write_text("CONSUMER: NAVI\nAPPROVAL:\n  CHANNEL: hermes\n  TTL_MIN: 30\n")
    local = tmp_path / "rudder.local.yml"
    local.write_text("APPROVAL:\n  REPLY_PROFILES: [chatbot]\n")
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(base))
    monkeypatch.delenv("RUDDER_LOCAL", raising=False)
    assert "REPLY_PROFILES" not in config.load().approval      # a test bootstrap ignores the home file
    monkeypatch.setenv("RUDDER_LOCAL", str(local))
    assert config.load().approval == {"CHANNEL": "hermes", "TTL_MIN": 30, "REPLY_PROFILES": ["chatbot"]}


async def test_schedule_tool_listed(rig, monkeypatch, tmp_path):  # noqa: F811
    from rudder_devices.server import mcp
    boot = tmp_path / "rudder.yml"
    boot.write_text(f"ATLAS_SOCKET: {rig['sock']}\nTOKEN_FILE: {rig['tok']}\nSTATE_PATH: {tmp_path / 'st'}\n")
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(boot))
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert "change_schedule" in tools
    assert "at" in tools["change_propose"].inputSchema["properties"]
