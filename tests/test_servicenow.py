"""Tasks 18 + 19 / decision #159: ServiceNow link only and gate, against a mocked Table API (no PDI)."""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from conftest import SECRET
from rudder_devices import servicenow as SN
from rudder_devices.changes import Changes
from rudder_devices.config import Settings
from rudder_devices.core import RudderError
from test_p3 import rig  # noqa: F401
from test_p4 import ch as _ch, propose, _decide  # noqa: F401


def utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


class FakeSnow:
    """change_request + incident tables, PATCH, and the attachment API; basic auth rudder / SECRET."""

    def __init__(self):
        now = time.time()
        self.tables = {"change_request": {
            "a1": {"sys_id": "a1", "number": "CHG0030001", "short_description": "Loopback10 for QA",
                   "state": "-1", "type": "normal", "approval": "approved",
                   "start_date": utc(now - 600), "end_date": utc(now + 3600)}},
            "incident": {"i1": {"sys_id": "i1", "number": "INC0010001", "short_description": "link down",
                                "state": "2", "type": "", "approval": "not requested",
                                "start_date": "", "end_date": ""}}}
        self.patches, self.attachments, self.calls, self.down = [], [], 0, False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("down")
        user, _, pw = base64.b64decode(req.headers["authorization"].split()[1]).decode().partition(":")
        if (user, pw) != ("rudder", SECRET):
            return httpx.Response(401)
        path = req.url.path.removeprefix("/api/now/")
        if req.method == "GET" and path.startswith("table/"):
            table = path.split("/")[1]
            if "sysparm_query" not in req.url.params:                     # setup's ping
                return httpx.Response(200, json={"result": [{"sys_id": "a1"}]})
            assert req.url.params["sysparm_display_value"] == "false"
            num = req.url.params["sysparm_query"].removeprefix("number=")
            rows = [r for r in self.tables.get(table, {}).values() if r["number"] == num]
            return httpx.Response(200, json={"result": rows})
        if req.method == "PATCH":
            _, table, sid = path.split("/")
            body = json.loads(req.content)
            self.patches.append((table, sid, body))
            self.tables[table][sid].update({k: v for k, v in body.items() if k != "work_notes"})
            return httpx.Response(200, json={"result": self.tables[table][sid]})
        if req.method == "POST" and path == "attachment/file":
            self.attachments.append((dict(req.url.params), req.content.decode()))
            return httpx.Response(201, json={"result": {"sys_id": "att1"}})
        return httpx.Response(404)

    def cr(self, **kw):
        self.tables["change_request"]["a1"].update(kw)


@pytest.fixture()
def sn(_ch, rig, monkeypatch):  # noqa: F811
    creds = rig["tmp"] / "NAVI.DATA" / "CREDENTIALS.yml"
    creds.write_text(creds.read_text() + f"- {{TAG: SNOW, USERNAME: rudder, PASSWORD: {SECRET}}}\n")
    fake = FakeSnow()

    def make(mode="link", **extra):
        s = Settings(**{**_ch["s"].__dict__, "servicenow": {"MODE": mode, "URL": "https://dev00001.service-now.com",
                                                            "CREDENTIAL": "SNOW", **extra}})
        c = Changes(s, factories=_ch["fac"], channel=_ch["chan"])
        c.sn_transport = httpx.MockTransport(fake.handler)
        return {**_ch, "c": c, "s": s, "fake": fake}
    return make


# ------------------------------------------------------------------ settings
def test_settings():
    s = type("S", (), {"servicenow": None})()
    assert SN.settings(s)["MODE"] == "off" and not SN.enabled(s)
    s.servicenow = {"MODE": "gate", "URL": "http://x.service-now.com", "CREDENTIAL": "SNOW"}
    with pytest.raises(SN.ServiceNowError, match="https"):
        SN.settings(s)
    assert SN.enabled(s)                      # a broken setting never turns the gate off silently
    s.servicenow = {"MODE": "magic"}
    with pytest.raises(SN.ServiceNowError):
        SN.settings(s)


def test_off_by_default_touches_nothing(_ch):  # noqa: F811
    st = propose(_ch, ticket_id="CHG0030001")
    assert st["state"] == "waiting_approval" and "servicenow" not in st


# ------------------------------------------------------------------ task 18: link only
def test_link_writes_diff_hash_then_result_and_close_code(sn):
    x = sn("link")
    st = propose(x, ticket_id="CHG0030001")
    f = x["fake"]
    assert st["servicenow"]["number"] == "CHG0030001" and st["servicenow"]["written"] == ["diff", "hash"]
    (params, body), = f.attachments
    assert params["table_name"] == "change_request" and params["table_sys_id"] == "a1"
    t = json.loads((x["s"].state_path / "tickets" / f"{st['ticket']}.json").read_text())
    assert t["hash"] in body and "+interface Loopback10" in body
    assert "work_notes" in f.patches[0][2] and t["hash"] in f.patches[0][2]["work_notes"]
    assert "ServiceNow CHG0030001: diff and hash attached" in x["sent"][-1]["message"]
    blob = json.dumps(f.patches) + json.dumps(f.attachments) + json.dumps(st)
    assert SECRET not in blob and SECRET not in json.dumps(t)
    _decide(x)
    done = x["c"].apply(st["ticket"])
    assert done["state"] == "applied"
    last = f.patches[-1][2]
    assert last["close_code"] == "successful" and "APPLIED" in last["work_notes"] and "state" not in last
    assert done["servicenow"]["closed"] == {"close_code": "successful"}


def test_link_never_blocks_when_servicenow_is_down(sn):
    x = sn("link")
    x["fake"].down = True
    st = propose(x, ticket_id="CHG0030001")
    assert st["state"] == "waiting_approval" and "not reachable" in st["servicenow"]["error"]
    assert "NOT updated" in x["sent"][-1]["message"]
    _decide(x)
    done = x["c"].apply(st["ticket"])
    assert done["state"] == "applied"


def test_link_reads_incidents_and_unknown_numbers(sn):
    x = sn("link")
    st = propose(x, ticket_id="INC0010001")
    assert st["servicenow"]["number"] == "INC0010001" and x["fake"].attachments[0][0]["table_name"] == "incident"
    x["c"].cancel(st["ticket"])
    st = propose(x, ticket_id="CHG0099999")
    assert "not found" in st["servicenow"]["error"]


def test_link_failed_apply_closes_unsuccessful(sn, monkeypatch):
    x = sn("link")
    st = propose(x, ticket_id="CHG0030001")
    _decide(x)

    def boom(t, d):
        d["state"] = "rolled_back"
        raise RudderError("verify failed")
    monkeypatch.setattr(x["c"], "_apply_one", boom)
    done = x["c"].apply(st["ticket"])
    last = x["fake"].patches[-1][2]
    assert done["state"] == "failed" and last["close_code"] == "unsuccessful" and "FAILED" in last["work_notes"]


def test_close_state_is_optional(sn):
    x = sn("link", CLOSE_STATE="0")
    st = propose(x, ticket_id="CHG0030001")
    _decide(x)
    x["c"].apply(st["ticket"])
    assert x["fake"].patches[-1][2]["state"] == "0"


def test_setup_saves_only_after_the_test(tmp_path, monkeypatch):
    import yaml
    from navigator_engineer import setup as S
    nav = tmp_path / "navigator.yml"
    nav.write_text(f"PROJECT_ROOT: {tmp_path}\nSTATE_PATH: {tmp_path / 'st'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    local = tmp_path / "rudder.local.yml"
    local.write_text("APPROVAL: {METHOD: telegram, BOT: default}\n")
    monkeypatch.setenv("RUDDER_LOCAL", str(local))
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(tmp_path / "rudder.yml"))
    (tmp_path / "rudder.yml").write_text(f"STATE_PATH: {tmp_path / 'rs'}\n")
    pairs = [("SERVICENOW.MODE", "gate"), ("SERVICENOW.URL", "https://dev00001.service-now.com"),
             ("SERVICENOW.CREDENTIAL", "SNOW")]

    def bad(block):
        raise S.SetupError("ServiceNow test failed, nothing saved: refused the login")
    with pytest.raises(S.SetupError, match="nothing saved"):
        S.apply_changes(pairs, check_sn=bad)
    assert "SERVICENOW" not in yaml.safe_load(local.read_text())
    msgs = S.apply_changes(pairs, check_sn=lambda b: "SERVICENOW gate: answered")
    assert yaml.safe_load(local.read_text())["SERVICENOW"] == {"MODE": "gate", "URL": "https://dev00001.service-now.com",
                                                               "CREDENTIAL": "SNOW"}
    assert any(m.startswith("SERVICENOW saved") for m in msgs)
    assert "SERVICENOW.MODE         gate" in S.show()
    assert "SERVICENOW:" in (tmp_path / "st" / "settings.log").read_text()
    with pytest.raises(S.SetupError, match="https"):
        S.apply_changes([("SERVICENOW.URL", "http://dev00001.service-now.com")], check_sn=lambda b: "ok")


def test_setup_test_reads_change_request(sn):
    x = sn("link")
    assert "may read change_request" in SN.test(x["s"], x["c"].atlas, httpx.MockTransport(x["fake"].handler))


# ------------------------------------------------------------------ task 19: gate
def _locks(x):
    return list((x["s"].state_path / "locks").iterdir())


def test_gate_needs_a_change_request_that_can_pass(sn):
    x = sn("gate")
    with pytest.raises(RudderError, match="give ticket_id"):
        propose(x)
    with pytest.raises(RudderError, match="not a change request"):
        propose(x, ticket_id="INC0010001")
    x["fake"].cr(state="4")
    with pytest.raises(RudderError, match="cancelled or rejected"):
        propose(x, ticket_id="CHG0030001")
    x["fake"].cr(state="-1", end_date=utc(time.time() - 60))
    with pytest.raises(RudderError, match="window .* has ended"):
        propose(x, ticket_id="CHG0030001")
    x["fake"].down = True
    with pytest.raises(RudderError, match="not reachable"):
        propose(x, ticket_id="CHG0030001")
    assert not _locks(x) and not [m for m in x["sent"] if "/pushok" in (m["message"] or "")]
    assert x["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]


def test_gate_attaches_first_then_pushok_then_pushes_in_implement(sn, monkeypatch):
    x = sn("gate")
    f = x["fake"]
    at_send = []
    real = x["chan"].run

    def run(argv, **kw):
        at_send.append(len(f.attachments))
        return real(argv, **kw)
    monkeypatch.setattr(x["chan"], "run", run)
    st = propose(x, ticket_id="CHG0030001")
    assert st["state"] == "waiting_approval" and st["servicenow"]["mode"] == "gate"
    assert at_send and at_send[0] == 1                                  # attached BEFORE the approval request
    assert "Gate:" in f.attachments[0][1] and "gate: pushed only in Implement" in x["sent"][-1]["message"]
    t = json.loads((x["s"].state_path / "tickets" / f"{st['ticket']}.json").read_text())
    assert t["hash"] in f.attachments[0][1]
    assert t["expires"] > time.time() + 3000                           # waits until the window's end
    _decide(x)
    f.cr(state="-2")                                                    # back to Scheduled: not yet
    from rudder_devices.changes import GateWait
    with pytest.raises(GateWait, match="not in Implement"):
        x["c"].apply(st["ticket"])
    assert x["c"].status(st["ticket"])["state"] == "approved" and "Implement" in x["c"].status(st["ticket"])["next"]
    from rudder_devices.schedule import Scheduler
    assert Scheduler(x["c"]).on_approval(st["ticket"]) is None           # the scheduler waits, never cancels
    assert x["c"].status(st["ticket"])["state"] == "approved"
    f.cr(state="-1")
    done = x["c"].apply(st["ticket"])
    assert done["state"] == "applied" and "interface Loopback10" in x["sw"].running
    assert f.patches[-1][2]["close_code"] == "successful"


def test_gate_window_start_becomes_the_schedule(sn):
    x = sn("gate")
    start = time.time() + 2 * 3600
    x["fake"].cr(state="-2", start_date=utc(start), end_date=utc(start + 3600))
    st = propose(x, ticket_id="CHG0030001")
    assert st["schedule"]["tz"] == "UTC" and time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)) in st["schedule"]["at_text"]
    x["c"].cancel(st["ticket"])
    with pytest.raises(RudderError, match="outside the planned window"):
        propose(x, ticket_id="CHG0030001", at=utc(start + 7200), timezone="UTC")


def test_gate_cancel_or_reject_cancels_our_change(sn):
    x = sn("gate")
    st = propose(x, ticket_id="CHG0030001")
    assert _locks(x)
    assert x["c"].servicenow_watch() == []
    x["fake"].cr(state="4")
    assert x["c"].servicenow_watch() == []                                # throttled: one read a minute
    out = x["c"].servicenow_watch(min_gap=0)
    assert out == [(st["ticket"], "ServiceNow CHG0030001 was cancelled")]
    assert x["c"].status(st["ticket"])["state"] == "cancelled" and not _locks(x)
    assert "cancelled" in x["fake"].patches[-1][2]["work_notes"]
    x["fake"].cr(state="-1")
    st = propose(x, ticket_id="CHG0030001")
    _decide(x)
    x["fake"].cr(approval="rejected")
    assert x["c"].servicenow_watch(min_gap=0)[0][1].endswith("rejected")
    assert x["c"].status(st["ticket"])["state"] == "cancelled"


def test_gate_scheduler_tick_reports_the_cancel(sn):
    from rudder_devices.schedule import Scheduler
    x = sn("gate")
    st = propose(x, ticket_id="CHG0030001")
    x["fake"].cr(state="4")
    ev = Scheduler(x["c"]).tick()
    assert any(st["ticket"] in e and "cancelled" in e for e in ev)
    assert "CANCELLED" in x["sent"][-1]["message"]


def test_gate_emergency_pushes_now_and_asks_for_approval_afterwards(sn):
    x = sn("gate")
    x["fake"].cr(type="emergency", state="-3", start_date="", end_date="")
    st = propose(x, ticket_id="CHG0030001")
    assert st["servicenow"]["emergency"] is True and "EMERGENCY" in x["sent"][-1]["message"]
    _decide(x)
    done = x["c"].apply(st["ticket"])
    assert done["state"] == "applied"
    assert "must be approved afterwards" in x["fake"].patches[-1][2]["work_notes"]


def test_gate_attach_failure_blocks_before_the_approval(sn, monkeypatch):
    x = sn("gate")
    orig = x["fake"].handler

    def no_attach(req):
        if req.method == "POST":
            return httpx.Response(500)
        return orig(req)
    x["c"].sn_transport = httpx.MockTransport(no_attach)
    with pytest.raises(RudderError, match="could not be attached"):
        propose(x, ticket_id="CHG0030001")
    assert not _locks(x) and not [m for m in x["sent"] if "/pushok" in (m["message"] or "")]


def test_gate_binds_the_change_request_into_the_hash(sn):
    x = sn("gate")
    st = propose(x, ticket_id="CHG0030001")
    p = x["s"].state_path / "tickets" / f"{st['ticket']}.json"
    t = json.loads(p.read_text())
    t["servicenow"]["number"] = "CHG0039999"
    p.write_text(json.dumps(t))
    _decide(x)
    with pytest.raises(RudderError, match="altered"):
        x["c"].apply(st["ticket"])


def test_wrong_login_is_reported_not_leaked(sn, rig):  # noqa: F811
    x = sn("link")
    creds = rig["tmp"] / "NAVI.DATA" / "CREDENTIALS.yml"
    creds.write_text(creds.read_text().replace(f"USERNAME: rudder, PASSWORD: {SECRET}",
                                               "USERNAME: rudder, PASSWORD: wrong-one"))
    st = propose(x, ticket_id="CHG0030001")
    assert "refused the login (HTTP 401)" in st["servicenow"]["error"] and "wrong-one" not in json.dumps(st)
