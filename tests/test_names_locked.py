"""Project file names (NAVIGATOR_* for new projects, NAVI-era names still honoured), device reads in a LOCKED
project with a cloud host model (record paths only), and scrambler sanitize name=."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from navigator_engineer import config
from navigator_engineer.projects import Projects
from navigator_engineer.scrambler import Scrambler, ScrambleError


def _s(tmp_path, host="cloud"):
    ws = tmp_path / "ws"
    (ws / "ADHOC_WORK").mkdir(parents=True, exist_ok=True)
    return config.Settings(workspace=ws, state_path=tmp_path / "state", default_inventory="INVENTORY_LAB",
                           models={"HOST": host})


def test_new_projects_use_navigator_names(tmp_path):
    p = Projects(_s(tmp_path))
    pdir = tmp_path / "ws" / "NEWJOB"
    p.create("LOCKED", "a job", name="NEWJOB")
    for f in (".navigator_mode", ".navigator_inventory", "NAVIGATOR_STATE.json"):
        assert (pdir / f).is_file(), f
    for f in (".navi_mode", ".navi_inventory", "NAVI_STATE.json"):
        assert not (pdir / f).exists(), f
    p.status("NEWJOB", "done")
    assert json.loads((pdir / "NAVIGATOR_STATE.json").read_text())["status"] == "done"
    assert "NAVIGATOR_STATE.json" not in p.show("NEWJOB")["user_files"]
    assert "navi_mode" not in (pdir / "TASK.md").read_text()


def test_navi_era_projects_keep_their_files(tmp_path):
    s = _s(tmp_path)
    pdir = s.workspace / "OLDJOB"
    for sub in ("RAW", "OUT"):
        (pdir / sub).mkdir(parents=True)
    (pdir / ".navi_mode").write_text("OPEN\n")
    (pdir / ".navi_inventory").write_text("INVENTORY_OLD\n")
    (pdir / "NAVI_STATE.json").write_text(json.dumps({"incident": "OLDJOB", "status": "active"}))
    (pdir / "TASK.md").write_text("# old\n")
    p = Projects(s)
    r = p.create("LOCKED", "reopen", name="OLDJOB")
    assert r["mode"] == "CLOUD" and r["inventory"] == "INVENTORY_OLD" and not r["created"]
    p.status("OLDJOB", "done")
    p.set_mode_now("OLDJOB", "LOCKED")
    assert (pdir / ".navi_mode").read_text().strip() == "LOCKED"
    assert json.loads((pdir / "NAVI_STATE.json").read_text())["status"] == "done"
    for f in (".navigator_mode", ".navigator_inventory", "NAVIGATOR_STATE.json"):
        assert not (pdir / f).exists(), f            # nothing renamed or duplicated
    assert [x["project"] for x in p.list()] == ["OLDJOB"]
    assert "NAVI_STATE.json" not in p.show("OLDJOB")["user_files"]


READ = {"run": "20261116-164317-675e", "records": "/ws/JOB/RECORDS/DEVICES/20261116-164317-675e",
        "run_file": "/ws/JOB/RECORDS/DEVICES/20261116-164317-675e/RUN.json", "counts": {"ok": 1}, "seconds": 1.2,
        "devices": [{"device": "QALEAF1A", "platform": "EOS", "adapter": "eapi", "status": "ok", "seconds": 1.1,
                     "results": [{"command": "show running-config", "ok": True, "format": "text",
                                  "output": "hostname QALEAF1A\nusername admin secret sha512 $6$abc",
                                  "truncated": False, "error": None,
                                  "record": "/ws/JOB/RECORDS/DEVICES/x/QALEAF1A/01_show-running-config.txt"}],
                     "debug": "connected to 192.0.2.1"}]}
FACTS = {"device": "QALEAF1A", "status": "ok", "hostname": "QALEAF1A", "serial": "JPE123", "model": "vEOS",
         "records": "/ws/JOB/RECORDS/DEVICES/y"}


@pytest.mark.parametrize("mode,host,hidden", [("LOCKED", "cloud", True), ("LOCKED", "local", False),
                                              ("OPEN", "cloud", False)])
def test_device_output_withheld_only_for_locked_on_cloud(tmp_path, mode, host, hidden):
    from navigator_engineer import server
    sc = SimpleNamespace(locked=mode == "LOCKED", s=_s(tmp_path, host), project="JOB",
                         hidden=mode == "LOCKED" and host != "local")
    r = server._withhold(json.loads(json.dumps(READ)), sc)
    text = json.dumps(r)
    assert ("hostname QALEAF1A" in text) is (not hidden)
    assert ("192.0.2.1" in text) is (not hidden)
    res = r["devices"][0]["results"][0]
    assert res["record"].endswith("01_show-running-config.txt") and res["ok"] is True
    assert r["records"] == READ["records"] and r["counts"] == {"ok": 1}
    if hidden:
        assert r["withheld"] == ["debug", "output"]
        assert "scrambler action=sanitize" in r["next"] and "local_model action=ask" in r["next"]
    else:
        assert "withheld" not in r


def test_facts_and_compare_withheld(tmp_path):
    from navigator_engineer import server
    sc = SimpleNamespace(locked=True, s=_s(tmp_path), project="JOB", hidden=True)
    f = server._withhold(dict(FACTS), sc)
    assert "JPE123" not in json.dumps(f) and f["records"] == FACTS["records"] and f["status"] == "ok"
    cmp = {"device": "QALEAF1A", "inventory": "I", "methods": [{"via": "ssh", "status": "ok", "results": []}],
           "compare": [{"command": "show version", "between": ["ssh", "eapi"], "same": False,
                        "diff": "-serial JPE123\n+serial JPE124"}]}
    c = server._withhold(cmp, sc)
    assert "JPE12" not in json.dumps(c) and c["compare"][0]["same"] is False and "diff" in c["withheld"]


def test_device_tool_hides_outputs_in_a_locked_project(tmp_path, monkeypatch):
    """The real tool: a LOCKED project on a cloud host gets status and record paths back, not the config."""
    from navigator_engineer import server
    s = _s(tmp_path)
    monkeypatch.delenv("NAVIGATOR_HOST_MODEL", raising=False)
    monkeypatch.setattr(server, "_s", lambda: s)
    Projects(s).create("LOCKED", "test", name="JOB", inventory="INVENTORY_LAB")

    class FakeRudder:
        def read(self, *a, **k):
            return json.loads(json.dumps(READ))
    monkeypatch.setattr(server.Scope, "rudder", lambda self: FakeRudder())
    fn = getattr(server.device, "fn", server.device)
    r = fn("read", devices=["QALEAF1A"], commands=["show running-config"], project="JOB")
    assert "secret" not in json.dumps(r) and r["devices"][0]["results"][0]["record"] and "output" in r["withheld"]


def test_sanitize_one_record_with_a_name(tmp_path):
    s = _s(tmp_path)
    Projects(s).create("LOCKED", "test", name="JOB")
    pdir = s.workspace / "JOB"
    rec = pdir / "RECORDS" / "DEVICES" / "r1" / "QALEAF1A"
    rec.mkdir(parents=True)
    (rec / "01_show-running-config.txt").write_text(
        "hostname QALEAF1A\nusername admin privilege 15 secret sha512 $6$Wxyz1234abcd/xyz\n")
    sc = Scrambler(s)
    r = sc.sanitize("JOB", str(rec / "01_show-running-config.txt"), name="QALEAF1A_SANITIZED.txt")
    out = pdir / "SANITIZED"
    files = list(out.rglob("QALEAF1A_SANITIZED.txt"))
    assert len(files) == 1 and r["files"] == 1 and "text" not in r
    body = files[0].read_text()
    assert "Wxyz1234abcd" not in body and "hostname QALEAF1A" in body
    with pytest.raises(ScrambleError):
        sc.sanitize("JOB", "RECORDS", name="X.txt")             # a folder: name not allowed
    with pytest.raises(ScrambleError):
        sc.sanitize("JOB", str(rec / "01_show-running-config.txt"), name="../X.txt")
