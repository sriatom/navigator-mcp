"""navigator-reply: a LOCKED project message switches the thread to the local model; the project card."""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="navreply-")


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "navigator_reply_card", ROOT / "integrations" / "hermes" / "navigator-reply" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._home = lambda: _TMP                     # thread-project file never in the real Hermes home
    return mod


@pytest.mark.parametrize("text,want", [
    ("new LOCKED project DEMO2. Use the inventory EVPN_LAB", True),
    ("*new locked project DEMO3*", True),
    ("create project ACME_BGP as LOCKED", True),
    ("start project X in locked mode", True),
    ("Locked Project for the BGP audit", True),
    ("new OPEN project DEMO4", False),
    ("is QALEAF1A locked by a change?", False),
    ("/model locked", False),
    ("/model local", False),
    ("the port is locked down", False),
])
def test_wants_locked(text, want):
    assert _plugin().wants_locked(text) is want


@dataclasses.dataclass
class Event:
    text: str
    source: str = "slack:C1:T1"


class FakeGateway:
    def __init__(self, base_url="", authorized=True, works=True):
        self._session_model_overrides = {"K": {"base_url": base_url}} if base_url else {}
        self.commands, self.authorized, self.works = [], authorized, works

    def _normalize_source_for_session_key(self, src):
        return src

    def _session_key_for_source(self, src):
        return "K"

    def _is_user_authorized(self, src):
        return self.authorized

    async def _handle_model_command(self, event):
        self.commands.append(event.text)
        if self.works:
            self._session_model_overrides["K"] = {"model": "gemma4:12b", "base_url": "http://127.0.0.1:11434/v1"}
            return "Switched to gemma4:12b"
        return "❌ unknown alias"


def _run(p, gw, text):
    return asyncio.run(p.on_gateway_dispatch(event=Event(text), gateway=gw))


def test_locked_message_switches_once():
    p, gw = _plugin(), FakeGateway()
    assert _run(p, gw, "new LOCKED project DEMO2") is None          # the message still runs
    assert gw.commands == ["/model local"] and p._switched["K"] == "switched"
    assert _run(p, gw, "another LOCKED project") is None                 # already local: no second switch
    assert gw.commands == ["/model local"] and p._switched["K"] == "already"


def test_no_switch_for_open_unauthorised_or_failed():
    p = _plugin()
    gw = FakeGateway()
    _run(p, gw, "new OPEN project X")
    assert gw.commands == []
    gw = FakeGateway(authorized=False)
    _run(p, gw, "new LOCKED project X")
    assert gw.commands == []
    gw = FakeGateway(works=False)
    _run(p, gw, "new LOCKED project X")
    assert p._switched["K"].startswith("failed")


RES = {"project": "DEMO3", "path": "/data/projects/DEMO3", "mode": "LOCKED",
       "inventory": "EVPN_LAB", "ticket_id": None, "created": True, "notes": []}


@pytest.mark.parametrize("wrap", [
    lambda d: d, lambda d: json.dumps(d), lambda d: json.dumps({"result": json.dumps(d)}),
    lambda d: [{"type": "text", "text": json.dumps(d)}],
])
def test_result_unwrapped(wrap):
    assert _plugin()._result_dict(wrap(RES))["path"] == RES["path"]


def test_card_locked_local_and_cloud():
    p = _plugin()
    local = p.card(RES, {"model": "gemma4:12b", "base_url": "http://127.0.0.1:11434/v1"}, "switched")
    assert "DEMO3" in local and "/data/projects/DEMO3" in local and "LOCAL" in local
    assert "gemma4:12b" in local and "(local)" in local and "automatically" in local and "⚠️" not in local
    cloud = p.card(RES, {"model": "google/gemini-3.8-flash", "base_url": "https://aiplatform.googleapis.com/v1"},
                   None)
    assert "(cloud)" in cloud and "⚠️" in cloud and "/model local" in cloud
    opened = p.card({**RES, "mode": "OPEN", "created": False}, {"model": "gemini", "base_url": "https://x"}, None)
    assert "CLOUD" in opened and "(opened)" in opened and "⚠️" not in opened


def test_card_posted_after_project_create():
    p = _plugin()
    posted = []
    p._ctx = type("C", (), {"post_message": lambda self, t: posted.append(t)})()
    p.on_api_request(session_id="S", model="gemma4:12b", base_url="http://127.0.0.1:11434/v1")
    p.on_post_tool_call("mcp__navigator__project", {"action": "create"}, json.dumps(RES), session_id="S")
    p.on_post_tool_call("mcp__navigator__project", {"action": "list"}, json.dumps(RES), session_id="S")
    p.on_post_tool_call("mcp__navigator__device", {"action": "create"}, json.dumps(RES), session_id="S")
    assert len(posted) == 1 and "gemma4:12b" in posted[0]
