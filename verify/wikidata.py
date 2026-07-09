"""Wikidata SPARQL: companies whose founder (P112) was educated at (P69) Cornell
University (Q49115). Both a discovery SEED (structured candidates) and a VALIDATOR
(authoritative corroboration for the confidence score / structured-agreement
shortcut). Free, no key. The result is memoized so `confirms_founding` doesn't
re-query per lookup.
"""
import requests

ENDPOINT = "https://query.wikidata.org/sparql"
_QUERY = """
SELECT ?companyLabel ?founderLabel WHERE {
  ?company wdt:P112 ?founder .
  ?founder wdt:P69 wd:Q49115 .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""
_HEADERS = {"User-Agent": "cornell-founders-verify/1.0",
            "Accept": "application/sparql-results+json"}
_CACHE = {}  # memoized company -> [founders]


def _fetch():
    r = requests.get(ENDPOINT, params={"query": _QUERY, "format": "json"},
                     headers=_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def cornell_founded_companies() -> dict:
    if _CACHE:
        return _CACHE
    for b in _fetch():
        c = b["companyLabel"]["value"]
        f = b["founderLabel"]["value"]
        _CACHE.setdefault(c, [])
        if f not in _CACHE[c]:
            _CACHE[c].append(f)
    return _CACHE


def _last(name):
    p = (name or "").split()
    return p[-1].lower() if p else ""


def confirms_founding(company: str, person: str) -> bool:
    founders = cornell_founded_companies().get(company, [])
    return any(_last(person) and _last(person) == _last(f) for f in founders)
