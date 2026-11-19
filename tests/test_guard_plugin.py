"""navigator-guard (integrations/hermes/navigator-guard): private paths, LOCAL projects, web queries."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from navigator_engineer import research

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def G(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("navigator_guard", ROOT / "integrations/hermes/navigator-guard/__init__.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    home = tmp_path / "home"
    (home / ".config/navigator").mkdir(parents=True)
    ws = home / "jobs"
    (ws / "SECRETJOB/CONFIG").mkdir(parents=True)
    (ws / "SECRETJOB/CONFIG/BOS-93360A [10.3.1.3].txt").write_text("hostname BOS-93360A\n")
    (ws / "OPENJOB").mkdir()
    (ws / "OPENJOB/.navigator_mode").write_text("CLOUD\n")
    (home / ".config/navigator/navigator.yml").write_text(
        f"PROJECT_ROOT: {ws}\nMODELS:\n  LOCAL:\n    URL: http://10.9.9.9:11434/v1\n")
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "navigator-guard.json").write_text('{"always_local": false}')
    for k in ("NAVIGATOR_BOOTSTRAP", "ATLAS_BOOTSTRAP", "RUDDER_BOOTSTRAP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hh))
    g.home, g.ws, g.hh = home, ws, hh
    return g


def call(g, tool, sid="s1", **args):
    return g.on_pre_tool_call(tool_name=tool, args=args, session_id=sid)


def on(g, sid, url):
    g.on_api_request(session_id=sid, base_url=url, model="m", provider="p")


def test_off_without_the_profile_file(G):
    (G.hh / "navigator-guard.json").unlink()
    assert call(G, "read_file", path="~/.atlas/data/CREDENTIALS.yml") is None


def test_private_paths_in_any_form(G):
    for p in ("~/.atlas/data/CREDENTIALS.yml", f"{G.home}/.config/atlas/vault.key", "$HOME/.rudder/audit.log",
              "~/.hermes/push-approvals/x.decision"):
        r = call(G, "terminal", command=f"cat {p}")
        assert r and r["action"] == "block" and "private" in r["message"], p
    assert call(G, "mcp__navigator__inventory", action="list") is None       # NAVIGATOR's own tools pass


def test_mode_files_only_through_navigator(G):
    r = call(G, "write_file", path=f"{G.ws}/SECRETJOB/.navigator_mode", content="CLOUD")
    assert r["action"] == "block" and "mode action=set" in r["message"]


def test_local_project_on_a_cloud_model(G):
    on(G, "c", "https://cloud.example.com/v1")
    r = call(G, "read_file", sid="c", path=f"{G.ws}/SECRETJOB/CONFIG/x.txt")
    assert r["action"] == "block" and "/model local" in r["message"] and "SECRETJOB" in r["message"]
    assert call(G, "read_file", sid="c", path=f"{G.ws}/OPENJOB/notes.txt") is None          # CLOUD project
    assert call(G, "mcp__navigator__project", sid="c", action="search", project="SECRETJOB", search="x") is None
    # unknown endpoint counts as cloud
    assert call(G, "read_file", sid="new", path=f"{G.ws}/SECRETJOB/a")["action"] == "block"
    # a cd is remembered for later relative paths
    on(G, "c2", "https://cloud.example.com/v1")
    assert call(G, "terminal", sid="c2", command=f"cd {G.ws}/SECRETJOB")["action"] == "block"


def test_local_model_may_read_but_web_queries_stay_generic(G):
    on(G, "l", "http://10.9.9.9:11434/v1")                 # the configured local model
    assert call(G, "read_file", sid="l", path=f"{G.ws}/SECRETJOB/CONFIG/x.txt") is None
    assert call(G, "web_search", sid="l", query="nx-os hsrp version 2 preempt delay")is None
    for q in ("hsrp issue on 10.3.1.3", "BOS-93360A hsrp flapping", "SECRETJOB vlan question",
              "interface Vlan3102\n ip address 10.1.2.251/24\n hsrp 3102"):
        r = call(G, "web_search", sid="l", query=q)
        assert r and r["action"] == "block" and "LOCAL session" in r["message"], q


def test_navigator_tool_marks_the_session_local(G):
    on(G, "n", "http://127.0.0.1:11434/v1")
    call(G, "mcp__navigator__project", sid="n", action="search", project="SECRETJOB", search="vlan")
    assert call(G, "web_search", sid="n", query="SECRETJOB vlan 3102")["action"] == "block"


def test_always_local(G):
    (G.hh / "navigator-guard.json").write_text('{"always_local": true}')
    assert call(G, "web_search", query="bgp flap 192.0.2.7")["action"] == "block"
    assert call(G, "web_search", query="eos bgp graceful restart defaults") is None


def test_settings_files_named_by_connect(G, tmp_path):
    atlas = tmp_path / "atlas.yml"
    atlas.write_text(f"DATA_PATH: {tmp_path}/vault-data\n")
    (G.hh / "navigator-guard.json").write_text(json.dumps({"always_local": False, "atlas_bootstrap": str(atlas)}))
    assert call(G, "terminal", command=f"ls {tmp_path}/vault-data")["action"] == "block"


def test_broken_settings_fail_closed(G):
    (G.home / ".config/navigator/navigator.yml").write_text("PROJECT_ROOT: [unclosed\n")
    r = call(G, "terminal", command="echo hi")
    assert r["action"] == "block" and "cannot read NAVIGATOR's settings" in r["message"]


def test_model_command_unwrap(G):
    assert G.on_gateway_dispatch(SimpleNamespace(text="`/model local`")) == {"action": "rewrite", "text": "/model local"}
    assert G.on_gateway_dispatch(SimpleNamespace(text="please /model local now")) is None
    assert G.on_gateway_dispatch(SimpleNamespace(text="/model local")) is None


@pytest.mark.parametrize("q", ["bgp on 10.1.1.1", "fe80::1:2 neighbor", "aa:bb:cc:dd:ee:ff moved", "remote-as 65001",
                               "router bgp 65000", "eos 4.33.1 release notes", "hostname leaf1", "%BGP-5-ADJCHANGE",
                               "how to configure hsrp", "SECRET node", "a\nb\nc"])
def test_same_rules_as_research(G, q):
    assert G.check_generic(q, ["SECRET"]) == research.check_generic(q, ["SECRET"])
