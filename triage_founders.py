"""Non-destructive triage of the Cornellian-founders dataset.

Marx's review exposed a systemic conflation: the pipeline records any Cornellian
who appears next to a company as its "founder", regardless of whether they
FOUNDED it, work there, run it, donated to it, or merely attended. This script
classifies every record into buckets with a reason, WITHOUT deleting anything,
so the scale is visible and the bar is a human decision.

Buckets (first matching rule wins):
  reject:cornell-entity   -- Cornell's own centers/schools/programs, not startups
  reject:investment       -- VC / PE / foundation / fund / accelerator (Matt: exclude VCs)
  reject:employment       -- evidence shows employment/exec/donor role, no founding language
  keep:founding-language  -- evidence contains an explicit founding assertion
  review:no-founding      -- no clear founding language, but not clearly one of the rejects
                             (these still need LLM adjudication before keep/drop)

Writes triage_report.json (full per-record verdicts) and prints counts + samples.
Reads startups_db.json read-only; changes nothing.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

DB = Path("startup_output_overnight/startups_db.json")

FOUND = re.compile(
    r"\b(founded|co-?founded|co-?founder|founder of|founding (?:member|team|partner)|"
    r"started the company|started the firm|launched the (?:company|startup|firm)|"
    r"co-?created|spun out|spin-?out|incorporated the)\b", re.I)
EMPLOY = re.compile(
    r"\b(now (?:at|a|an|applied|senior|principal)|works? at|working at|joined|"
    r"employee|analyst|(?:hr |product |program )?manager|consultant|"
    r"engineer at|scientist at|researcher at|director at|"
    r"chair(?:man|woman|person)?|ceo of|cto|cfo|coo|president of|"
    r"vice president|vp |board member|advisor|intern|associate at|"
    r"partner at|alma mater|donor|gift from|support from|named after|namesake)\b", re.I)
CORNELL_ENT = re.compile(r"^(the\s+)?(cornell|weill cornell)\b", re.I)
INVEST = re.compile(
    r"\b(ventures|venture partners|venture capital|capital partners|capital management|"
    r"\bVC\b|private equity|foundation|\bfund\b|\bfunds\b|accelerator|incubator|"
    r"angel(?:s| group| network)|holdings|advisors|endowment)\b", re.I)


def classify(r):
    name = (r.get("company_name") or "").strip()
    ev = (r.get("affiliation_evidence") or "")
    founder = (r.get("cornellian_founder") or "")
    hay = f"{ev} {founder} {name}"

    if CORNELL_ENT.search(name):
        return "reject:cornell-entity", f"name starts with Cornell: {name!r}"
    m = INVEST.search(name)
    if m:
        return "reject:investment", f"name matches investment/foundation term {m.group(0)!r}"
    has_found = bool(FOUND.search(hay))
    has_employ = bool(EMPLOY.search(ev))
    if has_employ and not has_found:
        return "reject:employment", "evidence shows employment/exec/donor role, no founding language"
    if has_found:
        return "keep:founding-language", "evidence contains a founding assertion"
    return "review:no-founding", "no explicit founding language in evidence"


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB
    recs = json.loads(db_path.read_text(encoding="utf-8"))["records"]
    buckets = {}
    verdicts = []
    for r in recs:
        b, reason = classify(r)
        buckets.setdefault(b, []).append(r.get("company_name"))
        verdicts.append({"company_name": r.get("company_name"),
                         "cornellian_founder": r.get("cornellian_founder"),
                         "affiliation_type": r.get("affiliation_type"),
                         "bucket": b, "reason": reason,
                         "evidence": (r.get("affiliation_evidence") or "")[:200]})

    counts = Counter(v["bucket"] for v in verdicts)
    total = len(recs)
    print(f"TOTAL records: {total}\n")
    print("BUCKET BREAKDOWN")
    order = ["keep:founding-language", "review:no-founding", "reject:employment",
             "reject:cornell-entity", "reject:investment"]
    for b in order:
        c = counts.get(b, 0)
        print(f"  {b:26s} {c:5d}  ({100*c//total}%)")
    print()
    for b in order:
        names = [n for n in buckets.get(b, []) if n][:12]
        print(f"--- sample: {b}")
        for n in names:
            print(f"     {n}")
        print()

    out = db_path.parent / "triage_report.json"
    out.write_text(json.dumps({"total": total, "counts": dict(counts),
                               "verdicts": verdicts}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"wrote {out}  (per-record verdicts; nothing in the DB was modified)")


if __name__ == "__main__":
    main()
