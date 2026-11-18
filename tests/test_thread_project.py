"""navigator-reply remembers the thread's project and fills project= on later navigator calls;
the inventory tool takes inventory= as well as name=."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastmcp import Client

from test_p7d import _call, front  # noqa: F401
from test_p3 import rig  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def plug(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "navigator_reply_tp", ROOT / "integrations" / "hermes" / "navigator-reply" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_home", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "_host_args", lambda sid: {"reply_to": "r", "requested_by": "u"})
    return mod


def _create(mod, sid="S1", name="DEMO3"):
    mod.on_post_tool_call("mcp__navigator__project", {"action": "create", "mode": "LOCKED"},
                          json.dumps({"project": name, "mode": "LOCKED", "path": "/x", "created": True}),
                          session_id=sid)


def test_project_create_is_remembered_and_filled(plug, tmp_path):
    _create(plug)
    assert plug.thread_project("S1") == "DEMO3"
    assert json.loads((tmp_path / "navigator-reply-projects.json").read_text()) == {"S1": "DEMO3"}
    got = plug.on_pre_tool_call("mcp__navigator__inventory", {"action": "show"}, session_id="S1")
    assert got == {"action": "modify", "args": {"project": "DEMO3"}}
    assert plug.on_pre_tool_call("mcp__navigator__inventory", {"action": "show"}, session_id="S2") is None


def test_explicit_project_kept_and_unscoped_tools_untouched(plug):
    _create(plug)
    assert plug.on_pre_tool_call("mcp__navigator__device", {"action": "read", "project": "OTHER"},
                                 session_id="S1") is None
    for tool in ("mcp__navigator__skill", "mcp__navigator__mode", "mcp__navigator__project", "web_search"):
        assert plug.on_pre_tool_call(tool, {"action": "list"}, session_id="S1") is None


def test_survives_a_restart(plug, tmp_path):
    _create(plug)
    plug._projects = None                                     # a new process reads the file
    assert plug.thread_project("S1") == "DEMO3"


def test_successful_call_with_project_updates_but_errors_do_not(plug):
    _create(plug)
    plug.on_post_tool_call("mcp__navigator__inventory", {"action": "show", "project": "BAD"},
                           json.dumps({"error": "no such project"}), session_id="S1")
    assert plug.thread_project("S1") == "DEMO3"
    plug.on_post_tool_call("mcp__navigator__device", {"action": "read", "project": "LAB2"},
                           json.dumps({"run": "r1"}), session_id="S1")
    assert plug.thread_project("S1") == "LAB2"


def test_tool_call_wrapper_filled_and_propose_still_gets_host_args(plug):
    _create(plug)
    got = plug.on_pre_tool_call("tool_call", {"calls": [
        {"name": "mcp__navigator__inventory", "arguments": {"action": "show"}},
        {"name": "mcp__navigator__change", "arguments": {"action": "propose", "project": "X"}},
        {"name": "web_search", "arguments": {"q": "evpn"}}]}, session_id="S1")
    calls = got["args"]["calls"]
    assert calls[0]["arguments"] == {"action": "show", "project": "DEMO3"}
    assert calls[1]["arguments"] == {"action": "propose", "project": "X", "reply_to": "r", "requested_by": "u"}
    assert calls[2]["arguments"] == {"q": "evpn"}
    assert plug.on_pre_tool_call("tool_call", {"calls": [{"name": "web_search", "arguments": {}}]},
                                 session_id="S1") is None


def test_propose_without_thread_project(plug):
    got = plug.on_pre_tool_call("mcp__navigator__change", {"action": "propose"}, session_id="S9")
    assert got == {"action": "modify", "args": {"reply_to": "r", "requested_by": "u"}}


async def test_inventory_accepts_inventory_keyword(front):  # noqa: F811
    async with Client(front["mcp"]) as c:
        by_name = await _call(c, "inventory", action="show", name="FAB")
        by_inv = await _call(c, "inventory", action="show", inventory="FAB")
    assert by_name == by_inv and "QALEAF1A" in json.dumps(by_inv)
