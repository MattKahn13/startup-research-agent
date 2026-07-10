"""Real-company + entity-type check. Fast local pre-filters (Cornell-internal,
investment/foundation by name) short-circuit before any network call; otherwise
OpenCorporates (free tier) confirms a registered entity. Returns a CompanyCheck
dict: {company_real, entity_type, source, detail}.
"""
import re
import requests

OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
_CORNELL = re.compile(r"^(the\s+)?(cornell|weill cornell)\b", re.I)
_FUND = re.compile(r"\b(ventures|venture partners|equity partners|capital partners|"
                   r"capital management|capital|private equity|fund|accelerator|incubator|"
                   r"angel|holdings|advisors)\b", re.I)
_FOUNDATION = re.compile(r"\b(foundation|endowment|philanthropies|charitable trust)\b", re.I)


def _result(real, etype, source, detail=""):
    return {"company_real": real, "entity_type": etype, "source": source, "detail": detail}


# Circuit breaker: OpenCorporates' anonymous tier now returns 401 (an API key is
# required). Over a 1800-company batch we must not fire 1800 doomed calls. After
# _BREAKER_TRIP consecutive failures (401/403/429/network error) we stop calling
# and treat the API as unavailable for the rest of the run -- the name-rules still
# fire, so noncompany entities are still caught; only the bonus registration
# corroboration is lost.
_BREAKER_TRIP = 5
_breaker = {"fails": 0, "open": False}


def reset_breaker():
    _breaker["fails"] = 0
    _breaker["open"] = False


def _trip():
    _breaker["fails"] += 1
    if _breaker["fails"] >= _BREAKER_TRIP:
        _breaker["open"] = True


def check_company(name: str) -> dict:
    n = (name or "").strip()
    if _CORNELL.search(n):
        return _result(False, "university_unit", "name-rule")
    if _FOUNDATION.search(n):
        return _result(False, "foundation", "name-rule")
    if _FUND.search(n):
        return _result(False, "investment_fund", "name-rule")
    if _breaker["open"]:
        return _result(None, "unknown", "opencorporates", "api unavailable (breaker open)")
    try:
        r = requests.get(OC_URL, params={"q": n}, timeout=20)
        if r.status_code != 200:
            _trip()
            return _result(None, "unknown", "opencorporates", f"http {r.status_code}")
        cos = (r.json().get("results", {}) or {}).get("companies", [])
    except Exception:
        _trip()
        return _result(None, "unknown", "opencorporates", "lookup failed")
    _breaker["fails"] = 0  # a clean 200 resets the failure streak
    if cos:
        c = cos[0]["company"]
        return _result(True, "company", "opencorporates",
                       f'{c.get("name")} / {c.get("jurisdiction_code")} / {c.get("company_number")}')
    return _result(None, "unknown", "opencorporates", "no registration match")
