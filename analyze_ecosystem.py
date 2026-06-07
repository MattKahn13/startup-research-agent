"""Ecosystem analysis from the migrated Cornell-startup DB. Reads either
the heuristic-migrated DB or the backfill v2 DB (preferring v2 when present),
produces a JSON stats file and a markdown summary.

No external services. Pure local computation.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

MIGRATED = Path("startup_output_test/startups_db_migrated.json")
DEDUPED = Path("startup_output_test/startups_db_deduped.json")
BACKFILL_V2 = Path("startup_output_test/startups_db_v2.json")
OUT_JSON = Path("startup_output_test/ecosystem_stats.json")
OUT_MD = Path("startup_output_test/ECOSYSTEM_REPORT.md")


def load_merged() -> tuple[dict, dict]:
    """Load deduped DB (preferred) or migrated DB; overlay backfill v2 records.
    Returns (merged_dict, sources_dict) where sources_dict tracks origin per key."""
    merged: dict = {}
    sources: dict = {}
    base = DEDUPED if DEDUPED.exists() else MIGRATED
    if base.exists():
        m = json.loads(base.read_text(encoding="utf-8"))
        for k, v in m.items():
            merged[k] = v
            sources[k] = "deduped" if base == DEDUPED else "migrated"
    if BACKFILL_V2.exists():
        try:
            b = json.loads(BACKFILL_V2.read_text(encoding="utf-8"))
            for k, v in b.items():
                merged[k] = v
                sources[k] = "backfill"
        except Exception:
            pass
    return merged, sources


def safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def analyze(merged: dict, sources: dict) -> dict:
    schools = Counter()
    roles = Counter()
    statuses = Counter()
    tiers = Counter()
    industries = Counter()
    headquarters = Counter()
    funding_stages = Counter()
    funded_count = 0
    public_count = 0
    acquired_count = 0
    total_funding = 0
    founding_year_hist = Counter()
    cornellian_to_companies: defaultdict[str, list[str]] = defaultdict(list)
    cofounder_pairs = Counter()
    backfill_count = sum(1 for s in sources.values() if s == "backfill")

    top_funded = []
    top_acquisitions = []

    for key, r in merged.items():
        name = r.get("company_name", key)
        tier = r.get("validation_tier", "weak")
        tiers[tier] += 1
        if r.get("industry"):
            industries[r["industry"]] += 1
        if r.get("headquarters"):
            headquarters[r["headquarters"]] += 1
        st = r.get("status", "unknown")
        statuses[st] += 1
        if st == "ipo":
            public_count += 1
        if st == "acquired":
            acquired_count += 1
        if r.get("funding_stage"):
            funding_stages[r["funding_stage"]] += 1
        ftu = safe_int(r.get("funding_total_usd"))
        if ftu:
            funded_count += 1
            total_funding += ftu
            top_funded.append((ftu, name, st))
        amt = safe_int(r.get("acquisition_amount_usd"))
        if amt:
            top_acquisitions.append((amt, name, r.get("acquirer") or "?"))
        fy = safe_int(r.get("founded_year"))
        if fy and 1900 <= fy <= 2030:
            founding_year_hist[fy] += 1

        cornellians = r.get("cornellians") or []
        names_here = []
        for c in cornellians:
            schools[c.get("school", "unknown")] += 1
            roles[c.get("role", "alumnus")] += 1
            pn = c.get("name", "").strip()
            if pn and pn.lower() not in ("(unspecified)", "unknown"):
                names_here.append(pn)
                cornellian_to_companies[pn].append(name)
        for i, a in enumerate(names_here):
            for b in names_here[i + 1:]:
                pair = tuple(sorted([a, b]))
                cofounder_pairs[pair] += 1

    top_funded.sort(reverse=True)
    top_acquisitions.sort(reverse=True)
    multi_company = {n: cs for n, cs in cornellian_to_companies.items() if len(cs) > 1}

    decade_hist = Counter()
    for y, n in founding_year_hist.items():
        decade_hist[(y // 10) * 10] += n

    stats = {
        "total_records": len(merged),
        "source_origin": {
            "from_backfill": backfill_count,
            "from_migration": len(merged) - backfill_count,
        },
        "validation_tier_distribution": dict(tiers),
        "status_distribution": dict(statuses),
        "public_count": public_count,
        "acquired_count": acquired_count,
        "funded_count": funded_count,
        "total_disclosed_funding_usd": total_funding,
        "average_funding_when_disclosed_usd": (total_funding // funded_count) if funded_count else 0,
        "top_funded_records": [
            {"company": n, "funding_usd": f, "status": s}
            for f, n, s in top_funded[:20]
        ],
        "top_acquisitions": [
            {"company": n, "amount_usd": a, "acquirer": acq}
            for a, n, acq in top_acquisitions[:10]
        ],
        "school_distribution": dict(schools.most_common()),
        "role_distribution": dict(roles.most_common()),
        "funding_stage_distribution": dict(funding_stages.most_common()),
        "top_industries": dict(industries.most_common(20)),
        "top_headquarters": dict(headquarters.most_common(15)),
        "founding_decades": dict(sorted(decade_hist.items())),
        "multi_company_cornellians": {n: cs for n, cs in
                                     sorted(multi_company.items(),
                                            key=lambda kv: -len(kv[1]))[:25]},
        "multi_company_cornellians_count": len(multi_company),
        "top_cofounder_pairs": [
            {"pair": list(p), "count": c}
            for p, c in cofounder_pairs.most_common(15)
        ],
    }
    return stats


def make_markdown(stats: dict, n_input: int) -> str:
    lines: list[str] = []
    p = lines.append
    p("# Cornell Startup Ecosystem -- Report")
    p("")
    p(f"Generated from `startups_db_migrated.json` (and `startups_db_v2.json` where present). "
      f"Total records: **{stats['total_records']:,}** "
      f"({stats['source_origin']['from_backfill']:,} from live re-extract, "
      f"{stats['source_origin']['from_migration']:,} from heuristic migration of legacy DB).")
    p("")
    p("## Tier and status")
    p("")
    p("| Validation tier | Count |")
    p("|---|---:|")
    for t, n in stats["validation_tier_distribution"].items():
        p(f"| {t} | {n:,} |")
    p("")
    p("| Status | Count |")
    p("|---|---:|")
    for s, n in stats["status_distribution"].items():
        p(f"| {s} | {n:,} |")
    p("")
    p(f"- Public (IPO): {stats['public_count']:,}")
    p(f"- Acquired: {stats['acquired_count']:,}")
    p(f"- Companies with disclosed funding: {stats['funded_count']:,}")
    p(f"- Total disclosed funding across the dataset: **${stats['total_disclosed_funding_usd']:,}**")
    p(f"- Mean disclosed-funding round: ${stats['average_funding_when_disclosed_usd']:,}")
    p("")
    p("## School distribution (Cornellian affiliations)")
    p("")
    p("| School | Affiliations |")
    p("|---|---:|")
    for s, n in stats["school_distribution"].items():
        p(f"| {s} | {n:,} |")
    p("")
    p("## Role at Cornell")
    p("")
    p("| Role | Affiliations |")
    p("|---|---:|")
    for s, n in stats["role_distribution"].items():
        p(f"| {s} | {n:,} |")
    p("")
    p("## Top 20 funded companies")
    p("")
    p("| Company | Funding (USD) | Status |")
    p("|---|---:|---|")
    for r in stats["top_funded_records"]:
        p(f"| {r['company']} | ${r['funding_usd']:,} | {r['status']} |")
    p("")
    p("## Top acquisitions")
    p("")
    p("| Company | Amount | Acquirer |")
    p("|---|---:|---|")
    for r in stats["top_acquisitions"]:
        p(f"| {r['company']} | ${r['amount_usd']:,} | {r['acquirer']} |")
    p("")
    p("## Top industries")
    p("")
    p("| Industry | Companies |")
    p("|---|---:|")
    for ind, n in stats["top_industries"].items():
        p(f"| {ind} | {n:,} |")
    p("")
    p("## Top headquarters locations")
    p("")
    p("| Headquarters | Companies |")
    p("|---|---:|")
    for h, n in stats["top_headquarters"].items():
        p(f"| {h} | {n:,} |")
    p("")
    p("## Founding decades")
    p("")
    p("| Decade | Companies |")
    p("|---|---:|")
    for d, n in stats["founding_decades"].items():
        p(f"| {d}s | {n:,} |")
    p("")
    p("## Funding stage")
    p("")
    p("| Stage | Count |")
    p("|---|---:|")
    for s, n in stats["funding_stage_distribution"].items():
        p(f"| {s} | {n:,} |")
    p("")
    p(f"## Multi-company Cornellians ({stats['multi_company_cornellians_count']:,} total)")
    p("")
    p("Top 25 founders who appear in multiple companies in this dataset. "
      "Network effect signal.")
    p("")
    p("| Founder | # Companies | Companies |")
    p("|---|---:|---|")
    for name, cs in stats["multi_company_cornellians"].items():
        listing = ", ".join(cs[:6]) + (f", +{len(cs)-6} more" if len(cs) > 6 else "")
        p(f"| {name} | {len(cs)} | {listing} |")
    p("")
    p("## Top co-founder pairs")
    p("")
    p("| Pair | Companies together |")
    p("|---|---:|")
    for r in stats["top_cofounder_pairs"]:
        a, b = r["pair"]
        p(f"| {a} ⇄ {b} | {r['count']} |")
    p("")
    p("---")
    p("")
    p(f"Source: 1,525 records in `startup_output/startups_db.json`. "
      f"Migration recovered {n_input:,} of those into the new schema.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrated", default=str(MIGRATED))
    args = ap.parse_args()

    merged, sources = load_merged()
    n_input = len(merged)
    if not merged:
        print("no migrated or backfill data to analyze; aborting.")
        return 1
    stats = analyze(merged, sources)

    OUT_JSON.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    md = make_markdown(stats, n_input)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"wrote {OUT_JSON} and {OUT_MD}")
    print(f"  records analyzed: {stats['total_records']:,}")
    print(f"  from backfill:    {stats['source_origin']['from_backfill']:,}")
    print(f"  schools top 5:    {dict(list(stats['school_distribution'].items())[:5])}")
    print(f"  multi-company founders: {stats['multi_company_cornellians_count']:,}")


if __name__ == "__main__":
    main()
