from build_deliverable import merge_signals, entity_status


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


def test_unprovable_entity_is_noted_not_disqualified():
    # OpenCorporates couldn't confirm (None) and no Wikidata hit: the row must carry
    # an explicit UNVERIFIED note but must NOT be rejected on that basis alone.
    es = entity_status({"company_real": None, "entity_type": "unknown"}, wikidata_confirms=False)
    assert es["verified"] is None
    assert "UNVERIFIED" in es["note"] and "not disqualifying" in es["note"]

    row = merge_signals(
        record={"company_name": "Stealth Co", "cornellian_founder": "Jane Doe",
                "affiliation_evidence": "founded by Jane Doe", "proof_url": "https://eship.cornell.edu/x",
                "affiliation_type": "Alumnus"},
        adj_verdict="FOUNDER", recovery_verdict=None, wikidata_confirms=False,
        company_check={"company_real": None, "entity_type": "unknown"}, source_tier="directory")
    assert row["entity_verified"] is None
    assert "UNVERIFIED" in row["entity_note"]
    assert row["state"] == "verified"  # unprovable entity did NOT block a directory-sourced founder


def test_wikidata_hit_marks_entity_confirmed():
    es = entity_status({"company_real": None, "entity_type": "unknown"}, wikidata_confirms=True)
    assert es["verified"] is True and "wikidata" in es["note"]


def test_execs_stay_rejected():
    row = merge_signals(
        record={"company_name": "Citigroup", "cornellian_founder": "Sandy Weill",
                "affiliation_evidence": "former chairman", "proof_url": "x",
                "affiliation_type": "Alumnus"},
        adj_verdict="EXECUTIVE", recovery_verdict=None, wikidata_confirms=False,
        company_check={"company_real": True, "entity_type": "company"}, source_tier="mention")
    assert row["state"] == "rejected"
