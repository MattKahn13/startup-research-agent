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
