"""Apply the founder adjudication: build the cleaned dataset + Excel + rejects log.

Reads startups_db.json and adjudication_results.jsonl (verdict per company from
adjudicate_founders.py). Keeps only FOUNDER verdicts; everything else (employee,
executive, investor, donor, attendee, non-company, unclear, rule-dropped) goes to
a rejects file WITH its verdict + evidence + source, so the cleanup is auditable
and defensible to Marx -- nothing is silently deleted.

Outputs (in startup_output_overnight/):
  startups_db_clean.json          -- kept founder records only
  founders_rejected.json          -- dropped records with reason
  cornellian_founders_clean.xlsx  -- single Excel of the kept founders (the deliverable)

Non-destructive: never modifies startups_db.json.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

OUT = Path("startup_output_overnight")
DB = OUT / "startups_db.json"
RESULTS = OUT / "adjudication_results.jsonl"

KEEP = {"FOUNDER"}
EXCEL_COLS = ["company_name", "cornellian_founder", "affiliation_type",
              "proof_url", "source_domain", "affiliation_evidence", "verified"]


def _domain(u):
    return urlparse(u or "").netloc.lower().replace("www.", "")


def load_verdicts():
    v = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        v[o["company_name"]] = o
    return v


def write_excel(rows, path):
    """Prefer openpyxl; fall back to a CSV sibling if it's not installed."""
    try:
        from openpyxl import Workbook
    except ImportError:
        csv_path = path.with_suffix(".csv")
        import csv
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=EXCEL_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return csv_path
    wb = Workbook()
    ws = wb.active
    ws.title = "Cornellian Founders"
    ws.append([c.replace("_", " ").title() for c in EXCEL_COLS])
    for r in rows:
        ws.append([str(r.get(c, "") or "") for c in EXCEL_COLS])
    # freeze header + a sane width
    ws.freeze_panes = "A2"
    for i, c in enumerate(EXCEL_COLS, 1):
        ws.column_dimensions[chr(64 + i)].width = min(50, max(14, len(c) + 4))
    wb.save(path)
    return path


def main():
    recs = json.loads(DB.read_text(encoding="utf-8"))["records"]
    verdicts = load_verdicts()

    kept, rejected = [], []
    from collections import Counter
    tally = Counter()
    missing = 0
    for r in recs:
        nm = r.get("company_name")
        v = verdicts.get(nm)
        if v is None:
            missing += 1
            verdict = "UNADJUDICATED"
        else:
            verdict = v.get("verdict", "UNCLEAR")
        tally[verdict] += 1
        if verdict in KEEP:
            row = dict(r)
            row["source_domain"] = _domain(r.get("proof_url"))
            kept.append(row)
        else:
            rejected.append({"company_name": nm,
                             "cornellian_founder": r.get("cornellian_founder"),
                             "verdict": verdict,
                             "source_domain": _domain(r.get("proof_url")),
                             "evidence": (r.get("affiliation_evidence") or "")[:200]})

    (OUT / "startups_db_clean.json").write_text(
        json.dumps({"records": kept}, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "founders_rejected.json").write_text(
        json.dumps(rejected, indent=2, ensure_ascii=False), encoding="utf-8")
    xlsx = write_excel(kept, OUT / "cornellian_founders_clean.xlsx")

    print(f"total records:        {len(recs)}")
    print(f"KEPT (founders):      {len(kept)}")
    print(f"rejected:             {len(rejected)}")
    if missing:
        print(f"  (of which {missing} had no adjudication verdict yet)")
    print("\nverdict breakdown:")
    for k, c in tally.most_common():
        print(f"  {k:16s} {c}")
    print(f"\nwrote:\n  {OUT/'startups_db_clean.json'}\n  {OUT/'founders_rejected.json'}\n  {xlsx}")


if __name__ == "__main__":
    main()
