"""Change validation (decision #158): before the approval (a fail blocks), after the commit (a fail rolls back)."""
from __future__ import annotations

import json

import pytest
import yaml

from navigator_engineer import setup as S
from rudder_devices import validation as V
from rudder_devices.changes import Changes
from rudder_devices.config import Settings
from test_p3 import rig  # noqa: F401
from test_p4 import ch as _ch, propose, _decide  # noqa: F401
from pathlib import Path

class FileBf:
    """A fake Batfish that reads the snapshot files: BAD in an after config (or the flag file) = an undefined
    route-map, so the result is a fail."""

    def __init__(self, flag):
        self.flag, self.text = flag, {}
        outer = self

        class Q:
            def __getattr__(self, name):
                def question(**kw):
                    class A:
                        def answer(self, snapshot, reference_snapshot=None):
                            class R:
                                def frame(self):
                                    bad = snapshot.endswith("-after") and (
                                        "BAD" in outer.text.get(snapshot, "") or outer.flag.exists())
                                    rows = [{"File_Name": "configs/QALEAF1A.cfg", "Struct_Type": "route-map",
                                             "Ref_Name": "RM_BAD", "Context": "bgp"}] if (
                                        bad and name == "undefinedReferences") else []
                                    return Frame(rows)
                            return R()
                    return A()
                return question
        self.q = Q()

    def set_network(self, n):
        pass

    def init_snapshot(self, path, name, overwrite):
        self.text[name] = "".join(f.read_text() for f in sorted((Path(path) / "configs").glob("*.cfg")))

    def delete_snapshot(self, n):
        pass


@pytest.fixture()
def vch(_ch, tmp_path, monkeypatch):  # noqa: F811
    flag = tmp_path / "FAIL_NOW"
    down = {"on": False}

    def session(self):
        if down["on"]:
            raise V.Unreachable("connection refused")
        return FileBf(flag)
    monkeypatch.setattr(V.BatfishValidator, "_session", session)

    def make(down_now=False, **val):
        down["on"] = down_now
        s = Settings(**{**_ch["s"].__dict__, "validation": {"MODE": "batfish", **val}})
        c = Changes(s, factories=_ch["fac"], channel=_ch["chan"])
        return {**_ch, "c": c, "s": s, "flag": flag}
    return make


# ------------------------------------------------------------------ settings and helpers
def test_settings_defaults_and_errors():
    s = type("S", (), {"validation": None})()
    assert V.settings(s)["MODE"] == "none" and not V.enabled(s)
    s.validation = {"MODE": "batfish"}
    cfg = V.settings(s)
    assert cfg["HOST"] == "127.0.0.1" and cfg["ON_UNREACHABLE"] == "block" and cfg["POST_PUSH"] is True
    s.validation = {"MODE": "magic"}
    with pytest.raises(V.ValidationError):
        V.settings(s)
    assert V.enabled(s)                                  # a broken setting never turns validation off


def test_only_private_validators():
    assert V.check_private("127.0.0.1") == "127.0.0.1"
    assert V.check_private("10.1.2.3") == "10.1.2.3"
    with pytest.raises(V.ValidationError, match="public"):
        V.check_private("8.8.8.8")


def test_netclaw_service_from_its_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BATFISH_HOST", raising=False)
    monkeypatch.delenv("BATFISH_PORT", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nexport BATFISH_HOST=10.9.9.9\nBATFISH_PORT='9997'  # coordinator\n")
    assert V.netclaw_service([str(env)]) == ("10.9.9.9", 9997)
    assert V.netclaw_service([str(tmp_path / "none")]) is None
    s = type("S", (), {"validation": {"MODE": "netclaw"}})()
    monkeypatch.setattr(V, "NETCLAW_ENV_FILES", (str(env),))
    monkeypatch.setattr(V, "netclaw_service", lambda files=(str(env),): ("10.9.9.9", 9997))
    assert V.settings(s)["HOST"] == "10.9.9.9" and V.settings(s)["PORT"] == 9997


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, how):
        return list(self.rows)


class FakeBf:
    """Enough of a pybatfish Session: answers by snapshot name."""

    def __init__(self, answers):
        self.answers, self.deleted, self.network = answers, [], None
        outer = self

        class Q:
            def __getattr__(self, name):
                def question(**kw):
                    class A:
                        def answer(self, snapshot, reference_snapshot=None):
                            class R:
                                def frame(self):
                                    key = "after" if snapshot.endswith("-after") else "before"
                                    return Frame(outer.answers.get(name, {}).get(key, []))
                            return R()
                    return A()
                return question
        self.q = Q()

    def set_network(self, n):
        self.network = n

    def init_snapshot(self, path, name, overwrite):
        assert (__import__("pathlib").Path(path) / "configs").is_dir()

    def delete_snapshot(self, n):
        self.deleted.append(n)


def _bf_run(answers, changed=("QALEAF1A",)):
    bf = FakeBf(answers)
    s = type("S", (), {"validation": {"MODE": "batfish"}, "consumer": "NAVI"})()
    r = V.run(s, "before", {"QALEAF1A": "hostname a\n", "QASPINE1": "hostname s\n"},
              {"QALEAF1A": "hostname a\nx\n"}, {"QALEAF1A": "ARISTA_EOS", "QASPINE1": "ARISTA_EOS"},
              list(changed), "C1", session_factory=lambda h, p: bf)
    return r, bf


def test_batfish_pass_and_cleanup():
    r, bf = _bf_run({"fileParseStatus": {"after": [{"File_Name": "configs/QALEAF1A.cfg", "Status": "PASSED"}]}})
    assert r["status"] == "pass" and r["validator"] == "batfish" and bf.network == "rudder-navi"
    assert sorted(bf.deleted) == ["c1-after", "c1-before"]


def test_batfish_new_undefined_reference_fails_old_one_does_not():
    old = {"File_Name": "configs/QALEAF1A.cfg", "Struct_Type": "route-map", "Ref_Name": "RM_OLD", "Context": "bgp"}
    new = {**old, "Ref_Name": "RM_MISSING", "Context": "bgp inbound route-map"}
    r, _ = _bf_run({"undefinedReferences": {"before": [old], "after": [old, new]}})
    assert r["status"] == "fail" and "RM_MISSING" in r["summary"] and "RM_OLD" not in json.dumps(r)


def test_batfish_bgp_break_fails_and_parse_warning_warns():
    sess = {"Node": "qaleaf1a", "VRF": "default", "Remote_IP": "10.0.0.1"}
    r, _ = _bf_run({"bgpSessionCompatibility": {"before": [{**sess, "Configured_Status": "UNIQUE_MATCH"}],
                                                 "after": [{**sess, "Configured_Status": "NO_MATCH_FOUND"}]}})
    assert r["status"] == "fail" and "BGP session to 10.0.0.1" in r["summary"]
    r, _ = _bf_run({"initIssues": {"after": [{"Type": "Parse warning", "Line_Text": "magic on",
                                               "Source_Lines": ["configs/QALEAF1A.cfg:[3]"]}]}})
    assert r["status"] == "warn" and "magic on" in r["summary"]


def test_batfish_unreachable_blocks_or_warns():
    def boom(h, p):
        raise V.Unreachable("connection refused")
    s = type("S", (), {"validation": {"MODE": "batfish"}, "consumer": "NAVI"})()
    args = ("before", {"A": "x"}, {"A": "y"}, {"A": "ARISTA_EOS"}, ["A"], "C2")
    r = V.run(s, *args, session_factory=boom)
    assert r["status"] == "fail" and r.get("unreachable")
    s.validation["ON_UNREACHABLE"] = "warn"
    assert V.run(s, *args, session_factory=boom)["status"] == "warn"


def test_no_candidate_is_skipped():
    s = type("S", (), {"validation": {"MODE": "batfish"}, "consumer": "NAVI"})()
    r = V.run(s, "before", {"FW": "x"}, {"FW": None}, {"FW": "PANOS"}, ["FW"], "C3")
    assert r["status"] == "skipped" and "PANOS" in r["details"][0]


# ------------------------------------------------------------------ in the change flow
def test_pass_goes_into_the_approval_and_the_hash(vch):
    ch = vch()
    st = propose(ch)
    assert st["state"] == "waiting_approval" and st["validation"]["status"] == "pass"
    assert "Validation (batfish): PASS" in ch["sent"][-1]["message"]
    t = json.loads((ch["s"].state_path / "tickets" / f"{st['ticket']}.json").read_text())
    good = t["hash"]
    t["validation"]["status"] = "fail"
    assert Changes._hash(t) != good                      # the approval covers the validated result


def test_fail_before_push_blocks(vch):
    ch = vch()
    n = len(ch["sent"])
    st = propose(ch, lines=["interface Loopback10", "description BAD"])
    assert st["state"] == "failed" and st["attention"] and st["failure"]["kind"] == "validation"
    assert "FAILED validation" in st["next"] and st["validation"]["status"] == "fail"
    assert "/pushok" not in "".join(m["message"] for m in ch["sent"][n:])        # no approval opened
    assert "FAILED validation" in ch["sent"][-1]["message"]
    assert ch["sw"].running == ["hostname qaleaf1a", "interface Ethernet1"]
    assert not list((ch["s"].state_path / "locks").iterdir())


def test_fail_after_push_rolls_back(vch):
    ch = vch()
    tid = propose(ch)["ticket"]
    _decide(ch)
    ch["flag"].write_text("1")                          # the live config is judged bad after the commit
    done = ch["c"].apply(tid)
    assert done["state"] == "failed" and done["devices"][0]["state"] == "rolled_back"
    assert "validation after the push" in done["devices"][0]["error"]
    assert done["validation_after_push"][0]["status"] == "fail"
    assert "interface Loopback10" not in ch["sw"].running and ch["sw"].timer is None


def test_post_push_off_and_pass(vch):
    ch = vch(POST_PUSH=False)
    tid = propose(ch)["ticket"]
    _decide(ch)
    ch["flag"].write_text("1")
    assert ch["c"].apply(tid)["state"] == "applied"     # no second check
    ch["flag"].unlink()
    ch2 = vch()
    tid = propose(ch2, lines=["interface Loopback11", "description ok"])["ticket"]
    _decide(ch2)
    done = ch2["c"].apply(tid)
    assert done["state"] == "applied" and done["validation_after_push"][0]["status"] == "pass"


def test_unreachable_validator_blocks_or_warns(vch):
    ch = vch(down_now=True)
    st = propose(ch)
    assert st["state"] == "failed" and "not reachable" in st["validation"]["summary"]
    ch = vch(down_now=True, ON_UNREACHABLE="warn")
    st = propose(ch)
    assert st["state"] == "waiting_approval" and st["validation"]["status"] == "warn"
    assert "NOT validated" in ch["sent"][-1]["message"]


def test_no_validation_keeps_the_old_flow(_ch):  # noqa: F811
    st = propose(_ch)
    assert st["state"] == "waiting_approval" and "validation" not in st
    assert "Validation" not in _ch["sent"][-1]["message"]


# ------------------------------------------------------------------ navigator-setup
@pytest.fixture
def env(tmp_path, monkeypatch):
    nav = tmp_path / "navigator.yml"
    nav.write_text("PROJECT_ROOT: %s\nCONSUMER: NAVI\n" % tmp_path)
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(nav))
    monkeypatch.setenv("RUDDER_LOCAL", str(tmp_path / "rudder.local.yml"))
    monkeypatch.setattr(S, "hermes_home", lambda: tmp_path / "nohermes")
    return tmp_path


def test_setup_saves_validation_only_after_the_test(env):
    seen = []
    msgs = S.apply_changes([("VALIDATION.MODE", "batfish"), ("VALIDATION.HOST", "127.0.0.1")],
                           check_val=lambda b: seen.append(b) or "validator ok")
    assert seen == [{"MODE": "batfish", "ON_UNREACHABLE": "block", "POST_PUSH": True, "HOST": "127.0.0.1"}]
    assert "validator ok" in msgs
    d = yaml.safe_load((env / "rudder.local.yml").read_text())
    assert d["VALIDATION"]["MODE"] == "batfish"
    assert "VALIDATION.MODE         batfish" in S.show()

    def bad(b):
        raise S.SetupError("validation test failed, nothing saved: refused")
    with pytest.raises(S.SetupError):
        S.apply_changes([("VALIDATION.MODE", "netclaw"), ("VALIDATION.HOST", "10.1.1.1")], check_val=bad)
    assert yaml.safe_load((env / "rudder.local.yml").read_text())["VALIDATION"]["MODE"] == "batfish"
    with pytest.raises(S.SetupError, match="public"):
        S.apply_changes([("VALIDATION.HOST", "8.8.8.8")], check_val=lambda b: "x")
    S.apply_changes([("VALIDATION.MODE", "none")], check_val=lambda b: "x")
    assert yaml.safe_load((env / "rudder.local.yml").read_text())["VALIDATION"] == {"MODE": "none"}


def test_interview_questions():
    answers = iter(["batfish", "10.0.0.5", "-", "warn"])
    pairs = S.validation_questions(lambda text, default="": next(answers), {"validation": {}}, out=lambda *a: None)
    assert pairs == [("VALIDATION.MODE", "batfish"), ("VALIDATION.HOST", "10.0.0.5"), ("VALIDATION.PORT", "-"),
                     ("VALIDATION.ON_UNREACHABLE", "warn")]
    assert S.validation_questions(lambda text, default="": "none", {"validation": {}}, out=lambda *a: None) == \
        [("VALIDATION.MODE", "none")]


def test_command_mode_is_gone():
    s = type("S", (), {"validation": {"MODE": "command"}})()
    with pytest.raises(V.ValidationError):
        V.settings(s)
    with pytest.raises(S.SetupError):
        S._split("VALIDATION.COMMAND")
