import verify.real_company as rc


class _Resp:
    def __init__(self, js, code=200):
        self._js, self.status_code = js, code

    def json(self):
        return self._js


def test_opencorporates_hit_marks_real(monkeypatch):
    monkeypatch.setattr(rc.requests, "get",
        lambda url, params=None, timeout=0: _Resp(
            {"results": {"companies": [{"company": {"name": "Anduril Industries Inc",
                                                    "company_number": "123", "jurisdiction_code": "us_ca",
                                                    "inactive": False}}]}}))
    r = rc.check_company("Anduril Industries")
    assert r["company_real"] is True
    assert r["entity_type"] == "company"
    assert r["source"] == "opencorporates"


def test_cornell_unit_name_is_flagged_noncompany_without_a_call():
    r = rc.check_company("Cornell Feline Health Center")
    assert r["company_real"] is False
    assert r["entity_type"] == "university_unit"


def test_investment_name_flagged_fund():
    r = rc.check_company("Everywhere Ventures")
    assert r["entity_type"] == "investment_fund"


def test_pe_and_capital_names_flagged_fund():
    # These leaked into a verified run before the rule was tightened.
    for nm in ["Vista Equity Partners", "NRDC Equity Partners", "Catylyst Capital",
               "Security Capital Group, Inc"]:
        r = rc.check_company(nm)
        assert r["entity_type"] == "investment_fund", nm
        assert r["company_real"] is False, nm


def test_breaker_opens_after_repeated_401_then_stops_calling(monkeypatch):
    rc.reset_breaker()
    calls = {"n": 0}

    def failing_get(url, params=None, timeout=0):
        calls["n"] += 1
        return _Resp({"error": "unauthorized"}, code=401)

    monkeypatch.setattr(rc.requests, "get", failing_get)
    # First _BREAKER_TRIP names all attempt the call and fail...
    for i in range(rc._BREAKER_TRIP):
        assert rc.check_company(f"Some Real Co {i}")["company_real"] is None
    assert calls["n"] == rc._BREAKER_TRIP
    # ...after which the breaker is open and no further HTTP calls are made.
    r = rc.check_company("Another Real Co")
    assert calls["n"] == rc._BREAKER_TRIP  # unchanged -- no new call
    assert "breaker open" in r["detail"]
    rc.reset_breaker()
