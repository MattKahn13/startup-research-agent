"""Aggressive deduplication of the migrated DB based on canonical company name.
Strips suffixes (Inc, LLC, Corp, Ltd), parens, "the", whitespace.

Merges duplicate records: unions cornellians (by name), funding_total_usd =
max of values, prefers higher tier, concatenates validation_issues.

Outputs:
- startup_output_test/startups_db_deduped.json
- startup_output_test/dedup_report.md  (which records merged into which)
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

INPUT = Path("startup_output_test/startups_db_migrated.json")
OUT = Path("startup_output_test/startups_db_deduped.json")
OUT_REPORT = Path("startup_output_test/dedup_report.md")


_NAME_SUFFIXES = re.compile(
    r"\b(inc\.?|incorporated|llc|l\.l\.c\.|ltd\.?|limited|corp\.?|corporation|"
    r"co\.?|company|gmbh|s\.a\.|s\.r\.l\.|plc|holdings?|technologies|"
    r"labs?|industries|systems)\b",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\([^)]*\)")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def canonical_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = _PAREN.sub(" ", n)        # strip parenthetical descriptors
    n = _NAME_SUFFIXES.sub(" ", n) # strip corporate suffixes
    n = _PUNCT.sub(" ", n)
    n = _WS.sub(" ", n).strip()
    # Drop leading "the "
    if n.startswith("the "):
        n = n[4:]
    return n


TIER_RANK = {"high": 3, "provisional": 2, "weak": 1}


def merge_two(a: dict, b: dict) -> dict:
    """Merge b into a (b's data fills gaps and may overwrite when stronger)."""
    out = dict(a)
    # Prefer higher tier
    if TIER_RANK.get(b.get("validation_tier", "weak"), 1) > \
       TIER_RANK.get(out.get("validation_tier", "weak"), 1):
        out["validation_tier"] = b.get("validation_tier")

    # Union cornellians by name
    by_name = {c.get("name", ""): c for c in out.get("cornellians", [])}
    for c in b.get("cornellians", []):
        nm = c.get("name", "")
        if nm and nm not in by_name:
            by_name[nm] = c
    out["cornellians"] = list(by_name.values())

    # Tags: union
    out["tags"] = list({*out.get("tags", []), *b.get("tags", [])})

    # validation_issues: union, mark merged
    issues = set(out.get("validation_issues", []) + b.get("validation_issues", []))
    issues.add(f"deduped-with:{b.get('company_name', '?')}")
    out["validation_issues"] = sorted(issues)

    # Scalar fields: prefer non-null; for funding/employee, prefer larger
    def _fill(field):
        if out.get(field) in (None, "", "unknown") and b.get(field) not in (None, "", "unknown"):
            out[field] = b[field]

    def _max(field):
        av, bv = out.get(field), b.get(field)
        try:
            if av is None and bv is not None:
                out[field] = bv
            elif av is not None and bv is not None:
                out[field] = max(av, bv)
        except TypeError:
            pass

    for f in ("description", "industry", "funding_stage", "funding_last_round_year",
              "founded_year", "is_public", "headquarters", "exit_year", "acquirer",
              "website_url", "linkedin_company_url", "crunchbase_url"):
        _fill(f)
    for f in ("funding_total_usd", "employee_count", "acquisition_amount_usd"):
        _max(f)

    # Status: prefer non-"unknown"
    if out.get("status", "unknown") == "unknown" and b.get("status", "unknown") != "unknown":
        out["status"] = b["status"]

    return out


def main():
    db = json.loads(INPUT.read_text(encoding="utf-8"))
    print(f"loaded {len(db):,} migrated records")

    # Build canonical-name -> [record_keys] map
    canon_to_keys: defaultdict[str, list[str]] = defaultdict(list)
    for k, r in db.items():
        cn = canonical_name(r.get("company_name", ""))
        canon_to_keys[cn].append(k)

    merge_groups = {cn: ks for cn, ks in canon_to_keys.items() if len(ks) > 1 and cn}
    print(f"found {len(merge_groups):,} canonical names with duplicates "
          f"({sum(len(v) for v in merge_groups.values()):,} records involved)")

    deduped: dict = {}
    merge_log: list[dict] = []

    # Records with unique canonical name: just copy
    for cn, ks in canon_to_keys.items():
        if not cn:
            # Empty canonical (e.g. blank company_name) — preserve under original key
            for k in ks:
                deduped[k] = db[k]
            continue
        if len(ks) == 1:
            deduped[cn] = db[ks[0]]
            continue
        # Multiple records share canonical name → merge
        # Pick the highest-tier record as base, then merge others
        ks_sorted = sorted(ks, key=lambda k: -TIER_RANK.get(db[k].get("validation_tier", "weak"), 1))
        base = db[ks_sorted[0]]
        for k in ks_sorted[1:]:
            base = merge_two(base, db[k])
        deduped[cn] = base
        merge_log.append({
            "canonical": cn,
            "original_names": [db[k].get("company_name") for k in ks_sorted],
            "kept_tier": deduped[cn].get("validation_tier"),
        })

    OUT.write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(deduped):,} deduped records to {OUT}")
    print(f"removed {len(db) - len(deduped):,} duplicates")

    # Markdown report
    lines = ["# Dedup Report", "",
             f"Input: {len(db):,} migrated records",
             f"Output: {len(deduped):,} deduped records",
             f"Removed duplicates: **{len(db) - len(deduped):,}**", "",
             "## Largest merge groups", ""]
    for entry in sorted(merge_log, key=lambda e: -len(e["original_names"]))[:25]:
        lines.append(f"### Canonical: `{entry['canonical']}` "
                     f"(kept tier: {entry['kept_tier']})")
        for n in entry["original_names"]:
            lines.append(f"- {n}")
        lines.append("")
    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"report: {OUT_REPORT}")


if __name__ == "__main__":
    main()
