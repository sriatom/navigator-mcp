"""navigator-setup: the APPROVE.* keys write rudder-approve's config (approve.yml)."""
from __future__ import annotations

import stat

import pytest
import yaml

from navigator_engineer import setup as S
from rudder_devices import approve_cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    nav = tmp_path / "navigator.yml"
    nav.write_text(f"PROJECT_ROOT: {tmp_path}\nSTATE_PATH: {tmp_path / 'state'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    local = tmp_path / "rudder.local.yml"
    local.write_text("APPROVAL: {METHOD: teams, BOT: default, CHANNEL: '19:abc@thread.tacv2'}\n")
    monkeypatch.setenv("RUDDER_LOCAL", str(local))
    monkeypatch.setenv("RUDDER_APPROVE_CONFIG", str(tmp_path / "approve.yml"))
    monkeypatch.setattr(S, "hermes_home", lambda: tmp_path / "nohermes")
    return tmp_path / "approve.yml"


def test_set_writes_a_valid_private_config(env):
    msgs = S.apply_changes([("APPROVE.PLATFORMS", "teams,slack"),
                            ("APPROVE.CHANNELS", "teams=19:abc@thread.tacv2,slack=C0123ABCDEF"),
                            ("APPROVE.APPROVERS", "*")])
    assert any("rudder-approve config saved" in m for m in msgs) and not any("WARNING" in m for m in msgs)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    cfg = approve_cli.load_config(str(env))               # rudder-approve itself accepts it
    assert cfg["platforms"] == ["teams", "slack"]
    assert cfg["channels"] == {"teams": ["19:abc@thread.tacv2"], "slack": ["C0123ABCDEF"]}
    assert cfg["approvers"] == "*"
    text = S.show()
    assert "APPROVE.PLATFORMS       teams, slack" in text and "teams=19:abc@thread.tacv2" in text
    assert "APPROVE:" in (env.parent / "state" / "settings.log").read_text()


def test_add_remove_and_turn_off(env):
    S.apply_changes([("APPROVE.PLATFORMS", "teams")])
    S.edit_list("APPROVE.PLATFORMS", "discord", True)
    S.edit_list("APPROVE.CHANNELS", "discord=1234567890", True)
    S.edit_list("APPROVE.APPROVERS", "U0123ABCDEF", True)
    d = yaml.safe_load(env.read_text())
    assert d["PLATFORMS"] == ["teams", "discord"] and d["CHANNELS"] == {"discord": ["1234567890"]}
    assert d["APPROVERS"] == ["U0123ABCDEF"]
    S.edit_list("APPROVE.APPROVERS", "U0123ABCDEF", False)
    assert yaml.safe_load(env.read_text())["APPROVERS"] == "*"
    S.edit_list("APPROVE.CHANNELS", "discord=1234567890", False)
    assert "CHANNELS" not in yaml.safe_load(env.read_text())
    msgs = S.apply_changes([("APPROVE.PLATFORMS", "")])
    assert any("turned off" in m for m in msgs) and not env.exists()
    assert list(env.parent.glob("approve.yml.bak-*"))     # the old file is kept


def test_bad_values_change_nothing(env):
    S.apply_changes([("APPROVE.PLATFORMS", "teams")])
    before = env.read_text()
    for pairs in ([("APPROVE.CHANNELS", "slack=C1")],               # slack is not in PLATFORMS
                  [("APPROVE.CHANNELS", "teams")],                  # no =ID
                  [("APPROVE.PLATFORMS", "Teams Chat")],
                  [("APPROVE.APPROVERS", "bad user!")],
                  [("APPROVE.NOPE", "x")]):
        with pytest.raises(S.SetupError):
            S.apply_changes(pairs)
    assert env.read_text() == before


def test_warns_when_the_method_is_not_a_platform(env):
    msgs = S.apply_changes([("APPROVE.PLATFORMS", "slack")])
    assert any("APPROVAL.METHOD is teams" in m for m in msgs)


def test_interview_defaults_come_from_the_approval_settings(env):
    cur = S.current()
    got = dict(S._approve_questions(lambda text, default="": default, cur, "teams", "19:abc@thread.tacv2", "*"))
    assert got == {"APPROVE.PLATFORMS": "teams", "APPROVE.CHANNELS": "teams=19:abc@thread.tacv2",
                   "APPROVE.APPROVERS": "*"}
    assert S._approve_questions(lambda text, default="": "no", cur, "slack", "C1", "*") == []
