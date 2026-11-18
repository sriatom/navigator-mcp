"""rudder-approve: a host's own /pushok handler records the decision (same checks as push-approve)."""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

from rudder_devices import approval
from rudder_devices import approve_cli as A
from test_p3 import rig  # noqa: F401
from test_p4 import ch, propose  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path):
    d = tmp_path / "push-approvals"
    d.mkdir(mode=0o700)
    conf = tmp_path / "approve.yml"
    conf.write_text(f"APPROVAL_DIR: {d}\nPLATFORMS: [slack, telegram, teams]\n"
                    f"CHANNELS:\n  slack: [C0APPROVE1]\nLOG: {tmp_path / 'approve.log'}\n")
    conf.chmod(0o600)
    return {"dir": d, "conf": conf, "tmp": tmp_path}


def _pending(env, rules: dict | None = None, label="RUDDER C20261115-abcdef: QALEAF1A"):
    store = approval.FileDecisions(env["dir"])
    code, hashes = store.open(label, rules)
    return code, hashes, store


RULES = {"method": "slack", "channel": "C0APPROVE1", "approvers": "*", "requester": "U1", "self_approve": True}


def run(env, *args) -> tuple[int, str]:
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = A.main([*args, "--config", str(env["conf"])])
    return rc, buf.getvalue()


def test_ok_records_the_decision(env):
    code, h, store = _pending(env, RULES)
    rc, out = run(env, "ok", code, "--user", "U2", "--name", "Pat", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 0 and "Approved" in out
    assert store.state(h) == "approved" and store.who(h) == "Pat (U2) on slack"
    assert not (env["dir"] / h["pending"]).exists()
    log = (env["tmp"] / "approve.log").read_text()
    assert "APPROVED" in log and code not in log


def test_no_records_a_rejection_json(env):
    code, h, store = _pending(env, RULES)
    rc, out = run(env, "no", code, "--user", "U1", "--platform", "slack", "--channel", "C0APPROVE1", "--json")
    got = json.loads(out)
    assert rc == 0 and got["recorded"] is True and got["verdict"] == "rejected" and "Rejected" in got["message"]
    assert store.state(h) == "rejected"


@pytest.mark.parametrize("args,why", [
    (["--platform", "discord", "--channel", "X"], "not taken on discord"),          # host: platform
    (["--platform", "slack", "--channel", "C0OTHER"], "only in the approval channel"),  # host: channel
    (["--platform", "telegram", "--channel", "-100"], "on slack only"),            # request: method
    (["--platform", "teams", "--channel", "19:abc"], "on slack only"),
])
def test_refusals_leave_the_code_open(env, args, why):
    code, h, store = _pending(env, RULES)
    rc, out = run(env, "ok", code, "--user", "U2", *args)
    assert rc == 1 and why in out and "still open" in out
    assert store.state(h) == "waiting"


def test_self_approve_off(env):
    code, h, store = _pending(env, {**RULES, "self_approve": False})
    rc, out = run(env, "ok", code, "--user", "U1", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 1 and "another approver" in out
    rc, _ = run(env, "no", code, "--user", "U1", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 0 and store.state(h) == "rejected"                 # the requester may still reject


def test_request_and_host_approver_lists(env):
    code, h, store = _pending(env, {**RULES, "approvers": ["U9"]})
    assert run(env, "ok", code, "--user", "U2", "--platform", "slack", "--channel", "C0APPROVE1")[0] == 1
    env["conf"].write_text(env["conf"].read_text() + "APPROVERS: [U2]\n")
    rc, out = run(env, "ok", code, "--user", "U9", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 1 and "not on the approvers list" in out
    assert store.state(h) == "waiting"


def test_unknown_or_rule_less_codes(env):
    rc, out = run(env, "ok", "ZZZZZZ", "--user", "U2", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 1 and "No pending" in out
    code, h, store = _pending(env, None, label="nv-push something")         # old plain-text request
    rc, out = run(env, "ok", code, "--user", "U2", "--platform", "slack", "--channel", "C0APPROVE1")
    assert rc == 1 and store.state(h) == "waiting"
    assert run(env, "ok", "bad code!", "--user", "U2", "--platform", "slack")[0] == 1


def test_thread_parent_channel(env):
    code, h, store = _pending(env, {**RULES, "channel": "C0APPROVE1"})
    env["conf"].write_text(env["conf"].read_text().replace("  slack: [C0APPROVE1]\n", ""))
    rc, _ = run(env, "ok", code, "--user", "U2", "--platform", "slack", "--channel", "C0THREAD", "--parent", "C0APPROVE1")
    assert rc == 0 and store.state(h) == "approved"


@pytest.mark.parametrize("body,err", [(None, "not found"), ("PLATFORMS: []\n", "at least one platform"),
                                      ("- a\n", "mapping"), ("PLATFORMS: [slack]\nCHANNELS: [x]\n", "CHANNELS")])
def test_config_required_and_valid(env, body, err):
    if body is None:
        env["conf"].unlink()
    else:
        env["conf"].write_text(body)
    rc, out = run(env, "check")
    assert rc == 2 and err in out


def test_config_must_not_be_writable_by_others(env):
    os.chmod(env["conf"], 0o666)
    rc, out = run(env, "check")
    assert rc == 2 and "chmod 600" in out
    os.chmod(env["conf"], 0o600)
    rc, out = run(env, "check", "--json")
    assert rc == 0 and json.loads(out)["platforms"] == ["slack", "telegram", "teams"]


def test_ids_are_checked(env):
    code, _, _ = _pending(env, RULES)
    assert run(env, "ok", code, "--user", "U2;rm -rf", "--platform", "slack")[0] == 2


def test_same_checks_as_the_hermes_plugin():
    spec = importlib.util.spec_from_file_location("push_approve", ROOT / "integrations/hermes/push-approve/__init__.py")
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    src = lambda plat, chat, user, parent="": type("S", (), {"platform": plat, "chat_id": chat,  # noqa: E731
                                                            "parent_chat_id": parent, "user_id": user,
                                                            "user_name": "x"})()
    cases = [(RULES, "approved", "slack", "C0APPROVE1", "U2", ""), (RULES, "approved", "telegram", "C0APPROVE1", "U2", ""),
             (RULES, "approved", "slack", "C9", "U2", ""), (RULES, "approved", "slack", "C9", "U2", "C0APPROVE1"),
             ({**RULES, "approvers": ["U3"]}, "approved", "slack", "C0APPROVE1", "U2", ""),
             ({**RULES, "self_approve": False}, "approved", "slack", "C0APPROVE1", "U1", ""),
             ({**RULES, "self_approve": False}, "rejected", "slack", "C0APPROVE1", "U1", ""),
             ({"method": "telegram", "channel": "", "approvers": "*"}, "approved", "telegram", "-1", "U2", "")]
    for rules, verdict, plat, chat, user, parent in cases:
        assert A.check_rules(rules, verdict, plat, chat, user, parent) == \
            plug._check(rules, verdict, src(plat, chat, user, parent)), (rules, verdict, plat, chat, user)


def test_end_to_end_with_a_ticket(ch, tmp_path):  # noqa: F811
    """RUDDER proposes, the host's handler calls rudder-approve, RUDDER sees the approval."""
    r = propose(ch)
    code = re.search(r"/pushok (\w+)", ch["sent"][-1]["message"]).group(1)
    conf = tmp_path / "approve.yml"
    conf.write_text(f"APPROVAL_DIR: {ch['dir']}\nPLATFORMS: [telegram]\nLOG: {tmp_path / 'a.log'}\n")
    conf.chmod(0o600)
    assert A.main(["ok", code, "--user", "U2", "--platform", "telegram", "--channel", "-100", "--config", str(conf)]) == 0
    st = ch["c"].status(r["ticket"])
    assert st["state"] == "approved" and st["decided_by"].startswith("U2")


def test_more_approval_methods():
    sent = []
    run_ = lambda argv, **kw: sent.append(argv) or type("R", (), {"returncode": 0})()  # noqa: E731
    c = approval.HermesChannel({"METHOD": "teams", "CHANNEL": "approvals.team", "APPROVAL_DIR": "/tmp/x"},
                               "NAVI", runner=run_)
    assert c.target == "teams:approvals.team" and c.rules()["method"] == "teams"
    with pytest.raises(approval.ApprovalError):
        approval.HermesChannel({"METHOD": "teams"}, "NAVI")
    with pytest.raises(approval.ApprovalError):
        approval.HermesChannel({"METHOD": "api"}, "NAVI")
