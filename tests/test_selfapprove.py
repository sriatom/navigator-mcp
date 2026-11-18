"""Task 16 / decision #161: self-approval on by default, no interview question, /selfapprove off from chat."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import types
from pathlib import Path

import pytest
import yaml

from navigator_engineer import setup as S
from rudder_devices import approval, approve_cli, selfapprove

ROOT = Path(__file__).resolve().parents[1]
CHAN = "C0APPROVE1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    nav = tmp_path / "navigator.yml"
    nav.write_text(f"PROJECT_ROOT: {tmp_path}\nSTATE_PATH: {tmp_path / 'navstate'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    adir = tmp_path / "push-approvals"
    adir.mkdir(mode=0o700)
    boot = tmp_path / "rudder.yml"
    boot.write_text(f"STATE_PATH: {tmp_path / 'rstate'}\nCONSUMER: NAVI\n")
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(boot))
    local = tmp_path / "rudder.local.yml"
    local.write_text(f"APPROVAL: {{METHOD: slack, BOT: default, CHANNEL: {CHAN}, APPROVERS: '*', "
                     f"APPROVAL_DIR: {adir}, TTL_MIN: 30}}\n")
    monkeypatch.setenv("RUDDER_LOCAL", str(local))
    monkeypatch.setenv("RUDDER_APPROVE_CONFIG", str(tmp_path / "no-approve.yml"))
    monkeypatch.setattr(S, "hermes_home", lambda: tmp_path / "nohermes")
    return types.SimpleNamespace(tmp=tmp_path, local=local, adir=adir, rstate=tmp_path / "rstate",
                                 navstate=tmp_path / "navstate")


def _settings(env, **appr):
    base = yaml.safe_load(env.local.read_text())["APPROVAL"]
    return types.SimpleNamespace(approval={**base, **appr}, state_path=env.rstate, consumer="NAVI")


def _open(env, requester="U1", self_approve=True):
    store = approval.FileDecisions(env.adir)
    rules = {"method": "slack", "channel": CHAN, "approvers": "*", "requester": requester,
             "self_approve": self_approve}
    code, hashes = store.open("RUDDER C20261117-aaaaaa: QALEAF1A", rules)
    return code, hashes, store


def test_default_is_on_and_the_interview_does_not_ask(env):
    out, asked = [], []
    answers = iter([str(env.tmp), "-", "slack", "default", CHAN, "*", "-", "90", "02:00", "no",
                    "cloud", "-", "none", "off", "no", "no", "no", "no"])

    def ask(p):
        asked.append(p)
        return next(answers)
    S.interview(ask=ask, out=out.append, test=lambda *a, **k: True, check_model=lambda b: "ok")
    assert not any("SELF_APPROVE" in p for p in asked)
    assert any("Self-approval is on" in str(o) and "/selfapprove off" in str(o)
               and "navigator-setup set APPROVAL.SELF_APPROVE true" in str(o) for o in out)
    assert approval.HermesChannel(yaml.safe_load(env.local.read_text())["APPROVAL"], "NAVI").self_approve is True


def test_show_prints_how_to_turn_it_off(env):
    assert "/selfapprove off in the approval channel" in S.show()


def test_turn_off_by_an_approver_tightens_saves_and_logs(env):
    code, h, store = _open(env)
    ok, msg = selfapprove.turn_off(user="U2", name="Pat", platform="slack", channel=CHAN, settings=_settings(env))
    assert ok and "OFF" in msg and "1 open request" in msg and selfapprove.ON_HINT in msg
    d = yaml.safe_load(env.local.read_text())["APPROVAL"]
    assert d["SELF_APPROVE"] is False and d["CHANNEL"] == CHAN and d["METHOD"] == "slack"     # others kept
    assert list(env.tmp.glob("rudder.local.yml.bak-*"))                                        # backed up
    assert "APPROVAL.SELF_APPROVE: True -> False (/selfapprove off by Pat (U2) on slack:C0APPROVE1)" in \
        (env.navstate / "settings.log").read_text()
    audit = (env.rstate / "audit.log").read_text()
    assert "SELF_APPROVE on -> off by Pat (U2)" in audit and code not in audit
    rules = json.loads((env.adir / h["pending"]).read_text())
    assert rules["self_approve"] is False and rules["requester"] == "U1"
    # the open request now refuses the requester (same check as the plugin and rudder-approve)
    assert "another approver" in approve_cli.check_rules(rules, "approved", "slack", CHAN, "U1")
    assert approve_cli.check_rules(rules, "approved", "slack", CHAN, "U3") is None
    assert approval.HermesChannel(d, "NAVI").self_approve is False


def test_turn_off_refused_outside_the_approval_channel_or_for_non_approvers(env):
    ok, msg = selfapprove.turn_off(user="U2", platform="slack", channel="C0OTHER99", settings=_settings(env))
    assert not ok and "approval channel" in msg
    ok, msg = selfapprove.turn_off(user="U2", platform="telegram", channel=CHAN, settings=_settings(env))
    assert not ok and "slack only" in msg
    ok, msg = selfapprove.turn_off(user="U9", platform="slack", channel=CHAN,
                                   settings=_settings(env, APPROVERS=["U2"]))
    assert not ok and "approvers" in msg
    assert "SELF_APPROVE" not in yaml.safe_load(env.local.read_text())["APPROVAL"]
    assert (env.rstate / "audit.log").read_text().count("REFUSED /selfapprove off") == 3


def test_thread_reply_is_accepted_in_the_approval_channel(env):
    ok, _ = selfapprove.turn_off(user="U2", platform="slack", channel="1700000000.1", parent=CHAN,
                                 settings=_settings(env))
    assert ok


def test_already_off_changes_nothing_but_still_tightens(env):
    _open(env)
    ok, msg = selfapprove.turn_off(user="U2", platform="slack", channel=CHAN,
                                   settings=_settings(env, SELF_APPROVE=False))
    assert ok and "already off" in msg
    assert not list(env.tmp.glob("rudder.local.yml.bak-*"))


def test_plain_text_nv_push_requests_are_left_alone(env):
    store = approval.FileDecisions(env.adir)
    _, h = store.open("nv-push QALEAF1A")
    assert selfapprove.tighten_pending(env.adir) == 0
    assert (env.adir / h["pending"]).read_text().strip() == "nv-push QALEAF1A"


def test_open_chat_threads_are_told(env):
    tdir = env.rstate / "tickets"
    tdir.mkdir(parents=True)
    (tdir / "C20261117-aaaaaa.json").write_text(json.dumps(
        {"id": "C20261117-aaaaaa", "state": "waiting_approval", "reply_to": "netclaw/slack:C0CHAT0001:1.2"}))
    (tdir / "C20261117-bbbbbb.json").write_text(json.dumps(
        {"id": "C20261117-bbbbbb", "state": "applied", "reply_to": "netclaw/slack:C0CHAT0001:1.3"}))
    sent = []

    def runner(cmd, **kw):
        sent.append((cmd, kw.get("input")))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ok, _ = selfapprove.turn_off(user="U2", platform="slack", channel=CHAN, runner=runner,
                                 settings=_settings(env, REPLY_PROFILES=["netclaw"]))
    assert ok and len(sent) == 1 and "C20261117-aaaaaa" in sent[0][1] and "slack:C0CHAT0001:1.2" in sent[0][0]


def _cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = approve_cli.main(list(args))
    return rc, buf.getvalue()


def test_cli_off_and_never_on(env):
    rc, out = _cli("selfapprove", "on", "--user", "U2", "--platform", "slack", "--channel", CHAN)
    assert rc == 1 and "shell" in out
    assert "SELF_APPROVE" not in yaml.safe_load(env.local.read_text())["APPROVAL"]
    rc, out = _cli("selfapprove", "off", "--user", "U2", "--platform", "slack", "--channel", CHAN, "--json")
    assert rc == 0 and json.loads(out)["changed"] is True
    assert yaml.safe_load(env.local.read_text())["APPROVAL"]["SELF_APPROVE"] is False


def test_cli_host_gates_apply_with_an_approve_config(env, monkeypatch):
    conf = env.tmp / "approve.yml"
    conf.write_text(f"APPROVAL_DIR: {env.adir}\nPLATFORMS: [teams]\nLOG: {env.tmp / 'approve.log'}\n")
    conf.chmod(0o600)
    monkeypatch.setenv("RUDDER_APPROVE_CONFIG", str(conf))
    rc, out = _cli("selfapprove", "off", "--user", "U2", "--platform", "slack", "--channel", CHAN)
    assert rc == 1 and "not taken on slack" in out


def test_shell_turns_it_back_on_only_after_the_test_code(env):
    selfapprove.turn_off(user="U2", platform="slack", channel=CHAN, settings=_settings(env))
    with pytest.raises(S.SetupError):
        S.apply_changes([("APPROVAL.SELF_APPROVE", "true")], test=lambda *a, **k: False)
    assert yaml.safe_load(env.local.read_text())["APPROVAL"]["SELF_APPROVE"] is False
    msgs = S.apply_changes([("APPROVAL.SELF_APPROVE", "true")], test=lambda *a, **k: True)
    assert yaml.safe_load(env.local.read_text())["APPROVAL"]["SELF_APPROVE"] is True
    assert any("Self-approval is on" in m for m in msgs)


def _plugin():
    spec = importlib.util.spec_from_file_location("push_approve_t16", ROOT / "integrations/hermes/push-approve/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_command_and_approval_channel_filter(env, monkeypatch):
    P = _plugin()
    monkeypatch.setenv("PUSH_APPROVE_CHANNELS", CHAN)
    src = types.SimpleNamespace(platform="slack", chat_id=CHAN, parent_chat_id="", user_id="U2", user_name="Pat")
    ev = types.SimpleNamespace(source=src, text="`!selfapprove off`")
    assert P.on_gateway_dispatch(ev) == {"action": "rewrite", "text": "/selfapprove off"}
    assert P._selfapprove("on", src).startswith("⛔") and "shell" in P._selfapprove("on", src)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"changed": True, "message": "🔒 off"}))
    monkeypatch.setattr(P, "_rudder_approve", lambda: "/bin/rudder-approve")
    assert P._selfapprove("off", src, runner=runner) == "🔒 off"
    assert calls[0][:3] == ["/bin/rudder-approve", "selfapprove", "off"] and "U2" in calls[0] and CHAN in calls[0]
