"""Export the Cornell startup network as a graph:
- nodes_people.csv: one row per Cornellian
- nodes_companies.csv: one row per company
- edges_person_company.csv: (person, company, role, school)
- edges_cofounder.csv: (person_a, person_b, shared_companies, count)

Both Gephi-compatible CSV (id, label, attribute columns).
"""
from __future__ import annotations
import csv
import json
from collections import defaultdict, Counter
from pathlib import Path

CANDIDATES = [
    Path("startup_output_test/startups_db_enriched.json"),
    Path("startup_output_test/startups_db_deduped.json"),
    Path("startup_output_test/startups_db_migrated.json"),
]
OUT_DIR = Path("startup_output_test")


def pick_source() -> Path:
    for c in CANDIDATES:
        if c.exists():
            return c
    raise SystemExit("no DB to export")


def main():
    src = pick_source()
    db = json.loads(src.read_text(encoding="utf-8"))
    print(f"source: {src} | records: {len(db):,}")

    company_by_id: dict[str, dict] = {}
    person_by_id: dict[str, dict] = {}
    person_to_companies: defaultdict[str, list[str]] = defaultdict(list)
    company_to_people: defaultdict[str, list[str]] = defaultdict(list)

    def pid(name: str) -> str:
        return f"P:{name.strip()}"

    def cid(name: str) -> str:
        return f"C:{name.strip()}"

    for key, r in db.items():
        co_name = r.get("company_name", key)
        co_id = cid(co_name)
        company_by_id[co_id] = {
            "id": co_id,
            "label": co_name,
            "type": "company",
            "tier": r.get("validation_tier", ""),
            "status": r.get("status", ""),
            "founded_year": r.get("founded_year", ""),
            "funding_total_usd": r.get("funding_total_usd", ""),
            "industry": r.get("industry", ""),
            "headquarters": r.get("headquarters", ""),
            "url": r.get("wikipedia_url") or r.get("website_url") or r.get("proof_url", ""),
        }
        for c in r.get("cornellians", []):
            nm = (c.get("name") or "").strip()
            if not nm or nm.lower() in ("(unspecified)", "unknown"):
                continue
            p_id = pid(nm)
            if p_id not in person_by_id:
                person_by_id[p_id] = {
                    "id": p_id,
                    "label": nm,
                    "type": "person",
                    "school": c.get("school", "unknown"),
                    "role": c.get("role", "alumnus"),
                    "grad_year": c.get("grad_year", ""),
                }
            person_to_companies[p_id].append(co_id)
            company_to_people[co_id].append(p_id)

    # Edges person -> company
    edges_pc = []
    for p_id, co_ids in person_to_companies.items():
        for co_id in co_ids:
            edges_pc.append({
                "source": p_id,
                "target": co_id,
                "type": "founded_or_role",
                "person_name": person_by_id[p_id]["label"],
                "company_name": company_by_id[co_id]["label"],
                "school": person_by_id[p_id]["school"],
            })

    # Edges co-founder pairs (person-person)
    pair_count = Counter()
    pair_companies: defaultdict[tuple, set] = defaultdict(set)
    for co_id, people in company_to_people.items():
        people = sorted(set(people))
        for i, a in enumerate(people):
            for b in people[i + 1:]:
                key = (a, b)
                pair_count[key] += 1
                pair_companies[key].add(company_by_id[co_id]["label"])
    edges_cf = []
    for (a, b), n in pair_count.items():
        edges_cf.append({
            "source": a,
            "target": b,
            "weight": n,
            "person_a": person_by_id[a]["label"],
            "person_b": person_by_id[b]["label"],
            "companies": "; ".join(sorted(pair_companies[(a, b)])),
        })

    # Write CSVs
    def write_csv(path: Path, rows: list[dict]):
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write_csv(OUT_DIR / "nodes_people.csv", list(person_by_id.values()))
    write_csv(OUT_DIR / "nodes_companies.csv", list(company_by_id.values()))
    write_csv(OUT_DIR / "edges_person_company.csv", edges_pc)
    write_csv(OUT_DIR / "edges_cofounder.csv", edges_cf)

    print(f"nodes_people.csv:           {len(person_by_id):,}")
    print(f"nodes_companies.csv:        {len(company_by_id):,}")
    print(f"edges_person_company.csv:   {len(edges_pc):,}")
    print(f"edges_cofounder.csv:        {len(edges_cf):,}")

    # Quick network stats
    degree = {p_id: len(set(cs)) for p_id, cs in person_to_companies.items()}
    top_degree = sorted(degree.items(), key=lambda kv: -kv[1])[:15]
    print("\nMost-connected Cornellians (#companies):")
    for p_id, n in top_degree:
        print(f"  {n:>3}  {person_by_id[p_id]['label']} ({person_by_id[p_id]['school']})")


if __name__ == "__main__":
    main()
