import verify.wikidata as wd


class _Resp:
    def __init__(self, js):
        self._js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


_FAKE = {"results": {"bindings": [
    {"companyLabel": {"value": "Ava Labs"}, "founderLabel": {"value": "Emin Gun Sirer"}},
    {"companyLabel": {"value": "Ava Labs"}, "founderLabel": {"value": "Maofan Ted Yin"}},
    {"companyLabel": {"value": "OpenEvidence"}, "founderLabel": {"value": "Zachary Ziegler"}},
]}}


def test_returns_company_to_founders_map(monkeypatch):
    wd._CACHE.clear()
    monkeypatch.setattr(wd.requests, "get", lambda url, params=None, headers=None, timeout=0: _Resp(_FAKE))
    m = wd.cornell_founded_companies()
    assert set(m["Ava Labs"]) == {"Emin Gun Sirer", "Maofan Ted Yin"}
    assert m["OpenEvidence"] == ["Zachary Ziegler"]


def test_validator_confirms_known_edge(monkeypatch):
    wd._CACHE.clear()
    monkeypatch.setattr(wd.requests, "get", lambda url, params=None, headers=None, timeout=0: _Resp(_FAKE))
    assert wd.confirms_founding("Ava Labs", "Ted Yin") is True
    assert wd.confirms_founding("Ava Labs", "Jeff Bezos") is False
