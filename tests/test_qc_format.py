"""QC verdict format (night run 2026-11-17, live case H.M8): a claim without evidence is REJECT, spelled out."""
from navigator_engineer import roles


def test_claim_without_evidence_is_reject_with_the_reply_format():
    out = roles.qc_check("STATE_VALIDATOR", "QALEAF1A has 12 EVPN peers. There is no data behind it.")
    assert out["evidence_cited"] is False and out["verdict"] == "REJECT"
    assert out["failed"] == ["evidence", "no_invention"]
    assert "QC VERDICT: REJECT" in out["next"] and "Do not go and check the devices" in out["next"]
    assert len(out["checks"]) == 8 and "QC VERDICT:" in out["reply_format"]


def test_cited_claim_leaves_the_verdict_to_the_reviewer():
    for item in ("12 EVPN peers, see RECORDS/QALEAF1A/show_bgp_evpn_summary.json",
                 "peers listed in OUT/report.md line 14", "per https://www.arista.com/en/um-eos"):
        out = roles.qc_check("STATE_VALIDATOR", item)
        assert out["evidence_cited"] is True and "verdict" not in out, item


def test_sanity_agent_brief_has_the_verdict_line():
    text = (roles.ROLE_DIR / "SANITY_AGENT.md").read_text()
    assert "QC VERDICT: REJECT" in text and "not whether it is true" in text
