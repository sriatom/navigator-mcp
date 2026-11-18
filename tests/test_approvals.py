"""Approval methods (telegram / slack), approvers, SHIP engineer bypass, ticket search, project ticket IDs,
and navigator-setup."""
from __future__ import annotations

import dataclasses
import json

import pytest

from rudder_devices import approval
from rudder_devices.core import RudderError
from rudder_devices.schedule import Scheduler
from test_p3 import rig  # noqa: F401
from test_p4 import ch, propose  # noqa: F401
from test_schedule import _decide


def _chan(cfg, sent):
    def runner(argv, input=None, **kw):
        sent.append({"argv": argv, "message": input})
        return type("R", (), {"returncode": 0})()
    return approval.HermesChannel(cfg, "NAVI", runner=runner)


# ------------------------------------------------------------------------------- the channel
def test_slack_method_posts_to_the_channel_with_bang_commands(tmp_path):
    sent = []
    c = _chan({"METHOD": "slack", "BOT": "navi", "CHANNEL": "C0123ABCDEF", "APPROVAL_DIR": str(tmp_path)}, sent)
    h = c.request("RUDDER X", lambda code: f"Approve: /pushok {code}\nReject:  /pushno {code}", requester="U1")
    assert sent[0]["argv"][1:6] == ["-p", "navi", "send", "-t", "slack:C0123ABCDEF"]
    assert "`!pushok " in sent[0]["message"] and "`!pushno " in sent[0]["message"] and "/push" not in sent[0]["message"]
    rules = json.loads((tmp_path / h["pending"]).read_text())
    assert rules == {"label": "RUDDER X", "method": "slack", "channel": "C0123ABCDEF", "approvers": "*",
                     "requester": "U1", "self_approve": True}
    assert "Slack C0123ABCDEF" in h["where"]


def test_method_settings_are_checked():
    with pytest.raises(approval.ApprovalError, match="needs APPROVAL.CHANNEL"):
        approval.HermesChannel({"METHOD": "slack", "BOT": "navi"}, "NAVI")
    with pytest.raises(approval.ApprovalError, match="METHOD must be"):
        approval.HermesChannel({"METHOD": "email"}, "NAVI")
    old = approval.HermesChannel({"CHANNEL": "hermes", "PROFILES": {"NAVI": "navi"}}, "NAVI")   # the old keys
    assert (old.method, old.target, old.profile, old.prefix) == ("telegram", "telegram", "navi", "/")
    assert approval.approvers({}) == "*" and approval.approvers({"APPROVERS": "U1, U2"}) == ["U1", "U2"]
    assert approval.approvers({"APPROVERS": ["U1", "*"]}) == "*"


def test_who_decided_is_read_from_the_decision(tmp_path):
    c = _chan({"APPROVAL_DIR": str(tmp_path)}, [])
    h = c.request("x", lambda code: code)
    (tmp_path / h["decision"]).write_text("approved\nPat (U0123ABCDEF) on slack")
    (tmp_path / h["pending"]).unlink()
    assert c.state(h) == "approved" and c.who(h) == "Pat (U0123ABCDEF) on slack"
    assert c.state({}) == "gone"
    c.close({})                                                # tolerated


def test_decided_by_lands_on_the_ticket(ch):  # noqa: F811
    tid = propose(ch, requested_by="U0123ABCDEF|Pat")["ticket"]
    code_msg = ch["sent"][-1]["message"]
    assert "Requested by Pat (U0123ABCDEF) via NAVI" in code_msg
    import re
    code = re.search(r"/pushok (\w+)", code_msg).group(1)
    (ch["dir"] / approval.code_hash("decision", code)).write_text("approved\nBoss (U9) on slack")
    (ch["dir"] / approval.code_hash("pending", code)).unlink()
    st = ch["c"].status(tid)
    assert st["state"] == "approved" and st["decided_by"] == "Boss (U9) on slack"
    assert st["requested_by"] == "Pat (U0123ABCDEF)"
    assert "approved by Boss (U9) on slack" in (ch["s"].state_path / "audit.log").read_text()


# ------------------------------------------------------------------------------- SHIP engineer bypass
def _ship(ch, monkeypatch, labs=("FAB",)):  # noqa: F811
    monkeypatch.setattr(ch["c"], "s", dataclasses.replace(
        ch["c"].s, approval={**(ch["c"].s.approval or {}), "SHIP_ENGINEERS": ["U0123ABCDEF"]},
        lab_inventories=tuple(labs)))


def test_ship_engineer_lab_change_needs_no_pushok(ch, monkeypatch):  # noqa: F811
    _ship(ch, monkeypatch)
    st = propose(ch, requested_by="U0123ABCDEF|Pat")
    assert st["state"] == "approved" and "none needed" in st["approval"]
    assert not ch["dir"].exists() or list(ch["dir"].iterdir()) == []   # no code was opened
    assert "runs WITHOUT approval" in ch["sent"][-1]["message"]   # the approval channel is told
    assert Scheduler(ch["c"]).tick() == [f"{st['ticket']}: applied (after approval)"]
    assert "interface Loopback10" in ch["sw"].running
    audit = (ch["s"].state_path / "audit.log").read_text()
    assert "approved without /pushok: SHIP engineer Pat (U0123ABCDEF)" in audit


@pytest.mark.parametrize("case", ["other person", "not a lab", "scheduled", "no requester"])
def test_ship_bypass_only_when_every_rule_holds(ch, monkeypatch, case):  # noqa: F811
    _ship(ch, monkeypatch, labs=() if case == "not a lab" else ("FAB",))
    kw = {"requested_by": "U0123ABCDEF|Pat"}
    if case == "other person":
        kw["requested_by"] = "U12345678|Someone"
    if case == "no requester":
        kw = {}
    if case == "scheduled":
        import time
        from test_schedule import _iso
        kw["at"] = _iso(time.time() + 300)
    st = propose(ch, **kw)
    assert st["state"] == "waiting_approval"
    assert len(list(ch["dir"].iterdir())) == 1                   # a code is waiting for /pushok


# ------------------------------------------------------------------------------- ticket search
def test_ticket_search_filters(ch):  # noqa: F811
    a = propose(ch, requested_by="U0123ABCDEF|Pat", project="LAB1", ticket_id="CHG0012345")["ticket"]
    ch["c"].cancel(a)
    b = propose(ch, requested_by="U7777777|Ana")["ticket"]
    L = ch["c"].list
    assert [r["ticket"] for r in L()] == [b, a]                   # newest first
    assert [r["ticket"] for r in L(requested_by="pat")] == [a]
    assert [r["ticket"] for r in L(requested_by="U7777777")] == [b]
    assert [r["ticket"] for r in L(state="cancelled")] == [a]
    assert [r["ticket"] for r in L(ticket_id="chg0012345")] == [a]
    assert [r["ticket"] for r in L(project="LAB1")] == [a]
    assert len(L(device="qaleaf1a")) == 2 and L(device="OTHER") == []
    assert len(L(inventory="INVENTORY_FAB")) == 2 and L(inventory="X") == []
    assert len(L(platform="EOS")) == 2 and L(platform="PANOS") == []
    import time
    today = time.strftime("%Y-%m-%d")
    assert len(L(since=today, until=today)) == 2 and L(until="2000-01-01") == []
    assert len(L(limit=1)) == 1
    row = L(ticket_id="CHG0012345")[0]
    assert row["requested_by"] == "Pat (U0123ABCDEF)" and row["project"] == "LAB1" and row["created"].startswith(today)
    with pytest.raises(RudderError, match="cannot read the date"):
        L(since="yesterday")
    with pytest.raises(RudderError, match="ticket_id"):
        propose(ch, ticket_id="bad id;")


def test_requested_by_is_cleaned():
    from rudder_devices.changes import _person
    assert _person("U0123ABCDEF|Pat") == {"id": "U0123ABCDEF", "name": "Pat"}
    assert _person("Pat") == {"name": "Pat"} and _person("U0123ABCDEF") == {"id": "U0123ABCDEF"}
    dirty = json.dumps(_person("U1;rm -rf /|x"))
    assert ";" not in dirty and "/" not in dirty
    assert _person(None) == {}


# ------------------------------------------------------------------------------- project ticket ID
def test_project_ticket_id(tmp_path):
    from navigator_engineer.config import Settings
    from navigator_engineer.projects import ProjectError, Projects
    P = Projects(Settings(workspace=tmp_path))
    out = P.create("OPEN", "check bgp", "CHG_TEST", None, "CHG0012345")
    assert out["ticket_id"] == "CHG0012345"
    assert P.list()[0]["ticket_id"] == "CHG0012345"
    again = P.create("OPEN", "check bgp", "CHG_TEST", None, "CHG999")
    assert again["ticket_id"] == "CHG0012345" and "not changed" in again["notes"][0]
    assert P.create("OPEN", "no outside ticket", "PLAIN")["ticket_id"] is None
    with pytest.raises(ProjectError, match="ticket_id"):
        P.create("OPEN", "x", "BAD", None, "no spaces allowed")


# ------------------------------------------------------------------------------- navigator-setup
@pytest.fixture
def host(tmp_path, monkeypatch):
    nav = tmp_path / "navigator.yml"
    nav.write_text("WORKSPACE: /old   # project folders\nCONSUMER: NAVI\nSTATE_PATH: %s\n"
                   "APPROVAL:\n  CHANNEL: hermes\n  PROFILES: {NAVI: navi}\n" % (tmp_path / "state"))
    rboot = tmp_path / "rudder.yml"
    rboot.write_text(f"APPROVAL:\n  CHANNEL: hermes\n  APPROVAL_DIR: {tmp_path / 'appr'}\n  "
                     f"PROFILES: {{NAVI: navi}}\n  TTL_MIN: 30\n")
    local = tmp_path / "rudder.local.yml"
    for k, v in {"NAVIGATOR_BOOTSTRAP": nav, "RUDDER_BOOTSTRAP": rboot, "RUDDER_LOCAL": local,
                 "HERMES_ROOT": tmp_path / "hermes"}.items():
        monkeypatch.setenv(k, str(v))
    for prof in ("navi", "chatbot"):
        (tmp_path / "hermes" / "profiles" / prof).mkdir(parents=True)
    (tmp_path / "projects").mkdir()
    sk = tmp_path / "skills" / "LOG-ANALYZER"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("# skill")
    return {"tmp": tmp_path, "nav": nav, "local": local}


def test_setup_paths_are_checked(host):
    from navigator_engineer import setup as S
    t = host["tmp"]
    assert S.check_project_root(t / "projects") == t / "projects"
    for bad, why in ((t / "missing", "does not exist"), ("relative/path", "absolute"), ("", "required")):
        with pytest.raises(S.SetupError, match=why):
            S.check_project_root(bad)
    assert S.check_skills_root(t / "skills") == (t / "skills", 1)
    assert S.check_skills_root("") == (None, 0)
    with pytest.raises(S.SetupError, match="no <skill>/SKILL.md"):
        S.check_skills_root(t / "projects")


def test_setup_set_paths_keeps_comments_and_old_key(host):
    from navigator_engineer import setup as S
    from navigator_engineer.config import load
    msgs = S.apply_changes([("PROJECT_ROOT", str(host["tmp"] / "projects")),
                            ("SKILLS_ROOT", str(host["tmp"] / "skills"))], test=None)
    assert any("readable and writable" in m for m in msgs)
    text = host["nav"].read_text()
    assert "# WORKSPACE: /old" in text and "replaced by PROJECT_ROOT" in text
    s = load()
    assert s.workspace == host["tmp"] / "projects" and s.extra_skills == (host["tmp"] / "skills",)
    assert "PROJECT_ROOT" in (host["tmp"] / "state" / "settings.log").read_text()


def test_setup_switch_to_slack_needs_a_passing_test(host):
    from navigator_engineer import setup as S
    from rudder_devices import config as rconfig
    seen = []

    def fake_test(cfg, consumer, timeout=300, out=print):
        seen.append(cfg)
        return False

    with pytest.raises(S.SetupError, match="nothing was saved"):
        S.apply_changes([("APPROVAL.METHOD", "slack"), ("APPROVAL.CHANNEL", "C0123ABCDEF")], test=fake_test)
    assert not host["local"].exists() and seen[0]["METHOD"] == "slack"
    msgs = S.apply_changes([("APPROVAL.METHOD", "slack"), ("APPROVAL.CHANNEL", "C0123ABCDEF")],
                           test=lambda *a, **k: True)
    assert any("APPROVAL saved" in m for m in msgs)
    a = rconfig.load().approval
    assert (a["METHOD"], a["CHANNEL"], a["BOT"], a["APPROVERS"]) == ("slack", "C0123ABCDEF", "navi", "*")
    assert "# APPROVAL:" in host["nav"].read_text()          # navigator.yml's own copy no longer shadows it
    from navigator_engineer.config import load
    assert load().approval["METHOD"] == "slack"                # one APPROVAL section for the host
    ch = approval.channel(a, "NAVI")
    assert ch.target == "slack:C0123ABCDEF"


def test_setup_refuses_unsafe_approval_settings(host):
    from navigator_engineer import setup as S
    ok = lambda *a, **k: True  # noqa: E731
    with pytest.raises(S.SetupError, match="Slack channel ID"):
        S.apply_changes([("APPROVAL.METHOD", "slack"), ("APPROVAL.CHANNEL", "#approvals")], test=ok)
    with pytest.raises(S.SetupError, match="never be the one that approves"):
        S.apply_changes([("APPROVAL.REPLY_PROFILES", ["navi"])], test=ok)
    with pytest.raises(S.SetupError, match="no Hermes profile"):
        S.apply_changes([("APPROVAL.BOT", "ghost")], test=ok)
    with pytest.raises(S.SetupError, match="never"):
        S.apply_changes([("APPROVAL.SHIP_ENGINEERS", ["*"])], test=ok)
    with pytest.raises(S.SetupError, match="unknown key"):
        S.apply_changes([("APPROVAL.SECRET", "x")], test=ok)
    assert not host["local"].exists()


def test_setup_add_remove_approvers(host):
    from navigator_engineer import setup as S
    from rudder_devices import config as rconfig
    tested = []
    S.edit_list("APPROVAL.APPROVERS", "U0123ABCDEF", True, test=lambda cfg, *a, **k: tested.append(cfg) or True)
    assert rconfig.load().approval["APPROVERS"] == ["U0123ABCDEF"] and tested
    S.edit_list("APPROVAL.APPROVERS", "U0123ABCDEF", False, test=lambda *a, **k: True)
    assert rconfig.load().approval["APPROVERS"] == "*"           # empty list = anyone in the channel


def test_setup_approval_test_waits_for_the_pushok(host, tmp_path):
    from navigator_engineer import setup as S
    sent = []

    def runner(argv, input=None, **kw):
        sent.append(input)
        import re
        code = re.search(r"pushok (\w+)", input).group(1)
        d = host["tmp"] / "appr"
        (d / approval.code_hash("decision", code)).write_text("approved\nPat (U1) on slack")
        (d / approval.code_hash("pending", code)).unlink()
        return type("R", (), {"returncode": 0})()

    cfg = {"METHOD": "slack", "BOT": "navi", "CHANNEL": "C0123ABCDEF", "APPROVAL_DIR": str(host["tmp"] / "appr")}
    lines = []
    assert S.test_approval(cfg, out=lines.append, runner=runner, sleep=lambda s: None)
    assert "`!pushok" in sent[0] and "Approved by Pat (U1) on slack" in lines[-1]
    assert list((host["tmp"] / "appr").iterdir()) == []


def test_show_lists_everything(host):
    from navigator_engineer import setup as S
    text = S.show()
    for k in ("PROJECT_ROOT", "SKILLS_ROOT", "APPROVAL.METHOD", "APPROVAL.APPROVERS", "APPROVAL.TTL_MIN"):
        assert k in text
    assert "telegram" in text and "(old key WORKSPACE)" in text
