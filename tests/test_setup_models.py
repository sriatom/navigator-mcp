"""navigator-setup: the MODELS keys (local model for LOCKED work)."""
from __future__ import annotations

import httpx
import pytest
import yaml

from navigator_engineer import setup as S


@pytest.fixture
def env(tmp_path, monkeypatch):
    nav = tmp_path / "navigator.yml"
    nav.write_text("PROJECT_ROOT: %s\n# a comment that must stay\nCONSUMER: NAVI\nSTATE_PATH: %s\n"
                   % (tmp_path, tmp_path / "state"))
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    monkeypatch.setenv("RUDDER_LOCAL", str(tmp_path / "rudder.local.yml"))
    monkeypatch.setattr(S, "hermes_home", lambda: tmp_path / "nohermes")
    return nav


def ok(block):
    return "local model ok"


def test_set_local_model_writes_a_nested_block(env):
    msgs = S.apply_changes([("MODELS.LOCAL_URL", "http://127.0.0.1:11434/v1"), ("MODELS.LOCAL_MODEL", "qwen3.5:9b")],
                           check_model=ok)
    assert "local model ok" in msgs and any(m.startswith("MODELS saved") for m in msgs)
    text = env.read_text()
    assert "# a comment that must stay" in text
    d = yaml.safe_load(text)
    assert d["MODELS"]["HOST"] == "cloud"
    assert d["MODELS"]["LOCAL"]["URL"] == "http://127.0.0.1:11434/v1"
    assert d["MODELS"]["LOCAL"]["MODEL"] == "qwen3.5:9b" and d["MODELS"]["LOCAL"]["ALLOW_HOSTS"] == []
    assert d["CONSUMER"] == "NAVI"
    # a second change replaces the block, never duplicates it; other LOCAL keys are kept
    S.apply_changes([("MODELS.TIMEOUT_SEC", "120")], check_model=ok)
    text = env.read_text()
    assert text.count("MODELS:") == 1
    d = yaml.safe_load(text)
    assert d["MODELS"]["LOCAL"]["TIMEOUT_SEC"] == 120 and d["MODELS"]["LOCAL"]["MODEL"] == "qwen3.5:9b"
    assert "MODELS.HOST" in S.show() and "qwen3.5:9b" in S.show()


def test_bad_values_are_refused_and_nothing_is_saved(env):
    before = env.read_text()
    for pairs in ([("MODELS.HOST", "maybe")],
                  [("MODELS.HOST", "local")],                                        # local host needs a URL
                  [("MODELS.LOCAL_URL", "http://8.8.8.8:11434/v1"), ("MODELS.LOCAL_MODEL", "x")],
                  [("MODELS.LOCAL_URL", "ftp://127.0.0.1/v1"), ("MODELS.LOCAL_MODEL", "x")],
                  [("MODELS.LOCAL_URL", "http://127.0.0.1:11434/v1")],                # no model name
                  [("MODELS.TIMEOUT_SEC", "5")]):
        with pytest.raises(S.SetupError):
            S.apply_changes(pairs, check_model=ok)
    assert env.read_text() == before


def test_the_endpoint_must_answer_with_the_model(env):
    def handler(req):
        return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    block = S.normalize_models({"LOCAL_URL": "http://127.0.0.1:11434/v1", "LOCAL_MODEL": "qwen3.5:9b"})
    with pytest.raises(S.SetupError, match="not on that endpoint"):
        S.check_local_model(block, client=client)
    block = S.normalize_models({"LOCAL_URL": "http://127.0.0.1:11434/v1", "LOCAL_MODEL": "llama3.1:8b"})
    assert "answers" in S.check_local_model(block, client=client)

    def down(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(S.SetupError, match="no answer"):
        S.check_local_model(block, client=httpx.Client(transport=httpx.MockTransport(down)))
    before = env.read_text()

    def refuse(b):
        raise S.SetupError("no answer")
    with pytest.raises(S.SetupError):
        S.apply_changes([("MODELS.LOCAL_URL", "http://127.0.0.1:11434/v1"), ("MODELS.LOCAL_MODEL", "x")],
                        check_model=refuse)
    assert env.read_text() == before


def test_removing_the_local_model(env):
    S.apply_changes([("MODELS.LOCAL_URL", "http://127.0.0.1:11434/v1"), ("MODELS.LOCAL_MODEL", "qwen3.5:9b")],
                    check_model=ok)
    S.apply_changes([("MODELS.LOCAL_URL", "")], check_model=ok)
    d = yaml.safe_load(env.read_text())
    assert d["MODELS"]["LOCAL"]["URL"] == "" and d["MODELS"]["LOCAL"]["MODEL"] == ""


def test_interview_asks_the_models(env, tmp_path):
    answers = iter([str(tmp_path), "-", "telegram", "default", "-", "*", "-", "90", "02:00", "no",
                    "cloud", "http://127.0.0.1:11434/v1", "qwen3.5:9b", "none", "off", "no", "no", "no", "no"])
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return next(answers)
    msgs = S.interview(ask=ask, out=lambda *a: None, test=lambda *a, **k: True, check_model=ok)
    assert any(p.startswith("MODELS.HOST") for p in asked) and any(p.startswith("MODELS.LOCAL_MODEL") for p in asked)
    assert any(m.startswith("MODELS saved: HOST=cloud") for m in msgs)
    assert yaml.safe_load(env.read_text())["MODELS"]["LOCAL"]["MODEL"] == "qwen3.5:9b"
