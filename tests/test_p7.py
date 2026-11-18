"""P7: NAVIGATOR — projects, mode changes with approval, roles / QC / lessons, skills, and the scrambler
(default and deep modes) on LOG-SANITIZER's multi-vendor samples. No network, no devices."""
from __future__ import annotations

import difflib
import json
import re
import shutil
import stat
from pathlib import Path

import pytest
from fastmcp import Client

from navigator_engineer import roles, skills_store
from navigator_engineer.config import Settings
from navigator_engineer.modes import ModeChanger
from navigator_engineer.projects import ProjectError, Projects
from navigator_engineer.scrambler import Scrambler, ScrambleError
from navigator_engineer.scrambler import engine as E
from rudder_devices import approval

SAMPLES = Path(__file__).resolve().parent / "scrambler_samples"
SECRETS = [
    "0822455D0A16544541", "$9$", "$1$Xf2c", "Gl0bexVPNkey", "AwsLocalSecret123", "AwsRemoteSecret456",
    "104D000A061843595F", "0300540A0A1F0E18", "1511021F07257A767B", "01100F175804", "05080F1C22431F5B4A",
    "Gl0bexRO", "Gl0bexRW", "AuthPassw0rd", "PrivPassw0rd", "Gl0bexTRAP", "0822455D0A165445415F59",
    "13061E010803557878", "08314D5D1A0E0A0516", "060506324F41", "30820330",
    "Wxyz1234abcd", "Qrst5678efgh", "0x8a1f6d2b", "9125d59c18a9b015", "d3b1d6dbd6a5ff5d", "$5$",
    "$sha512$", "2KFQnbNIdI", "Ld3SHMbrfnGZq", "Gl0bexTacKey", "AcmeL2LPsk2026", "AcmeRemotePsk",
    "AcmeLocalPsk", "Gl0bexASAcomm", "Gl0bexASAro", "Fa1loverK3y", "308204a8", "$6$", "$8$",
    "070C285F4D06485744", "0207165218120E", "Gl0bexEOSro", "Gl0bexEOStrap", "Vc1ZQf8YbJ7mU0nN3pQk2g",
    "Gl0bexPANro", "MIIEowIBAAKCAQEA", "SH2nlSm9", "SH2xYzAb", "Gl0bexFGTcomm", "7cW1Zb3tfA9yVd",
    "XXAbCdEfGh", "kj3d9DLkGHvbn", "Zm9yd2FyZEJHUHBhc3N3b3JkMTIzNA",
]
IDENTIFIERS = [
    "nyc-core-rtr1", "nyc-core-sw1", "nyc-edge-fw1", "vedge-nyc-01", "dal-leaf-01", "dal-spine-01",
    "nyc-pa-fw01", "chi-branch-fgt01", "globex", "acme", "jsmith", "mlopez", "ktanaka", "rpatel", "dchen",
    "bwilson", "vpnuser-jdoe", "Jane Smith", "Bob Wilson", "Liberty Plaza", "Lumen", "Spectrum", "Comcast",
    "Cogent", "Wacker", "Dallas", "PAYROLL-SERVERS", "TRADING-FLOOR", "Row 4 Rack", "IDF-3", "CHG004411",
    "FDO2531M0XY", "FDO23461ABC", "JAD2241081X", "012801099999", "FGT60FTK21012345", "ISE-NYC-01",
    "64.86.33.", "52.95.110.", "8.8.8.8", "129.6.15.28", "93.184.216.", "185.220.101.", "71.71.10.",
    "24.1.88.", "2600:1f18", "0050.56a1.b2c3", "7483.ef12.3456", "00:1b:17:00:aa:01", "04:d5:90:12:34:56",
]
PUBLIC_ASNS = [r"\b3356\b", r"\b701\b"]
PLACEHOLDER = re.compile(r"<(?:PASSWORD|KEY|PSK|SNMP-COMMUNITY|TOKEN|PEM-REDACTED|CERTIFICATE-REDACTED)")


class FakeChannel:
    def __init__(self, tmp):
        self.sent = []
        self.ch = approval.HermesChannel({"APPROVAL_DIR": str(tmp / "appr"), "PROFILES": {"NAVI": "navi"},
                                          "HERMES_BIN": "/bin/true"}, "NAVI", runner=self._run)
        self.dir = tmp / "appr"

    def _run(self, argv, input=None, **kw):
        self.sent.append(input)
        return type("R", (), {"returncode": 0})()

    def decide(self, verdict="approved"):
        code = re.search(r"/pushok (\w+)", self.sent[-1]).group(1)
        (self.dir / approval.code_hash("decision", code)).write_text(verdict)
        (self.dir / approval.code_hash("pending", code)).unlink()


@pytest.fixture()
def nav(tmp_path):
    ws = tmp_path / "a projects folder"
    (ws / "ADHOC_WORK").mkdir(parents=True)
    s = Settings(workspace=ws, state_path=tmp_path / "state", default_inventory="INVENTORY_LAB",
                 extra_skills=(tmp_path / "myskills",), approval={"TTL_MIN": 30})
    return {"s": s, "p": Projects(s), "tmp": tmp_path, "ws": ws}


# ============================================================================ projects
def test_project_create_reuse_and_list(nav):
    r = nav["p"].create("LOCKED", "BGP flap at DC1", name="JUD_TEST")
    pdir = Path(r["path"])
    assert r["created"] and r["mode"] == "LOCAL" and r["inventory"] == "INVENTORY_LAB"
    for f in ("TASK.md", "RUNLOG.md", "NAVIGATOR_STATE.json", ".navigator_mode", ".navigator_inventory", "RAW", "OUT",
              "RECORDS"):
        assert (pdir / f).exists()
    assert "MODE: LOCAL" in (pdir / "TASK.md").read_text() and "BGP flap at DC1" in (pdir / "TASK.md").read_text()
    (pdir / "TASK.md").write_text("mine")
    again = nav["p"].create("OPEN", "again", name="JUD_TEST")
    assert again["mode"] == "LOCAL" and not again["created"] and "not changed" in again["notes"][0]
    assert (pdir / "TASK.md").read_text() == "mine"                       # never overwritten
    adhoc = nav["p"].create("OPEN", "quick check of leafs")
    assert "/ADHOC_WORK/QUICK_CHECK_OF_LEAFS_" in adhoc["path"].replace("\\", "/")
    names = {x["project"]: x for x in nav["p"].list()}
    assert names["JUD_TEST"]["mode"] == "LOCAL" and any(x["adhoc"] for x in names.values())


def test_project_status_record_and_names(nav):
    nav["p"].create("OPEN", "x", name="P1")
    st = nav["p"].status("P1", "waiting_input", phase="collect", ask="Which VRF?")
    assert st["status"] == "waiting_input"
    with pytest.raises(ProjectError, match="needs the question"):
        nav["p"].status("P1", "waiting_input")
    nav["p"].record("P1", "state_validator", "ran show bgp", "OK records/run1")
    log = (nav["ws"] / "P1" / "RUNLOG.md").read_text()
    assert "-- STATE_VALIDATOR" in log and "ASK: Which VRF?" in log
    for bad in ("../etc", "a/b", "ADHOC_WORK", ""):
        with pytest.raises(ProjectError):
            nav["p"].find(bad)


def test_mode_change_needs_approval(nav):
    nav["p"].create("LOCKED", "x", name="P2")
    fc = FakeChannel(nav["tmp"])
    mc = ModeChanger(nav["s"], channel=fc.ch)
    r = mc.set("P2", "OPEN")
    assert r["state"] == "waiting_approval" and r["mode"] == "LOCAL" and "/pushok" in fc.sent[-1]
    assert mc.set("P2", "OPEN")["state"] == "waiting_approval"            # not approved yet: still LOCKED
    fc.decide()
    r = mc.set("P2", "OPEN")
    assert r["changed"] and r["mode"] == "CLOUD" and nav["p"].mode(nav["ws"] / "P2") == "CLOUD"
    assert mc.set("P2", "LOCKED")["mode"] == "LOCAL"                       # back to LOCKED: no approval
    mc.set("P2", "OPEN")
    fc.decide("rejected")
    assert mc.set("P2", "OPEN")["state"] == "rejected" and nav["p"].mode(nav["ws"] / "P2") == "LOCAL"


# ============================================================================ roles, QC, lessons, skills
def test_roles_brief_qc_lessons(nav):
    names = {r["role"] for r in roles.role_list()}
    assert names == {"ANALYZER", "CONFIG_GENERATOR", "CONFIG_PUSHER", "DATA_COLLECTOR", "STATE_VALIDATOR",
                     "LOG_REVIEWER", "DOCUMENTOR", "SANITY_AGENT", "SHIP_ENGINEER"}      # 9th role: P7d
    for r in roles.roles().values():                     # self-contained: no host paths, no nv-* scripts
        assert not re.search(r"/data|a skills folder|a projects folder|the private data folder|\bnv-[a-z]+", r["body"]), r["path"]
    nav["p"].create("LOCKED", "x", name="P3")
    b = roles.role_brief(nav["s"], "config_push", "add Loopback10 on LEAF1", project="P3")
    assert b["role"] == "CONFIG_PUSHER" and "change_propose" in b["brief"] and "MODE: LOCAL" in b["brief"]
    assert "add Loopback10 on LEAF1" in b["brief"]
    assert len(roles.qc_check("ANALYZER", "memo")["checks"]) == 8
    roles.qc_record(nav["s"], "P3", "memo", "ANALYZER", "REJECT", ["no citation for claim 2"])
    assert "REJECT: no citation" in (nav["ws"] / "P3" / "RUNLOG.md").read_text()
    with pytest.raises(roles.RoleError, match="findings"):
        roles.qc_record(nav["s"], "P3", "memo", "ANALYZER", "REJECT")
    with pytest.raises(roles.RoleError, match="generic"):
        roles.lesson_add(nav["s"], "ANALYZER", "bad", "technical", "peer 10.1.1.1 was wrong", "check", project="P3")
    roles.lesson_add(nav["s"], "ANALYZER", "Cite versions", "technical", "fix quoted for the wrong release",
                     "always quote the release the doc covers", project="P3")
    text = roles.lessons_list(nav["s"], "ANALYZER")
    assert "Source project: LOCAL project" in text and "Cite versions" in text
    assert stat.S_IMODE(nav["s"].lessons_file.stat().st_mode) == 0o600


def test_skills_and_export(nav):
    mine = nav["tmp"] / "myskills" / "MY-SKILL"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: my-skill\ndescription: user's own skill\n---\nbody\n")
    found = {x["skill"]: x for x in skills_store.skill_list(nav["s"])}
    assert found["navigator-scrambler"]["source"] == "navigator" and found["MY-SKILL"]["source"] == "extra"
    assert "default" in skills_store.skill_load(nav["s"], "navigator-scrambler")
    with pytest.raises(KeyError):
        skills_store.skill_load(nav["s"], "../../etc/passwd")
    out = roles.export_skills(nav["tmp"] / "hermes-skills")
    assert len(out) == 10 and all(Path(p).name == "SKILL.md" for p in out)        # 9 roles + scrambler
    assert "generated_by: navigator" in Path(out[0]).read_text()
    roles.export_skills(nav["tmp"] / "hermes-skills")                       # re-export over its own files: ok
    foreign = nav["tmp"] / "other" / "navigator-analyzer"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("hand written")
    with pytest.raises(roles.RoleError, match="not overwritten"):
        roles.export_skills(nav["tmp"] / "other")


# ============================================================================ scrambler
@pytest.fixture()
def scr(nav):
    nav["p"].create("OPEN", "scrambler test", name="TESTPROJ")
    shutil.copytree(SAMPLES, nav["ws"] / "TESTPROJ" / "RAW", dirs_exist_ok=True)
    return {**nav, "sc": Scrambler(nav["s"]), "pdir": nav["ws"] / "TESTPROJ"}


def _tree(d: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.iterdir()) if not p.name.startswith("_")}


def test_default_mode_removes_secrets_only(scr):
    r = scr["sc"].sanitize("TESTPROJ", mode="default")
    assert r["verify"] == "PASS", r["findings"]
    out = _tree(Path(r["output"]))
    blob = "\n".join(out.values()).lower()
    leaks = [s for s in SECRETS if s.lower() in blob]
    assert not leaks, leaks
    kept = [s for s in ("nyc-core-sw1", "8.8.8.8", "jsmith", "64.86.33.", "FDO2531M0XY") if s.lower() in blob]
    assert len(kept) == 5, kept                                             # identifiers stay as they are
    raw = _tree(scr["pdir"] / "RAW")
    for name, orig in raw.items():                                          # only secret lines differ
        for ln in difflib.unified_diff(orig.splitlines(), out[name].splitlines(), lineterm="", n=0):
            if ln.startswith("+") and not ln.startswith("+++"):
                assert PLACEHOLDER.search(ln), (name, ln)
    assert not (scr["s"].maps_path / "TESTPROJ" / "map.json").exists()      # nothing to restore, no map
    assert "secrets in these files were removed" in (Path(r["output"]) / "_MODEL_NOTE.txt").read_text()


def test_deep_mode_scrambles_identifiers_and_restores(scr):
    r = scr["sc"].sanitize("TESTPROJ", mode="deep", terms=["Globex", "ACME"])
    assert r["verify"] == "PASS", r["findings"]
    out = _tree(Path(r["output"]))
    blob = "\n".join(out.values())
    leaks = [s for s in SECRETS + IDENTIFIERS if s.lower() in blob.lower()]
    leaks += [rx for rx in PUBLIC_ASNS if re.search(rx, blob)]
    assert not leaks, leaks
    mp_file = scr["s"].maps_path / "TESTPROJ" / "map.json"
    assert stat.S_IMODE(mp_file.stat().st_mode) == 0o600
    mp = E.Mapping(str(mp_file))
    fake_sw = mp.d["entries"]["host"]["nyc-core-sw1"]["fake"]
    fake_ip = mp.d["ipv4"]["64.86.33"] + ".1"
    ans = f"Root cause: {fake_sw.lower()} lost its uplink; BGP peer {fake_ip} went down. Rotate <PASSWORD type=7>."
    back = scr["sc"].restore("TESTPROJ", text=ans)
    for want in ("nyc-core-sw1", "64.86.33.1", "<PASSWORD type=7>"):
        assert want in back["text"]
    # round trip on files: every line is back, except where a secret placeholder stands
    res = scr["sc"].restore("TESTPROJ", inp=str(Path(r["output"]).relative_to(scr["pdir"])))
    restored = _tree(Path(res["output"]))
    for name, orig in _tree(scr["pdir"] / "RAW").items():
        for ln in difflib.unified_diff(orig.splitlines(), restored[name].splitlines(), lineterm="", n=0):
            if ln.startswith("+") and not ln.startswith("+++"):
                assert PLACEHOLDER.search(ln), (name, ln)
    # stable pseudonyms: a second run gives the same output
    r2 = scr["sc"].sanitize("TESTPROJ", mode="deep")
    assert _tree(Path(r2["output"])) == out
    assert scr["sc"].map_counts("TESTPROJ")["entries"]["host"] > 3


def test_deep_strict_and_text_forms(scr):
    r = scr["sc"].sanitize("TESTPROJ", mode="deep", level="strict", terms=["Globex", "ACME"])
    assert r["verify"] == "PASS"
    blob = "\n".join(_tree(Path(r["output"])).values()).lower()
    assert not [s for s in ("payroll-db", "chi-printers", "sftp-01", "outside_in") if s in blob]
    t = scr["sc"].sanitize_text("hostname edge-r1\nsnmp-server community S3cretComm RO\n", mode="default")
    assert t["verify"] == "PASS" and "S3cretComm" not in t["text"] and "edge-r1" in t["text"]
    with pytest.raises(ScrambleError, match="needs a project"):
        scr["sc"].sanitize_text("x", mode="deep")
    d = scr["sc"].sanitize_text("hostname edge-r1\nip route 0.0.0.0 0.0.0.0 8.8.8.8\n", mode="deep", project="TESTPROJ")
    assert "8.8.8.8" not in d["text"] and "edge-r1" not in d["text"]
    with pytest.raises(ScrambleError, match="outside"):
        scr["sc"].sanitize("TESTPROJ", inp="../../etc")


def test_restore_in_locked_project_stays_in_files(scr):
    scr["sc"].sanitize("TESTPROJ", mode="deep")
    (scr["pdir"] / ".navigator_mode").write_text("LOCKED\n")
    r = scr["sc"].restore("TESTPROJ", text="DC1-SW1 is down")
    assert r["text"] is None and Path(r["output"], "answer_restored.txt").is_file()


async def test_navigator_tools_listed(nav, monkeypatch, tmp_path):
    boot = tmp_path / "navigator.yml"
    boot.write_text(f"WORKSPACE: {nav['ws']}\nSTATE_PATH: {tmp_path / 'st'}\n")
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(boot))
    from navigator_engineer.server import mcp
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        r = await c.call_tool("project", {"action": "create", "mode": "OPEN", "description": "tool test",
                                          "project": "TT"})
        assert json.loads(r.content[0].text)["project"] == "TT"
    assert {"project", "mode", "role", "qc", "lesson", "scrambler", "skill"} <= names     # P7d: grouped tools
