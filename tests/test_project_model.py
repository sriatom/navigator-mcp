"""A project follows the model (#156): CLOUD / LOCAL words, old words read, default from the host,
inventory kept for every project, host_model trusted only when the host says so."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from navigator_engineer import models
from navigator_engineer.projects import CLOUD, LOCAL, LOCKED, OPEN, ProjectError, Projects, norm_mode

ROOT = Path(__file__).resolve().parents[1]


def test_words():
    assert (OPEN, LOCKED) == (CLOUD, LOCAL) == ("CLOUD", "LOCAL")
    assert [norm_mode(w) for w in ("cloud", "Open", "LOCAL", "locked", "x", None)] == \
        ["CLOUD", "CLOUD", "LOCAL", "LOCAL", None, None]


def test_old_mode_files_are_read(tmp_path):
    from test_names_locked import _s
    s = _s(tmp_path)
    for word, want in (("OPEN", CLOUD), ("LOCKED", LOCAL), ("CLOUD", CLOUD), ("LOCAL", LOCAL), ("junk", LOCAL)):
        d = tmp_path / f"P_{word}"
        d.mkdir()
        (d / ".navigator_mode").write_text(word + "\n")
        assert Projects(s).mode(d) == want


@pytest.mark.parametrize("host,want", [("local", LOCAL), ("cloud", CLOUD)])
def test_new_project_follows_the_host_model(tmp_path, host, want):
    from test_names_locked import _s
    r = Projects(_s(tmp_path)).create(None, "bgp audit", "PFM_" + host, host=host)
    assert r["mode"] == want
    assert (Path(r["path"]) / ".navigator_mode").read_text().strip() == want


def test_old_words_accepted_on_create(tmp_path):
    from test_names_locked import _s
    assert Projects(_s(tmp_path)).create("locked", "x", "PFM_OLD")["mode"] == LOCAL
    with pytest.raises(ProjectError, match="CLOUD or LOCAL"):
        Projects(_s(tmp_path)).create("secret", "x", "PFM_BAD")


def test_host_hint_needs_trust(tmp_path, monkeypatch):
    from test_names_locked import _s
    s = _s(tmp_path, "cloud")
    monkeypatch.delenv("NAVIGATOR_TRUST_HOST_MODEL", raising=False)
    monkeypatch.delenv("NAVIGATOR_HOST_MODEL", raising=False)
    assert models.host_kind(s, "local") == "cloud"              # a client cannot claim local by itself
    monkeypatch.setenv("NAVIGATOR_TRUST_HOST_MODEL", "1")
    assert models.host_kind(s, "local") == "local"
    assert models.host_kind(s, "bogus") == "cloud"
    assert models.host_kind(s, None) == "cloud"


def _plugin(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "navigator_reply_pfm", ROOT / "integrations" / "hermes" / "navigator-reply" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._home = lambda: str(tmp_path)
    return mod


def test_plugin_fills_host_model_from_the_real_model(tmp_path):
    p = _plugin(tmp_path)
    assert p.on_pre_tool_call("mcp__navigator__device", {"action": "read"}, session_id="S") is None   # unknown
    p.on_api_request(session_id="S", model="gemma4:12b", base_url="http://127.0.0.1:11434/v1")
    got = p.on_pre_tool_call("mcp__navigator__device", {"action": "read", "host_model": "cloud"}, session_id="S")
    assert got["args"]["host_model"] == "local"
    p.on_api_request(session_id="S", model="gemini", base_url="https://aiplatform.googleapis.com/v1")
    got = p.on_pre_tool_call("mcp__navigator__project", {"action": "create", "host_model": "local"}, session_id="S")
    assert got["args"]["host_model"] == "cloud"                 # the model's own claim is replaced
    assert p.on_pre_tool_call("mcp__navigator__inventory", {"action": "list"}, session_id="S") is None
    assert p.on_pre_tool_call("mcp__navigator__device", {"action": "read", "host_model": "cloud"},
                              session_id="S") is None           # already right


@pytest.mark.parametrize("text,want", [("new LOCAL project X", True), ("create project X as local", True),
                                       ("new locked project X", True), ("new CLOUD project X", False),
                                       ("/model local", False), ("copy it to the local disk", False)])
def test_local_project_intent(tmp_path, text, want):
    assert _plugin(tmp_path).wants_locked(text) is want
