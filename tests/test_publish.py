from verify.publish import decide


def test_founder_real_company_publishes():
    d = decide(llm_verdict="FOUNDER", company_real=True, entity_type="company",
               cornell_tie="strong", confidence=0.9, contradiction=False)
    assert d["state"] == "verified"


def test_noncompany_rejected_even_if_founder():
    d = decide(llm_verdict="FOUNDER", company_real=False, entity_type="university_unit",
               cornell_tie="strong", confidence=0.9, contradiction=False)
    assert d["state"] == "rejected"
    assert "not-a-company" in d["reason"]


def test_contradiction_routes_to_human():
    d = decide(llm_verdict="FOUNDER", company_real=True, entity_type="company",
               cornell_tie="strong", confidence=0.9, contradiction=True)
    assert d["state"] == "needs_human"


def test_low_confidence_routes_to_human_not_reject():
    d = decide(llm_verdict="UNCLEAR", company_real=True, entity_type="company",
               cornell_tie="weak", confidence=0.4, contradiction=False)
    assert d["state"] == "needs_human"
