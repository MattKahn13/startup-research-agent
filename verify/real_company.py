"""Real-company + entity-type check. Fast local pre-filters (Cornell-internal,
investment/foundation by name) short-circuit before any network call; otherwise
OpenCorporates (free tier) confirms a registered entity. Returns a CompanyCheck
dict: {company_real, entity_type, source, detail}.
"""
import re
import requests

OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
_CORNELL = re.compile(r"^(the\s+)?(cornell|weill cornell)\b", re.I)
_FUND = re.compile(r"\b(ventures|venture partners|capital partners|capital management|"
                   r"private equity|\bfund\b|accelerator|incubator|angel|holdings|advisors)\b", re.I)
_FOUNDATION = re.compile(r"\b(foundation|endowment|philanthropies|charitable trust)\b", re.I)


def _result(real, etype, source, detail=""):
    return {"company_real": real, "entity_type": etype, "source": source, "detail": detail}


def check_company(name: str) -> dict:
    n = (name or "").strip()
    if _CORNELL.search(n):
        return _result(False, "university_unit", "name-rule")
    if _FOUNDATION.search(n):
        return _result(False, "foundation", "name-rule")
    if _FUND.search(n):
        return _result(False, "investment_fund", "name-rule")
    try:
        r = requests.get(OC_URL, params={"q": n}, timeout=20)
        cos = (r.json().get("results", {}) or {}).get("companies", [])
    except Exception:
        return _result(None, "unknown", "opencorporates", "lookup failed")
    if cos:
        c = cos[0]["company"]
        return _result(True, "company", "opencorporates",
                       f'{c.get("name")} / {c.get("jurisdiction_code")} / {c.get("company_number")}')
    return _result(None, "unknown", "opencorporates", "no registration match")
