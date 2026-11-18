"""P7e (2026-11-15 night): one result per ticket in the chat, refused lines are never dropped silently."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from rudder_devices.core import RudderError
from test_p3 import rig  # noqa: F401
from test_p4 import FakeSwitch, ch, propose  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "navigator_reply", ROOT / "integrations" / "hermes" / "navigator-reply" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ bug 1: fake results in the model's reply
FAKE = """**Proposing**

The shutdown for Loopback98 has been staged in ticket **C20261115-9b45fd**.

Approve it with `/pushok` on your approval bot; the result is sent there and in this thread.
✅ Change C20261115-9b45fd: applied
Shutdown Loopback98 and update Loopback99 description to Hello Moto on QALEAF1A
QALEAF1A: applied
✅ Change C20261115-9b45fd: applied
Shutdown Loopback98 and update Loopback99 description to Hello Moto on QALEAF1A
QALEAF1A: applied"""


def test_fake_results_are_removed():
    p = _plugin()
    out = p.strip_fake_results(FAKE)
    assert out is not None
    assert "✅" not in out and "QALEAF1A: applied" not in out
    assert "staged in ticket" in out and out.rstrip().endswith("in this thread.")


@pytest.mark.parametrize("head", ["❌ Change C20261115-abcdef: failed", "⏭ Change C20261115-abcdef was NOT applied",
                                  "🛑 Change C20261115-abcdef stopped.", "✅ Scheduled change C20261115-abcdef: applied",
                                  "🔐 Change C20261115-abcdef needs approval", "ℹ️ Change C20261115-abcdef by SHIP"])
def test_every_rudder_header_is_removed(head):
    out = _plugin().strip_fake_results(f"Ticket C20261115-abcdef is waiting.\n\n{head}\nmore\n\n- next bullet")
    assert head not in out and "more" not in out
    assert "Ticket C20261115-abcdef is waiting." in out and "- next bullet" in out


def test_normal_replies_untouched():
    p = _plugin()
    assert p.strip_fake_results("Ticket C20261115-abcdef is applied, see the result above.") is None
    assert p.strip_fake_results("") is None
    assert p.on_transform_llm_output(response_text="all good ✅") is None


def test_reply_that_is_only_a_fake_result():
    out = _plugin().on_transform_llm_output(response_text="✅ Change C20261115-abcdef: applied\nX: applied")
    assert "RUDDER posts the result" in out


def test_plugin_registers_its_hooks():
    got = []
    _plugin().register(type("Ctx", (), {"register_hook": lambda self, n, f: got.append(n)})())
    assert got == ["pre_tool_call", "transform_llm_output", "pre_gateway_dispatch", "pre_api_request",
                   "post_tool_call"]


def test_scheduler_sends_one_result_per_ticket(ch):  # noqa: F811
    """One ticket with several items: one message to the approval channel, one reply to the chat."""
    from rudder_devices.schedule import Scheduler
    from test_p4 import _decide
    ch["chan"].reply_profiles = {"netclaw"}
    r = propose(ch, lines=["interface Loopback10", "description a", "interface Loopback11", "description b"],
                reply_to="netclaw/slack:C0CFU074Z7U:1794771577.473219")
    _decide(ch)
    before = len(ch["sent"])
    sch = Scheduler(ch["c"])
    sch.tick()
    sch.tick()                                                # a second pass must not repeat it
    msgs = ch["sent"][before:]
    assert len(msgs) == 2, msgs
    assert msgs[0]["argv"][2] == "navi" and msgs[1]["argv"][2] == "netclaw"
    assert all(f"Change {r['ticket']}: applied" in m["message"] for m in msgs)


# ------------------------------------------------------------------ bug 2: a refused line dropped silently
class RefusingSwitch(FakeSwitch):
    refuse = ("ip ospf network point-to-point",)

    def __call__(self, req):
        body = json.loads(req.content)
        cmds = body["params"]["cmds"]
        for i, c in enumerate(cmds):
            if c.strip() in self.refuse:
                return self._err(f"CLI command {i + 1} of {len(cmds)} '{c.strip()}' failed: invalid command", 1002)
        return super().__call__(req)


OSPF = ["interface Loopback10", "description Hello Moto", "ip ospf network point-to-point"]


def test_refused_line_is_flagged_when_left_out(ch):  # noqa: F811
    ch["sw"].__class__ = RefusingSwitch
    propose(ch, lines=OSPF)
    r = propose(ch, lines=OSPF[:2], description="Loopback10 description")
    assert [x["line"] for x in r["left_out"]] == ["ip ospf network point-to-point"]
    assert r["next"].startswith("Tell the user these requested lines are NOT in this change")
    msg = ch["sent"][-1]["message"]
    assert "NOT in this change" in msg and "ip ospf network point-to-point" in msg
    page = __import__("rudder_devices.web", fromlist=["Pages"]).Pages(ch["s"].state_path).render(
        ch["c"]._load(r["ticket"]))
    assert "NOT in this change" in page and "ip ospf network point-to-point" in page


def test_refused_line_is_a_failed_change_needing_attention(ch):  # noqa: F811
    ch["sw"].__class__ = RefusingSwitch
    ch["chan"].reply_profiles = {"netclaw"}
    ch["s"].approval.update(WEB_BIND="127.0.0.1:8790")
    before = len(ch["sent"])
    r = propose(ch, lines=OSPF, reply_to="netclaw/slack:C0CFU074Z7U:1794771577.473219",
                description="Loopback10 description and OSPF point-to-point")
    assert r["state"] == "failed" and r["attention"] is True
    assert r["failure"]["line"] == "ip ospf network point-to-point" and r["failure"]["device"] == "QALEAF1A"
    assert r["failure"]["alerted"] == ["approval channel", "chat thread"]
    assert "INVESTIGATE" in r["next"] and "change action=investigate" in r["next"]
    msgs = ch["sent"][before:]
    assert len(msgs) == 2 and all("FAILED: needs attention" in m["message"] for m in msgs)
    assert msgs[0]["argv"][2] == "navi" and msgs[1]["argv"][2] == "netclaw"
    assert all("/pushok" not in m["message"] for m in msgs)               # no approval was opened
    assert not list(ch["dir"].glob("*")) or all(p.name == "decisions.log" for p in ch["dir"].iterdir())
    assert not ch["c"]._lock_path("QALEAF1A").exists()
    assert "page" in r and ch["c"]._load(r["ticket"])["devices"][0]["state"] == "refused"
    from rudder_devices.web import Pages
    page = Pages(ch["s"].state_path).render(ch["c"]._load(r["ticket"]))
    assert "FAILED: needs human attention" in page and "in progress" in page
    # the investigation is recorded, shown and sent to both channels
    r2 = ch["c"].investigation(r["ticket"], "EOS accepts the OSPF network type only on Ethernet, Port-Channel "
                               "and VLAN interfaces, not on a Loopback", ["interface Ethernet1",
                                                                          "ip ospf network point-to-point"])
    assert r2["failure"]["investigation"] == "done" and "Tell the user the finding" in r2["next"]
    assert all("🔎" in m["message"] for m in ch["sent"][-2:])
    page = Pages(ch["s"].state_path).render(ch["c"]._load(r["ticket"]))
    assert "Lines that work instead" in page and "interface Ethernet1" in page
    assert any("investigation:" in ln for ln in (ch["s"].state_path / "audit.log").read_text().splitlines())
    with pytest.raises(RudderError):
        ch["c"].investigation(propose(ch, lines=["interface Loopback11"])["ticket"], "x")


def test_connection_errors_still_raise(ch, monkeypatch):  # noqa: F811
    from rudder_devices import adapters

    def boom(*a, **k):
        raise adapters.DeviceError("unreachable", "unreachable: connection timed out")
    monkeypatch.setattr(adapters.EosEapiWriter, "dry_run", boom)
    with pytest.raises(RudderError, match="unreachable"):
        propose(ch)


def test_no_left_out_for_other_devices_or_when_kept(ch):  # noqa: F811
    c = ch["c"]
    c._refused_note(["OTHERDEV"], OSPF, "CLI command 3 of 3 'ip ospf network point-to-point' failed: x")
    assert c._left_out(["QALEAF1A"], OSPF[:2]) == []
    c._refused_note(["QALEAF1A"], OSPF, "CLI command 3 of 3 'ip ospf network point-to-point' failed: x")
    assert c._left_out(["qaleaf1a"], OSPF) == []                 # kept: nothing left out
    assert len(c._left_out(["QALEAF1A"], OSPF[:2])) == 1
    c._refused_note(["QALEAF1A"], OSPF, "some other error")       # no line named: nothing recorded
    assert len(json.loads((ch["s"].state_path / "refused.json").read_text())) == 2


def test_interrupted_apply_is_marked_once(ch, monkeypatch):  # noqa: F811
    import os
    from rudder_devices import changes
    from rudder_devices.schedule import Scheduler
    from test_p4 import _decide
    c = ch["c"]
    r = propose(ch)
    _decide(ch)
    c.status(r["ticket"])
    with c.locked():                                   # what a crash in the middle of apply leaves behind
        t = c._load(r["ticket"])
        t["state"] = "applying"
        t["devices"][0]["state"] = "committed_pending_confirm"
        c._save(t)
    assert c.recover_interrupted() == []               # still fresh: maybe really applying
    p = c._path(r["ticket"])
    old = changes._now() - changes.INTERRUPTED_SEC - 5
    os.utime(p, (old, old))
    before = len(ch["sent"])
    ev = Scheduler(c).tick()
    assert f"{r['ticket']}: interrupted" in ev
    st = c.status(r["ticket"])
    assert st["state"] == "interrupted" and st["devices"][0]["state"] == "check_device"
    assert "check the devices" in st["next"]
    assert "INTERRUPTED" in ch["sent"][before]["message"]
    assert not c._lock_path("QALEAF1A").exists()
    assert Scheduler(c).tick() == []                   # reported once


def test_atomic_state_writes(tmp_path):
    from navigator_engineer.config import atomic_write
    p = tmp_path / "NAVIGATOR_STATE.json"
    atomic_write(p, "{}", mode=0o600)
    assert p.read_text() == "{}" and oct(p.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("head", ["⚠️ Change C20261115-abcdef was INTERRUPTED: x"])
def test_interrupted_header_is_rudder_only(head):
    assert "INTERRUPTED" not in _plugin().strip_fake_results(f"ok\n\n{head}\n")


def test_refused_memory_expires(ch, monkeypatch):  # noqa: F811
    from rudder_devices import changes
    c = ch["c"]
    c._refused_note(["QALEAF1A"], OSPF, "CLI command 3 of 3 'ip ospf network point-to-point' failed: x")
    now = changes._now()
    monkeypatch.setattr(changes, "_now", lambda: now + changes.REFUSED_MIN * 60 + 5)
    assert c._left_out(["QALEAF1A"], OSPF[:2]) == []
