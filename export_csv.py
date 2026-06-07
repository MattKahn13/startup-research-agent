"""Export the deduped (or enriched) DB to a flat CSV for spreadsheet review.
Picks the most-enriched available file: enriched > deduped > migrated.

Two CSVs:
- startups.csv: one row per company (flat scalar fields + first cornellian collapsed)
- cornellians.csv: one row per (cornellian, company) edge (long format for network analysis)
"""
from __future__ import annotations
import csv
import json
from pathlib import Path


CANDIDATES = [
    Path("startup_output_test/startups_db_enriched.json"),
    Path("startup_output_test/startups_db_deduped.json"),
    Path("startup_output_test/startups_db_migrated.json"),
]

OUT_COMPANIES = Path("startup_output_test/startups.csv")
OUT_PEOPLE = Path("startup_output_test/cornellians.csv")


def pick_source() -> Path:
    for c in CANDIDATES:
        if c.exists():
            return c
    raise SystemExit("no DB to export")


COMPANY_FIELDS = [
    "company_name", "validation_tier", "status", "founded_year", "exit_year",
    "acquirer", "acquisition_amount_usd", "funding_total_usd", "funding_stage",
    "funding_last_round_year", "employee_count", "is_public", "industry",
    "headquarters", "tags", "website_url", "wikipedia_url", "linkedin_company_url",
    "crunchbase_url", "proof_url", "description", "cornellian_count",
    "cornellian_schools", "cornellian_names", "validation_issues",
]


def export_companies(db: dict) -> int:
    OUT_COMPANIES.parent.mkdir(parents=True, exist_ok=True)
    with OUT_COMPANIES.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMPANY_FIELDS)
        w.writeheader()
        for key, r in db.items():
            cornellians = r.get("cornellians") or []
            schools = sorted({c.get("school", "") for c in cornellians if c.get("school")})
            names = [c.get("name", "") for c in cornellians if c.get("name")]
            row = {
                "company_name": r.get("company_name", key),
                "validation_tier": r.get("validation_tier", ""),
                "status": r.get("status", ""),
                "founded_year": r.get("founded_year", ""),
                "exit_year": r.get("exit_year", ""),
                "acquirer": r.get("acquirer", ""),
                "acquisition_amount_usd": r.get("acquisition_amount_usd", ""),
                "funding_total_usd": r.get("funding_total_usd", ""),
                "funding_stage": r.get("funding_stage", ""),
                "funding_last_round_year": r.get("funding_last_round_year", ""),
                "employee_count": r.get("employee_count", ""),
                "is_public": r.get("is_public", ""),
                "industry": r.get("industry", ""),
                "headquarters": r.get("headquarters", ""),
                "tags": "; ".join(r.get("tags", [])),
                "website_url": r.get("website_url", ""),
                "wikipedia_url": r.get("wikipedia_url", ""),
                "linkedin_company_url": r.get("linkedin_company_url", ""),
                "crunchbase_url": r.get("crunchbase_url", ""),
                "proof_url": r.get("proof_url", ""),
                "description": r.get("description", ""),
                "cornellian_count": len(cornellians),
                "cornellian_schools": "; ".join(schools),
                "cornellian_names": "; ".join(names),
                "validation_issues": "; ".join(r.get("validation_issues", [])),
            }
            w.writerow(row)
    return len(db)


def export_people(db: dict) -> int:
    OUT_PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with OUT_PEOPLE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "name", "school", "role", "grad_year", "role_at_company",
            "company_name", "company_tier", "company_status",
            "company_funding_usd", "evidence_span", "source_url",
        ])
        for key, r in db.items():
            for c in r.get("cornellians", []):
                w.writerow([
                    c.get("name", ""),
                    c.get("school", ""),
                    c.get("role", ""),
                    c.get("grad_year", ""),
                    c.get("role_at_company", ""),
                    r.get("company_name", key),
                    r.get("validation_tier", ""),
                    r.get("status", ""),
                    r.get("funding_total_usd", ""),
                    (c.get("evidence_span", "") or "")[:300],
                    c.get("source_url", ""),
                ])
                rows += 1
    return rows


def main():
    src = pick_source()
    db = json.loads(src.read_text(encoding="utf-8"))
    print(f"source: {src}")
    n_co = export_companies(db)
    n_p = export_people(db)
    print(f"companies.csv:    {n_co:,} rows -> {OUT_COMPANIES}")
    print(f"cornellians.csv:  {n_p:,} rows -> {OUT_PEOPLE}")


if __name__ == "__main__":
    main()
