"""Merge every signal per candidate -> a verified/rejected/needs_human decision
with confidence + provenance, then write the deliverables. The recovery verdict
overrides an UNCLEAR adjudication; Wikidata/OpenCorporates agreement is the
structured-agreement shortcut. Reuses apply_adjudication's Excel writer."""
import json
from pathlib import Path
from urllib.parse import urlparse

from verify.confidence import score_edge
from verify.contradiction import founder_matches_evidence
from verify.publish import decide

OUT = Path("startup_output_overnight")
DB = OUT / "startups_db.json"
RESULTS = OUT / "adjudication_results.jsonl"
RECOVERY = OUT / "unclear_recovery.jsonl"

# Curated Cornell-startup directories -- a listing here is a founding assertion by
# the source itself, so it earns the "directory" tier (vs a bare press "mention").
DIRECTORY_DOMAINS = {
    "bigredai.com", "eship.cornell.edu", "tech.cornell.edu", "elabstartup.com",
    "startups.cornell.edu", "pce.cornell.edu",
}


def _domain(u):
    return urlparse(u or "").netloc.lower().replace("www.", "")


def source_tier_for(record):
    return "directory" if _domain(record.get("proof_url")) in DIRECTORY_DOMAINS else "mention"


def _cornell_tie(record):
    return "strong" if (record.get("affiliation_type") or "").strip() else "weak"


def entity_status(company_check, wikidata_confirms):
    """Cross-reference the company against entity-verification sources and NOTE the
    result. Inability to prove the entity is recorded, never used to disqualify --
    a real company can exist without appearing in any registry we can reach. Returns
    {verified: True|False|None, note: str}:
      True  -- an external source confirms the entity exists
      False -- a name-rule positively flagged it as a non-company (fund/unit/etc.)
      None  -- we could not confirm it; a note is made, and it is NOT disqualifying
    """
    real = company_check.get("company_real")
    src = company_check.get("source") or "registry"
    et = company_check.get("entity_type") or "unknown"
    if real is True:
        return {"verified": True, "note": f"entity confirmed ({src})"}
    if wikidata_confirms:
        return {"verified": True, "note": "entity confirmed (wikidata)"}
    if real is False:
        return {"verified": False, "note": f"flagged non-company ({et}, name-rule)"}
    return {"verified": None,
            "note": "entity UNVERIFIED -- no registry/Wikidata match (not disqualifying)"}


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
    es = entity_status(company_check, wikidata_confirms)
    return {**record, "state": d["state"], "reason": d["reason"],
            "confidence": s["confidence"], "provenance": s["provenance"],
            "entity_verified": es["verified"], "entity_note": es["note"]}


def _load_jsonl_verdicts(path):
    v = {}
    if not path.exists():
        return v
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            v[o["company_name"]] = o.get("verdict")
    return v


def main():
    import verify.wikidata as wd
    import verify.real_company as rc
    import apply_adjudication as aa

    recs = json.loads(DB.read_text(encoding="utf-8"))["records"]
    adj = _load_jsonl_verdicts(RESULTS)
    recovery = _load_jsonl_verdicts(RECOVERY)

    try:
        wiki = wd.cornell_founded_companies()
    except Exception as e:
        print(f"[warn] Wikidata unavailable ({e}); proceeding without API corroboration")
        wiki = {}

    company_cache = {}
    rows = []
    for r in recs:
        nm = r.get("company_name")
        person = r.get("cornellian_founder")
        if nm not in company_cache:
            try:
                company_cache[nm] = rc.check_company(nm)
            except Exception:
                company_cache[nm] = {"company_real": None, "entity_type": "unknown"}
        wik = bool(wiki) and wd.confirms_founding(nm, person)
        rows.append(merge_signals(
            record=r, adj_verdict=adj.get(nm), recovery_verdict=recovery.get(nm),
            wikidata_confirms=wik, company_check=company_cache[nm],
            source_tier=source_tier_for(r)))

    verified = sorted([x for x in rows if x["state"] == "verified"],
                      key=lambda x: x["confidence"], reverse=True)
    needs_human = [x for x in rows if x["state"] == "needs_human"]
    rejected = [x for x in rows if x["state"] == "rejected"]

    for x in verified:
        x["confidence_provenance"] = f'{x["confidence"]:.2f} | ' + "; ".join(x["provenance"])
        x["source_domain"] = _domain(x.get("proof_url"))

    aa.EXCEL_COLS = ["company_name", "cornellian_founder", "affiliation_type",
                     "confidence_provenance", "entity_note", "proof_url", "source_domain",
                     "affiliation_evidence"]
    xlsx = aa.write_excel(verified, OUT / "cornellian_founders_verified.xlsx")
    (OUT / "founders_needs_human.json").write_text(
        json.dumps(needs_human, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "founders_rejected.json").write_text(
        json.dumps(rejected, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"total candidates: {len(rows)}")
    print(f"  verified:     {len(verified)}")
    print(f"  needs_human:  {len(needs_human)}")
    print(f"  rejected:     {len(rejected)}")
    print(f"\nwrote:\n  {xlsx}\n  {OUT/'founders_needs_human.json'}\n  {OUT/'founders_rejected.json'}")


if __name__ == "__main__":
    main()
