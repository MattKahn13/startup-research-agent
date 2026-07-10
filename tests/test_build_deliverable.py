from build_deliverable import merge_signals


def test_founder_confirmed_by_wikidata_publishes_without_llm_doubt():
    row = merge_signals(
        record={"company_name": "Ava Labs", "cornellian_founder": "Emin Gun Sirer",
                "affiliation_evidence": "co-founded", "proof_url": "https://news.cornell.edu/x",
                "affiliation_type": "Alumnus"},
        adj_verdict="UNCLEAR",
        recovery_verdict="FOUNDER",
        wikidata_confirms=True,
        company_check={"company_real": True, "entity_type": "company"},
        source_tier="mention")
    assert row["state"] == "verified"
    assert row["confidence"] >= 0.70
    assert "api-confirmed" in row["provenance"]


def test_execs_stay_rejected():
    row = merge_signals(
        record={"company_name": "Citigroup", "cornellian_founder": "Sandy Weill",
                "affiliation_evidence": "former chairman", "proof_url": "x",
                "affiliation_type": "Alumnus"},
        adj_verdict="EXECUTIVE", recovery_verdict=None, wikidata_confirms=False,
        company_check={"company_real": True, "entity_type": "company"}, source_tier="mention")
    assert row["state"] == "rejected"
