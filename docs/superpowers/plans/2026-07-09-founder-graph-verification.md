# Founder Graph Verification -- Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** Turn the founder dataset from a recall-first scrape (~47% precision) into a
verified, confidence-scored, defensible dataset, by building a verification gate that is the only
door into the published data -- starting with a batch pass over the existing 1,761 candidates that
produces the Marx deliverable.

**Architecture:** Candidates (never "founders") flow through a verification gate:
founding-relationship (LLM, source-aware) + real-company (free APIs) + Cornell-tie + entity-type,
aggregated into a confidence score with a provenance chain. Authoritative structured sources
(Wikidata, OpenCorporates, EDGAR) both seed candidates and short-circuit the fragile LLM when they
agree. Spec: `docs/superpowers/specs/2026-07-09-founder-graph-verification-architecture-design.md`.

**Tech Stack:** Python 3.13, pytest, `requests` (free APIs: Wikidata SPARQL, OpenCorporates, SEC
EDGAR, SBIR/NIH/NSF/PatentsView), openpyxl (Excel), the existing `gemini_tool` for LLM adjudication.
Phase 2+ adds DuckDB. TDD throughout; API calls mocked in tests.

**Scope note:** This plan covers **Phase 1 only** (the batch verification gate + Marx deliverable,
buildable now with no perpetual scrape). Phases 2-5 (durable job queue, DuckDB store, discovery
rewire, expansion crawl, relaunch) are scoped as a roadmap at the end; each becomes its own plan.

---

## File structure (Phase 1)

- `verify/__init__.py` -- package marker.
- `verify/real_company.py` -- real-company + entity-type check via OpenCorporates / SEC EDGAR /
  GLEIF / ProPublica. One responsibility: given (company_name), return a `CompanyCheck`.
- `verify/wikidata.py` -- Wikidata SPARQL client: companies with a Cornell-educated founder
  (seed + validator). One responsibility: return `{company -> [founders]}`.
- `verify/confidence.py` -- pure scoring: aggregate signals -> confidence + provenance chain.
- `verify/contradiction.py` -- pure: detect founder-name vs evidence mismatch.
- `verify/publish.py` -- pure: the `verdict + signals -> keep/reject(reason)` decision + the
  published-row shape.
- `verify/rejects_query.py` -- pure: "why was X excluded?" lookup over the rejects log.
- `recover_unclear.py` -- re-adjudicate UNCLEAR records against their CACHED SOURCE PAGE (reuses
  `adjudicate_founders` + `startup_researcher.PageCache`).
- `build_deliverable.py` -- orchestrates: merge adjudication + Wikidata corroboration + real-company
  + confidence -> `cornellian_founders_verified.xlsx` + `founders_rejected.json` + `startups_db_verified.json`.
- Tests: `tests/test_real_company.py`, `tests/test_wikidata.py`, `tests/test_confidence.py`,
  `tests/test_contradiction.py`, `tests/test_publish.py`, `tests/test_rejects_query.py`.

---

## Task 1: Confidence scoring (pure)

**Files:**
- Create: `verify/__init__.py` (empty), `verify/confidence.py`
- Test: `tests/test_confidence.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_confidence.py
from verify.confidence import score_edge, SIGNAL_WEIGHTS

def test_directory_plus_api_agreement_is_high_confidence():
    s = score_edge(source_tier="directory", corroborations=2,
                   api_confirmed=True, cornell_tie="strong", llm_verdict="FOUNDER")
    assert s["confidence"] >= 0.85
    assert s["publishable"] is True
    assert "directory" in s["provenance"]

def test_single_low_source_no_api_is_low_confidence():
    s = score_edge(source_tier="mention", corroborations=1,
                   api_confirmed=False, cornell_tie="weak", llm_verdict="UNCLEAR")
    assert s["confidence"] < 0.5
    assert s["publishable"] is False

def test_api_confirmation_alone_can_publish_without_llm():
    # structured-agreement shortcut: two authoritative sources agree
    s = score_edge(source_tier="mention", corroborations=2,
                   api_confirmed=True, cornell_tie="strong", llm_verdict=None)
    assert s["publishable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_confidence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'verify.confidence'`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/confidence.py
"""Aggregate verification signals into a confidence score + provenance chain.
Pure: no I/O. The published dataset keeps only publishable edges, each carrying
its confidence and the reasons behind it (defensible on its face)."""

SIGNAL_WEIGHTS = {
    "source_directory": 0.45,   # a curated Cornell-startup directory listing
    "api_confirmed": 0.35,      # OpenCorporates/EDGAR/Wikidata confirmed the founding edge
    "per_corroboration": 0.12,  # each independent source beyond the first
    "cornell_strong": 0.20,     # confirmed Cornell education/affiliation
    "llm_founder": 0.25,        # the source-aware adjudicator said FOUNDER
}
PUBLISH_THRESHOLD = 0.70

def score_edge(source_tier, corroborations, api_confirmed, cornell_tie, llm_verdict):
    prov, score = [], 0.0
    if source_tier == "directory":
        score += SIGNAL_WEIGHTS["source_directory"]; prov.append("directory-source")
    if api_confirmed:
        score += SIGNAL_WEIGHTS["api_confirmed"]; prov.append("api-confirmed")
    extra = max(0, corroborations - 1)
    if extra:
        score += extra * SIGNAL_WEIGHTS["per_corroboration"]; prov.append(f"corroborations={corroborations}")
    if cornell_tie == "strong":
        score += SIGNAL_WEIGHTS["cornell_strong"]; prov.append("cornell-strong")
    if llm_verdict == "FOUNDER":
        score += SIGNAL_WEIGHTS["llm_founder"]; prov.append("llm-founder")
    score = min(1.0, score)
    return {"confidence": round(score, 3), "publishable": score >= PUBLISH_THRESHOLD,
            "provenance": prov}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_confidence.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/__init__.py verify/confidence.py tests/test_confidence.py
git commit -m "feat(verify): confidence scoring + provenance chain (pure)"
```

---

## Task 2: Contradiction detection (pure)

**Files:**
- Create: `verify/contradiction.py`
- Test: `tests/test_contradiction.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contradiction.py
from verify.contradiction import founder_matches_evidence

def test_matching_founder_and_evidence_is_consistent():
    assert founder_matches_evidence("Raj Mehra", "Raj Mehra, MBA '09, founded Sage") is True

def test_evidence_names_a_different_founder_flags_contradiction():
    # the Ava Labs case: record says Emin Gun Sirer, evidence names Ted Yin as founder
    assert founder_matches_evidence("Emin Gun Sirer",
                                    "founder Maofan 'Ted' Yin, M.S. '19") is False

def test_no_name_in_evidence_is_not_a_contradiction():
    # thin evidence (directory) -- absence is not a mismatch
    assert founder_matches_evidence("Will Bruey", "Will Bruey '11, MEng '12") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contradiction.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/contradiction.py
"""Detect when the record's founder name and the evidence disagree on WHO
founded the company (the Ava Labs bug -> a caught contradiction, not a silent
wrong pick). Pure."""
import re

_FOUND_NEAR = re.compile(r"\b(founder|co-?founder|founded by)\b[^.]{0,40}", re.I)

def _last(name):
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return parts[-1].lower() if parts else ""

def founder_matches_evidence(founder, evidence):
    """False only when the evidence explicitly names a DIFFERENT person as the
    founder. Absence of the name (thin evidence) is not a contradiction."""
    ev = evidence or ""
    if _last(founder) and _last(founder) in ev.lower():
        return True
    m = _FOUND_NEAR.search(ev)
    if not m:
        return True  # no explicit founder claim in evidence -> can't contradict
    named = m.group(0)
    # evidence explicitly says "founder <someone>" and it's not our person
    return _last(founder) in named.lower() if _last(founder) else True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contradiction.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/contradiction.py tests/test_contradiction.py
git commit -m "feat(verify): founder-name vs evidence contradiction detection (pure)"
```

---

## Task 3: The publish rule (pure)

**Files:**
- Create: `verify/publish.py`
- Test: `tests/test_publish.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_publish.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_publish.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/publish.py
"""The single gate decision: (verdict + signals) -> verified | rejected(reason)
| needs_human. Pure. The published dataset is exactly the 'verified' set."""

_NONCOMPANY_TYPES = {"university_unit", "investment_fund", "foundation", "nonprofit",
                     "journal", "government_program"}
_NONFOUNDER = {"EMPLOYEE", "EXECUTIVE", "INVESTOR", "DONOR", "ATTENDEE", "NONCOMPANY"}

def decide(llm_verdict, company_real, entity_type, cornell_tie, confidence, contradiction):
    if contradiction:
        return {"state": "needs_human", "reason": "founder/evidence contradiction"}
    if entity_type in _NONCOMPANY_TYPES or company_real is False:
        return {"state": "rejected", "reason": f"not-a-company ({entity_type})"}
    if cornell_tie == "none":
        return {"state": "rejected", "reason": "no Cornell tie"}
    if llm_verdict in _NONFOUNDER:
        return {"state": "rejected", "reason": f"role={llm_verdict.lower()}"}
    if confidence >= 0.70 and llm_verdict in (None, "FOUNDER"):
        return {"state": "verified", "reason": "founding confirmed"}
    return {"state": "needs_human", "reason": f"insufficient confidence ({confidence})"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_publish.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/publish.py tests/test_publish.py
git commit -m "feat(verify): the publish decision gate (pure)"
```

---

## Task 4: Real-company check (OpenCorporates + EDGAR), HTTP mocked

**Files:**
- Create: `verify/real_company.py`
- Test: `tests/test_real_company.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_real_company.py
import verify.real_company as rc

class _Resp:
    def __init__(self, js, code=200): self._js, self.status_code = js, code
    def json(self): return self._js

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_real_company.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/real_company.py
"""Real-company + entity-type check. Fast local pre-filters (Cornell-internal,
investment/foundation by name) short-circuit before any network call; otherwise
OpenCorporates (free tier) confirms a registered entity. EDGAR is a fallback for
funded/public companies. Returns a CompanyCheck dict."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_real_company.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/real_company.py tests/test_real_company.py
git commit -m "feat(verify): real-company + entity-type check (OpenCorporates + name rules)"
```

---

## Task 5: Wikidata seed + validator, HTTP mocked

**Files:**
- Create: `verify/wikidata.py`
- Test: `tests/test_wikidata.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wikidata.py
import verify.wikidata as wd

class _Resp:
    def __init__(self, js): self._js = js
    def raise_for_status(self): pass
    def json(self): return self._js

_FAKE = {"results": {"bindings": [
    {"companyLabel": {"value": "Ava Labs"}, "founderLabel": {"value": "Emin Gun Sirer"}},
    {"companyLabel": {"value": "Ava Labs"}, "founderLabel": {"value": "Maofan Ted Yin"}},
    {"companyLabel": {"value": "OpenEvidence"}, "founderLabel": {"value": "Zachary Ziegler"}},
]}}

def test_returns_company_to_founders_map(monkeypatch):
    monkeypatch.setattr(wd.requests, "get", lambda url, params=None, headers=None, timeout=0: _Resp(_FAKE))
    m = wd.cornell_founded_companies()
    assert set(m["Ava Labs"]) == {"Emin Gun Sirer", "Maofan Ted Yin"}
    assert m["OpenEvidence"] == ["Zachary Ziegler"]

def test_validator_confirms_known_edge(monkeypatch):
    monkeypatch.setattr(wd.requests, "get", lambda url, params=None, headers=None, timeout=0: _Resp(_FAKE))
    assert wd.confirms_founding("Ava Labs", "Ted Yin") is True
    assert wd.confirms_founding("Ava Labs", "Jeff Bezos") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wikidata.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/wikidata.py
"""Wikidata SPARQL: companies whose founder (P112) was educated at (P69) Cornell
University (Q49115). Both a discovery SEED (structured candidates) and a VALIDATOR
(authoritative corroboration for the confidence score / structured-agreement
shortcut). Free, no key."""
import requests

ENDPOINT = "https://query.wikidata.org/sparql"
_QUERY = """
SELECT ?companyLabel ?founderLabel WHERE {
  ?company wdt:P112 ?founder .
  ?founder wdt:P69 wd:Q49115 .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""
_HEADERS = {"User-Agent": "cornell-founders-verify/1.0", "Accept": "application/sparql-results+json"}

def _fetch():
    r = requests.get(ENDPOINT, params={"query": _QUERY, "format": "json"},
                     headers=_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()["results"]["bindings"]

def cornell_founded_companies() -> dict:
    out = {}
    for b in _fetch():
        c = b["companyLabel"]["value"]; f = b["founderLabel"]["value"]
        out.setdefault(c, [])
        if f not in out[c]:
            out[c].append(f)
    return out

def _last(name):
    p = (name or "").split()
    return p[-1].lower() if p else ""

def confirms_founding(company: str, person: str) -> bool:
    founders = cornell_founded_companies().get(company, [])
    return any(_last(person) and _last(person) == _last(f) for f in founders)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wikidata.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/wikidata.py tests/test_wikidata.py
git commit -m "feat(verify): Wikidata Cornell-founder seed + validator"
```

---

## Task 6: Queryable rejects ("why was X excluded?")

**Files:**
- Create: `verify/rejects_query.py`
- Test: `tests/test_rejects_query.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rejects_query.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rejects_query.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# verify/rejects_query.py
"""Answer Marx's 'what about X?' from the rejects log: verdict + evidence +
source, instantly. Pure."""

def why_excluded(company_name: str, rejects: list) -> dict | None:
    key = (company_name or "").strip().lower()
    for r in rejects:
        if (r.get("company_name") or "").strip().lower() == key:
            return r
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rejects_query.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add verify/rejects_query.py tests/test_rejects_query.py
git commit -m "feat(verify): queryable rejects -- instant 'why excluded' lookup"
```

---

## Task 7: UNCLEAR source-recovery (re-adjudicate against the cached page)

**Files:**
- Create: `recover_unclear.py`
- (Reuses `adjudicate_founders`, `startup_researcher.PageCache`, the existing Gemini session.)

- [ ] **Step 1: Write the failing test (pure prompt-builder only; the Gemini call is not unit-tested)**

```python
# tests/test_recover_unclear.py
from recover_unclear import build_recovery_prompt

def test_prompt_includes_page_text_and_asks_founder_yes_no():
    p = build_recovery_prompt(company="Ava Labs", person="Emin Gun Sirer",
                              page_text="Ava Labs was co-founded by Emin Gun Sirer and Ted Yin.")
    assert "Ava Labs" in p and "Emin Gun Sirer" in p
    assert "co-founded by Emin" in p
    assert "FOUNDER" in p and "NOT" in p  # asks for a founder/not decision
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_recover_unclear.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# recover_unclear.py
"""Recover real founders wrongly parked in UNCLEAR by re-reading the CACHED SOURCE
PAGE (not the thin snippet). Reuses the cached pages already on disk, so no
re-download. Resumable: appends to unclear_recovery.jsonl.
Run: python recover_unclear.py"""
import json
from pathlib import Path
import startup_researcher as sr

OUT = Path("startup_output_overnight")
RESULTS = OUT / "adjudication_results.jsonl"
RECOVERY = OUT / "unclear_recovery.jsonl"
DB = OUT / "startups_db.json"

def build_recovery_prompt(company, person, page_text):
    return (f"From the SOURCE PAGE below, did {person} FOUND (or co-found) {company}? "
            f"Answer exactly one token: FOUNDER if the page says they founded/co-founded it, "
            f"or NOT if the page shows any other relationship (employee, executive, donor, "
            f"attendee) or does not establish founding.\n\n"
            f"COMPANY: {company}\nPERSON: {person}\n\nSOURCE PAGE (truncated):\n{page_text[:6000]}")

def _unclear_records():
    verdicts = {json.loads(l)["company_name"]: json.loads(l)
                for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()}
    recs = json.loads(DB.read_text(encoding="utf-8"))["records"]
    return [r for r in recs if verdicts.get(r.get("company_name"), {}).get("verdict") == "UNCLEAR"]

def main():
    done = set()
    if RECOVERY.exists():
        done = {json.loads(l)["company_name"] for l in RECOVERY.read_text(encoding="utf-8").splitlines() if l.strip()}
    cache = sr.PageCache(str(OUT))
    todo = [r for r in _unclear_records() if r.get("company_name") not in done]
    sr.start_gemini()
    with RECOVERY.open("a", encoding="utf-8") as fh:
        for r in todo:
            page = cache.get(r.get("proof_url") or "") or (r.get("affiliation_evidence") or "")
            prompt = build_recovery_prompt(r.get("company_name"), r.get("cornellian_founder"), page)
            raw = sr.call_gemini(prompt, label="Recover")
            verdict = "FOUNDER" if "FOUNDER" in (raw or "").upper()[:20] else "NOT"
            fh.write(json.dumps({"company_name": r.get("company_name"), "verdict": verdict},
                                ensure_ascii=False) + "\n")
            fh.flush()
    print(f"recovered over {len(todo)} UNCLEAR records -> {RECOVERY}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_recover_unclear.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add recover_unclear.py tests/test_recover_unclear.py
git commit -m "feat(cleanup): UNCLEAR source-recovery via cached page re-read (resumable)"
```

- [ ] **Step 6: Run the recovery pass live (only when scrape is un-paused / Matt approves)**

Run: `PYTHONUTF8=1 python recover_unclear.py` (background, fresh kb-gate contract).
Expected: `unclear_recovery.jsonl` grows to ~423 lines.

---

## Task 8: Assemble the verified deliverable

**Files:**
- Create: `build_deliverable.py`
- (Reuses `verify/*`, the adjudication + recovery jsonl, Wikidata, real_company.)

- [ ] **Step 1: Write the failing test (the merge/publish logic)**

```python
# tests/test_build_deliverable.py
from build_deliverable import merge_signals

def test_founder_confirmed_by_wikidata_publishes_without_llm_doubt():
    row = merge_signals(
        record={"company_name": "Ava Labs", "cornellian_founder": "Emin Gun Sirer",
                "affiliation_evidence": "co-founded", "proof_url": "https://news.cornell.edu/x"},
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
                "affiliation_evidence": "former chairman", "proof_url": "x"},
        adj_verdict="EXECUTIVE", recovery_verdict=None, wikidata_confirms=False,
        company_check={"company_real": True, "entity_type": "company"}, source_tier="mention")
    assert row["state"] == "rejected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_deliverable.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# build_deliverable.py
"""Merge every signal per candidate -> a verified/rejected/needs_human decision
with confidence + provenance, then write the deliverables. The recovery verdict
overrides an UNCLEAR adjudication; Wikidata/OpenCorporates agreement is the
structured-agreement shortcut. Reuses apply_adjudication's Excel writer."""
import json
from pathlib import Path
from verify.confidence import score_edge
from verify.contradiction import founder_matches_evidence
from verify.publish import decide

OUT = Path("startup_output_overnight")

def _cornell_tie(record):
    return "strong" if (record.get("affiliation_type") or "").strip() else "weak"

def merge_signals(record, adj_verdict, recovery_verdict, wikidata_confirms,
                  company_check, source_tier):
    verdict = recovery_verdict or adj_verdict  # recovery overrides UNCLEAR
    api_confirmed = bool(wikidata_confirms)
    corrob = 1 + (1 if wikidata_confirms else 0)
    contradiction = not founder_matches_evidence(record.get("cornellian_founder"),
                                                 record.get("affiliation_evidence") or "")
    tie = _cornell_tie(record)
    s = score_edge(source_tier=source_tier, corroborations=corrob,
                   api_confirmed=api_confirmed, cornell_tie=tie,
                   llm_verdict=verdict if verdict == "FOUNDER" else None)
    d = decide(llm_verdict=verdict, company_real=company_check.get("company_real"),
               entity_type=company_check.get("entity_type"), cornell_tie=tie,
               confidence=s["confidence"], contradiction=contradiction)
    return {**record, "state": d["state"], "reason": d["reason"],
            "confidence": s["confidence"], "provenance": s["provenance"]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_build_deliverable.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Add the orchestration `main()` (reads jsonl + calls the free APIs + writes Excel/JSON)**

Extend `build_deliverable.py` with a `main()` that: loads `startups_db.json`,
`adjudication_results.jsonl`, `unclear_recovery.jsonl`; fetches `wikidata.cornell_founded_companies()`
once; calls `real_company.check_company` per unique company (cache results); runs `merge_signals`;
writes `cornellian_founders_verified.xlsx` (state=verified, sorted by confidence, with a Confidence +
Provenance column), `founders_needs_human.json`, and `founders_rejected.json`. Reuse
`apply_adjudication.write_excel`.

- [ ] **Step 6: Run the full suite + build**

Run: `python -m pytest -q` then `PYTHONUTF8=1 python build_deliverable.py`
Expected: suite green; prints verified/needs_human/rejected counts; three files written.

- [ ] **Step 7: Commit**

```bash
git add build_deliverable.py tests/test_build_deliverable.py
git commit -m "feat(deliverable): assemble verified founders (confidence + provenance) + Excel"
```

---

## Phase 1 self-review

- Spec coverage: verification gate (Tasks 3,4), founding recovery (7), real-company (4), Wikidata
  seed+validator+shortcut (5,8), confidence+provenance (1,8), contradiction (2), queryable rejects
  (6), Marx deliverable (8). Covered. NOT in Phase 1 (own plans): durable queue, DuckDB store,
  discovery rewire, expansion crawl, SBIR/patent discovery, extraction hardening -- see roadmap.
- No placeholders: every task has real test + implementation code + exact commands.
- Type consistency: `check_company` returns `{company_real, entity_type, source, detail}` used
  consistently in Tasks 4 and 8; `score_edge`/`decide` signatures match across Tasks 1,3,8.

---

## Roadmap: Phases 2-5 (each becomes its own plan)

**Phase 2 -- Durable job queue + DuckDB store + entity resolution.** `store.py` (DuckDB tables:
`entities`, `edges`, `jobs`, `verification_results`), `queue.py` (enqueue/lease/complete/retry,
crash+sleep durable), `entity_resolution.py` (canonical company/person keys; fixes the
one-bio-many-companies smear; the crawl visited-set). Migrate the 1,761 + verified deliverable in.

**Phase 3 -- Rewire discovery to enqueue; wire API verify-workers.** The existing scraper becomes a
SEARCH worker that writes CANDIDATES only (never asserts founder). Verify-workers (real_company,
wikidata, founding-adjudication) drain the queue. The perpetual run relaunches in candidate-only
mode behind the gate.

**Phase 4 -- Expansion crawl + deep-tech discovery.** `EXPAND_PERSON` (confirmed Cornellian ->
other ventures) and `EXPAND_COMPANY` (DBAs/subsidiaries), budget- + visited-set-bounded.
`GRANT_PATENT_SEARCH` worker (SBIR.gov / NIH RePORTER / NSF / PatentsView) as a first-class
discovery source; `EDGAR_SEARCH` (Form D + FTS) worker.

**Phase 5 -- Extraction hardening + relaunch.** Rewrite the extraction prompt to require a founding
relationship + emit only candidates; add the founding-relationship validation gate inline so the
perpetual scrape can never again assert affiliation-as-founder. Relaunch perpetual discovery behind
the full gate.
