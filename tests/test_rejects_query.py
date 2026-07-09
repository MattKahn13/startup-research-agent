from verify.rejects_query import why_excluded

_REJECTS = [
    {"company_name": "Cisco Systems", "cornellian_founder": "Lew Tucker",
     "verdict": "EXECUTIVE", "evidence": "one of our CTOs", "source_domain": "ezramagazine.cornell.edu"},
    {"company_name": "Everywhere Ventures", "verdict": "NONCOMPANY", "evidence": "VC fund",
     "source_domain": "tech.cornell.edu"},
]


def test_lookup_returns_reason_and_evidence():
    r = why_excluded("cisco systems", _REJECTS)
    assert r["verdict"] == "EXECUTIVE"
    assert "CTO" in r["evidence"]


def test_unknown_company_returns_none():
    assert why_excluded("SpaceX", _REJECTS) is None
