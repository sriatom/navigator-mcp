import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from navigator_engineer import roles

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("navigator_qc", ROOT / "integrations/hermes/navigator-qc/__init__.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
P.PYTHON = sys.executable


def ev(text):
    return SimpleNamespace(text=text)


def test_qc_request_forms():
    assert P.qc_request("QC this: QALEAF1A has 12 EVPN peers.") == "QALEAF1A has 12 EVPN peers."
    assert P.qc_request("qc: EDGE1 runs 17.9") == "EDGE1 runs 17.9"
    assert P.qc_request("<@U0ABC> please QC this claim: X has 3 peers") == "X has 3 peers"
    assert P.qc_request("`QC this` QALEAF1A has 12 EVPN peers") == "QALEAF1A has 12 EVPN peers"
    assert P.qc_request("sanity-check this: the MTU is 9214") == "the MTU is 9214"
    for no in ("show bgp summary on QALEAF1A", "/model local", "quality check later", "QC", "qcx this", None):
        assert P.qc_request(no) is None, no


def test_no_evidence_is_rejected_without_the_model():
    r = P.on_gateway_dispatch(ev("QC this: QALEAF1A has 12 EVPN peers."))
    assert r["action"] == "rewrite" and r["text"].startswith("/navqc ")
    reply = P.navqc(r["text"].split(" ", 1)[1])
    assert reply.startswith("QC VERDICT: REJECT") and "evidence" in reply
    assert "expired" in P.navqc(r["text"].split(" ", 1)[1])      # one use


def test_evidence_or_thread_reference_goes_to_the_model():
    for text in ("QC this: QALEAF1A has 2 EVPN peers\n```\n10.25.10.110 Estab\n10.25.10.111 Estab\n```",
                 "QC this: see RAW/QALEAF1A_bgp.txt line 4, 2 peers",
                 "QC the last result",
                 "QC your answer above"):
        r = P.on_gateway_dispatch(ev(text))
        assert r["action"] == "rewrite" and r["text"].startswith("[NAVIGATOR QC]") and r["text"].endswith(text)
        assert P.qc_request(r["text"]) is None                     # never rewritten twice


def test_failed_check_falls_back_to_the_model(monkeypatch):
    monkeypatch.setattr(P, "PYTHON", "/no/such/python")
    r = P.on_gateway_dispatch(ev("QC this: QALEAF1A has 12 EVPN peers."))
    assert r["text"].startswith("[NAVIGATOR QC]")


def test_not_qc_is_left_alone():
    assert P.on_gateway_dispatch(ev("show version on QALEAF1A")) is None


def test_qc_cli():
    out = subprocess.run([sys.executable, "-m", "navigator_engineer.qc_cli"], input=json.dumps({"item": "X has 12 peers"}),
                         capture_output=True, text=True, cwd=ROOT, check=True).stdout
    d = json.loads(out)
    assert d["verdict"] == "REJECT" and d["reply"].startswith(roles.REJECT_NO_EVIDENCE)
    d = json.loads(subprocess.run([sys.executable, "-m", "navigator_engineer.qc_cli"],
                                  input=json.dumps({"item": "X has 2 peers: RAW/x.txt"}), capture_output=True,
                                  text=True, cwd=ROOT, check=True).stdout)
    assert d["verdict"] is None and "reply" not in d


def test_strict_rule_in_the_texts():
    assert "never PASS WITH NOTES" in roles.qc_check("x", "RAW/a.txt")["next"]
    assert "could not verify" in (ROOT / "navigator_engineer/roles/SANITY_AGENT.md").read_text()
