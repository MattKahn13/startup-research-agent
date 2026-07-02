"""One-off data repair: promote a record's legacy 'founders' value into
'cornellian_founder' wherever cornellian_founder is missing/garbage but
founders already contains something that looks like a real human name.

This recovers work that gap-fill ALREADY did correctly before the
founders/cornellian_founder field-mismatch fix (see the 2026-07-02 commit
"fix(gap-fill): read/write cornellian_founder, not the dead legacy
'founders' field") -- the correct answer was found and written to the wrong
field. Rather than have the agent re-search-and-re-discover these on a
future run, this promotes the already-known-good value directly.

Records where BOTH fields are bad/missing are left alone; the (now-fixed)
gap-fill pipeline will naturally re-target and discover them on future runs
since gap_report now correctly checks cornellian_founder.

Usage: python repair_stranded_founders.py [path/to/startups_db.json]
Writes the repaired DB back in place (after printing a dry-run summary),
and re-validates every touched record so validation_tier/validation_issues
reflect the correction.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from startup_researcher import _looks_like_human_name, validate_record


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("startup_output_overnight/startups_db.json")
    if not db_path.exists():
        print(f"not found: {db_path}", file=sys.stderr)
        return 1

    data = json.loads(db_path.read_text(encoding="utf-8"))
    recs = data.get("records", [])
    promoted = []

    for r in recs:
        cf = (r.get("cornellian_founder") or "").strip()
        fo = (r.get("founders") or "").strip()
        cf_bad = (not cf) or (not _looks_like_human_name(cf))
        if not cf_bad or not fo:
            continue
        primary = fo.split(",")[0].strip()
        if not _looks_like_human_name(primary):
            continue
        old_cf = cf
        r["cornellian_founder"] = primary
        evidence = (r.get("affiliation_evidence") or "").strip()
        if len(evidence) < 15:
            r["affiliation_evidence"] = f"Recovered from a prior gap-fill result: founders={fo!r}"
        validate_record(r)
        promoted.append((r.get("company_name"), old_cf, primary))

    print(f"promoted {len(promoted)} / {len(recs)} records:")
    for name, old, new in promoted:
        print(f"  {name!r}: {old!r} -> {new!r}")

    if promoted:
        db_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote repaired DB back to {db_path}")
    else:
        print("\nno changes needed; DB left untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
