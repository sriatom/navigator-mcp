"""P4: change tickets, /pushok approval (Hermes protocol), device locks, EOS session writes."""
from __future__ import annotations

import json
import re
import time

import httpx
import pytest
from fastmcp import Client

from conftest import SECRET
from rudder_devices import adapters, approval
from rudder_devices.changes import Changes
from rudder_devices.config import Settings
from rudder_devices.core import RudderError
from test_p3 import rig  # noqa: F401  (fixture: ATLAS link on a temp socket + settings)


class FakeSwitch:
    """Enough of EOS config sessions + commit timer for RUDDER's write path."""

    def __init__(self):
        self.running = ["hostname qaleaf1a", "interface Ethernet1"]
        self.sessions: dict[str, list[str]] = {}
        self.timer: tuple[str, list[str]] | None = None      # (session, running before commit)
        self.saved = False
        self.refuse_second_session = False

    def diff(self, lines):
        return "".join(f"+{ln}\n" for ln in lines if ln not in self.running)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        cmds, fmt = body["params"]["cmds"], body["params"]["format"]
        res, cur = [], None
        for c in cmds:
            if c == "enable":
                res.append({})
                continue
            m = re.match(r"^configure session (\S+)( commit| abort)?$", c)
            if m:
                name, op = m.group(1), (m.group(2) or "").strip()
                if op == "commit":
                    if self.timer and self.timer[0] == name:
                        self.timer = None
                elif op == "abort":
                    if self.timer and self.timer[0] == name:
                        self.running = self.timer[1]
                        self.timer = None
                    self.sessions.pop(name, None)
                else:
                    if self.timer and self.refuse_second_session and name != self.timer[0]:
                        return self._err("cannot open a session while a commit timer is pending")
                    cur = name
                    self.sessions.setdefault(name, [])
                res.append({})
                continue
            if cur and c == "abort":
                self.sessions.pop(cur, None)
                cur = None
                res.append({})
            elif cur and c == "end":
                cur = None
                res.append({})
            elif cur and c.startswith("commit timer"):
                before = list(self.running)
                self.running += [ln for ln in self.sessions.pop(cur) if ln not in self.running]
                self.timer = (cur, before)
                cur = None
                res.append({})
            elif cur:
                self.sessions[cur].append(c.strip())
                res.append({})
            elif c.startswith("show session-config named"):
                name = c.split()[3]
                res.append({"output": f"--- system:/running-config\n+++ session:/{name}-session-config\n"
                                      + self.diff(self.sessions.get(name, []))})
            elif c == "show running-config":
                res.append({"output": "\n".join(self.running) + "\n"})
            elif c == "copy running-config startup-config":
                self.saved = True
                res.append({})
            elif c.startswith("show interfaces Loopback"):
                lo = c.split()[-1]
                if f"interface {lo}" not in self.running:
                    return self._err("Interface does not exist", 1002)
                res.append({"interfaces": {lo: {"description": "rudder-test"}}} if fmt == "json" else {"output": lo})
            elif c == "show version":
                res.append({"version": "4.35"})
            else:
                return self._err(f"invalid command {c}", 1002)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "rudder", "result": res})

    @staticmethod
    def _err(msg, code=1000):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "rudder", "error": {"code": code, "message": msg,
                                                                                         "data": [{"errors": [msg]}]}})


@pytest.fixture()
def ch(rig):  # noqa: F811
    sw = FakeSwitch()
    sent = []

    def runner(argv, input=None, **kw):
        sent.append({"argv": argv, "message": input})
        return type("R", (), {"returncode": 0})()

    appr_dir = rig["tmp"] / "push-approvals"
    chan = approval.HermesChannel({"APPROVAL_DIR": str(appr_dir), "PROFILES": {"NAVI": "navi"},
                                   "HERMES_BIN": "/bin/true"}, "NAVI", runner=runner)
    s = Settings(**{**rig["s"].__dict__, "state_path": rig["tmp"] / "rudder-state",
                    "approval": {"TTL_MIN": 30}})
    (rig["tmp"] / "catalog" / "COMMANDS.yml").write_text(
        "ARISTA_EOS:\n  system: [show version]\n  interfaces: [show interfaces Loopback10]\n")
    fac = {"eapi-writer": lambda: adapters.EosEapiWriter(s, client=lambda **kw: httpx.Client(
        transport=httpx.MockTransport(sw), **kw))}
    c = Changes(s, factories=fac, channel=chan)
    return {"c": c, "sw": sw, "sent": sent, "dir": appr_dir, "s": s, "fac": fac, "chan": chan}


LINES = ["interface Loopback10", "description rudder-test"]


def _code(ch) -> str:
    return re.search(r"/pushok (\w+)", ch["sent"][-1]["message"]).group(1)


def _decide(ch, verdict="approved"):
    """What the push-approve Hermes plugin does on /pushok CODE."""
    code = _code(ch)
    pending = ch["dir"] / approval.code_hash("pending", code)
    assert pending.exists()
    (ch["dir"] / approval.code_hash("decision", code)).write_text(verdict)
    pending.unlink()


def propose(ch, **kw):
    args = {"inventory": "FAB", "devices": ["QALEAF1A"], "lines": LINES, "description": "test loopback"}
    args.update(kw)
    return ch["c"].propose(**args)


def test_propose_approve_apply(ch):
    st = propose(ch, verify_commands=["show interfaces Loopback10"],
                 expect={"show interfaces Loopback10": ["rudder-test"]}, save=True)
    tid = st["ticket"]
    assert st["state"] == "waiting_approval"
    assert st["devices"][0]["diff"] == "+interface Loopback10\n+description rudder-test\n"
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]           # nothing changed yet
    msg = ch["sent"][-1]
    assert msg["argv"][1:6] == ["-p", "navi", "send", "-t", "telegram"] and "/pushok" in msg["message"]
    code = _code(ch)
    ticket_file = (ch["s"].state_path / "tickets" / f"{tid}.json").read_text()
    assert code not in json.dumps(st) and code not in ticket_file and SECRET not in ticket_file
    with pytest.raises(RudderError, match="waiting_approval"):
        ch["c"].apply(tid)
    _decide(ch)
    assert ch["c"].status(tid)["state"] == "approved"
    done = ch["c"].apply(tid)
    assert done["state"] == "applied" and done["devices"][0]["state"] == "applied"
    assert "interface Loopback10" in ch["sw"].running and ch["sw"].timer is None and ch["sw"].saved
    assert list(ch["dir"].iterdir()) == []                                    # approval used once
    assert not list((ch["s"].state_path / "locks").iterdir())                 # lock released
    with pytest.raises(RudderError, match="applied"):
        ch["c"].apply(tid)
    audit = (ch["s"].state_path / "audit.log").read_text()
    assert "proposed QALEAF1A" in audit and "applied QALEAF1A=applied" in audit


def test_verify_failure_rolls_back(ch):
    tid = propose(ch, verify_commands=["show interfaces Loopback10"],
                  expect={"show interfaces Loopback10": ["something-else"]})["ticket"]
    _decide(ch)
    r = ch["c"].apply(tid)
    assert r["state"] == "failed" and r["devices"][0]["state"] == "rolled_back"
    assert "does not show" in r["devices"][0]["error"]
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"] and ch["sw"].timer is None


def test_verify_falls_back_when_second_session_refused(ch):
    ch["sw"].refuse_second_session = True
    tid = propose(ch)["ticket"]
    _decide(ch)
    assert ch["c"].apply(tid)["state"] == "applied"


def test_drift_after_approval_is_refused(ch):
    tid = propose(ch)["ticket"]
    _decide(ch)
    ch["sw"].running.append("interface Loopback10")                          # someone changed the box
    r = ch["c"].apply(tid)
    assert r["state"] == "failed" and r["devices"][0]["state"] == "drifted"
    assert "description rudder-test" not in ch["sw"].running


def test_rejected(ch):
    tid = propose(ch)["ticket"]
    _decide(ch, "rejected")
    assert ch["c"].status(tid)["state"] == "rejected"
    with pytest.raises(RudderError, match="rejected"):
        ch["c"].apply(tid)
    assert not list((ch["s"].state_path / "locks").iterdir())


@pytest.mark.parametrize("bad", ["reload", "username evil secret x", "management api http-commands",
                                 "no aaa authorization exec default", "write memory", "configure terminal",
                                 "bash rm -rf /", "end"])
def test_deny_list(ch, bad):
    with pytest.raises(RudderError, match="deny list"):
        propose(ch, lines=["interface Loopback10", bad])
    assert ch["sent"] == []


def test_production_refused(ch):
    with pytest.raises(RudderError, match="PRODUCTION"):
        propose(ch, devices=["PDLEAF1A"])
    assert not list((ch["s"].state_path / "locks").iterdir())


def test_device_lock(ch):
    tid = propose(ch)["ticket"]
    with pytest.raises(RudderError, match=f"locked by change {tid}"):
        propose(ch, lines=["interface Loopback11"])
    ch["c"].cancel(tid)
    assert ch["c"].status(tid)["state"] == "cancelled"
    assert list(ch["dir"].iterdir()) == []
    propose(ch, lines=["interface Loopback11"])


def test_expiry(ch):
    tid = propose(ch)["ticket"]
    t = json.loads((ch["s"].state_path / "tickets" / f"{tid}.json").read_text())
    t["expires"] = time.time() - 1
    (ch["s"].state_path / "tickets" / f"{tid}.json").write_text(json.dumps(t))
    assert ch["c"].status(tid)["state"] == "expired"
    with pytest.raises(RudderError, match="expired"):
        ch["c"].apply(tid)


def test_no_op_and_send_failure(ch):
    ch["sw"].running += LINES
    with pytest.raises(RudderError, match="change nothing"):
        propose(ch)
    ch["sw"].running = ["hostname qaleaf1a"]
    ch["chan"].run = lambda *a, **k: type("R", (), {"returncode": 3})()
    with pytest.raises(RudderError, match="could not send"):
        propose(ch)
    assert not list((ch["s"].state_path / "locks").iterdir()) and list(ch["dir"].iterdir()) == []


def test_tampered_ticket_refused(ch):
    tid = propose(ch)["ticket"]
    _decide(ch)
    p = ch["s"].state_path / "tickets" / f"{tid}.json"
    t = json.loads(p.read_text())
    t["lines"] = ["interface Loopback10", "shutdown"]
    p.write_text(json.dumps(t))
    with pytest.raises(RudderError, match="altered"):
        ch["c"].apply(tid)


def test_ssh_records_cannot_be_changed_yet(ch):
    with pytest.raises(RudderError, match="over SSH are not built yet"):
        ch["c"].propose("PALAB", ["CORE1"], LINES, "x")


async def test_change_tools_listed(rig, monkeypatch, tmp_path):  # noqa: F811
    from rudder_devices.server import mcp
    boot = tmp_path / "rudder.yml"
    boot.write_text(f"ATLAS_SOCKET: {rig['sock']}\nTOKEN_FILE: {rig['tok']}\nSTATE_PATH: {tmp_path / 'st'}\n")
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(boot))
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"change_propose", "change_status", "change_apply", "change_cancel", "change_list"} <= names
    assert not any("approve" in n for n in names)                    # there is no approve tool
