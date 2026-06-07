"""Heuristic conversion of the prod startups_db.json (old flat schema) to the
new Pydantic schema (cornellians list, status enum, structured affiliation
fields), with no Gemini calls.

This is a defense-in-depth track that runs in parallel with reextract_all.
If backfill via live Gemini stalls, this still produces a complete v2 DB.
Backfill's per-record output overwrites the heuristic version when available.

Outputs:
- startup_output_test/startups_db_migrated.json  -- normalized to new schema
- startup_output_test/migrate_failures.jsonl      -- records that won't validate
- startup_output_test/migrate_summary.json        -- stats
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import ValidationError
from schema import StartupRecord, CornellianAffiliation
from url_canonical import canonicalize_url


# --- Heuristic parsers ------------------------------------------------------

_SCHOOL_PATTERNS = [
    (re.compile(r"cornell\s+tech|tech\s+campus|cornell\s+nyc\s+tech|roosevelt\s+island", re.I), "Cornell Tech"),
    (re.compile(r"weill|wcmc|medical\s+college", re.I), "Weill"),
    (re.compile(r"veterinary|vet\s+school|cvm\b", re.I), "Vet"),
    # If they mention Cornell but none of the above specialized schools, default to CU
]


def infer_school(text: str) -> str:
    if not text:
        return "unknown"
    for pat, school in _SCHOOL_PATTERNS:
        if pat.search(text):
            return school
    if re.search(r"cornell", text, re.I):
        return "CU"
    return "unknown"


_ROLE_PATTERNS = [
    (re.compile(r"\b(faculty|professor|associate\s+professor|assistant\s+professor)\b", re.I), "faculty"),
    (re.compile(r"\b(post.?doc|postdoctoral)\b", re.I), "postdoc"),
    (re.compile(r"\b(phd\s+student|graduate\s+student|undergraduate|current\s+student|enrolled)\b", re.I), "student"),
    (re.compile(r"\b(researcher|research\s+scientist|lab\s+member)\b", re.I), "researcher"),
    # Default to alumnus (most common); only used if nothing else matches
]


def infer_role(text: str) -> str:
    if not text:
        return "alumnus"
    for pat, role in _ROLE_PATTERNS:
        if pat.search(text):
            return role
    return "alumnus"


_YEAR_RE = re.compile(r"(?:class\s+of\s+|graduated\s+in\s+|',?|\b)((?:19|20)\d{2})\b")


def infer_grad_year(text: str) -> int | None:
    if not text:
        return None
    m = _YEAR_RE.search(text)
    if m:
        y = int(m.group(1))
        if 1860 <= y <= 2030:
            return y
    return None


_COMPANY_ROLE_PATTERNS = [
    (re.compile(r"\b(co.?founder|cofounder)\b", re.I), "cofounder"),
    (re.compile(r"\b(founder)\b", re.I), "founder"),
    (re.compile(r"\bceo\b", re.I), "ceo"),
    (re.compile(r"\bcto\b", re.I), "cto"),
    (re.compile(r"\bboard\b", re.I), "board"),
    (re.compile(r"\b(advisor|advisory)\b", re.I), "advisor"),
    (re.compile(r"\b(investor|partner\s+at)\b", re.I), "investor"),
    (re.compile(r"\b(early\s+employee|first\s+hire|employee\s+#?\d)\b", re.I), "early_employee"),
]


def infer_company_role(text: str, default: str = "founder") -> str:
    if not text:
        return default
    for pat, role in _COMPANY_ROLE_PATTERNS:
        if pat.search(text):
            return role
    return default


# --- Status / funding stage helpers -----------------------------------------

_STATUS_MAP = {
    "active": "active",
    "alive": "active",
    "operating": "active",
    "acquired": "acquired",
    "acquisition": "acquired",
    "shut down": "shutdown",
    "shutdown": "shutdown",
    "closed": "shutdown",
    "defunct": "shutdown",
    "ipo": "ipo",
    "public": "ipo",
    "listed": "ipo",
}


def infer_status(old_status: str | None, is_public: bool | None) -> str:
    if is_public:
        return "ipo"
    if not old_status:
        return "unknown"
    s = old_status.lower().strip()
    return _STATUS_MAP.get(s, "unknown")


_VALID_STAGES = {"pre-seed", "seed", "series-a", "series-b", "series-c",
                 "series-d", "series-e", "growth", "public", "unknown"}


def normalize_stage(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower().strip().replace(" ", "-")
    if s in _VALID_STAGES:
        return s
    # Map common variants
    if s in ("a", "series_a"):
        return "series-a"
    if s in ("b", "series_b"):
        return "series-b"
    if s in ("c", "series_c"):
        return "series-c"
    if s in ("preseed",):
        return "pre-seed"
    return None  # rather than the wrong category, drop it


# --- Industry → tags --------------------------------------------------------

def industry_to_tags(industry: str | None) -> list[str]:
    if not industry:
        return []
    raw = re.split(r"[,;/]", industry)
    return [t.strip() for t in raw if t.strip()]


# --- Main migration ---------------------------------------------------------

def migrate_record(old: dict) -> tuple[StartupRecord | None, str | None]:
    """Returns (StartupRecord, None) on success, (None, error_str) on failure."""
    name = old.get("company_name") or ""
    if not name:
        return None, "no company_name"

    # Build cornellians list. Old schema has cornellian_founder (str) and
    # affiliation_evidence (str). Some records have founders (list).
    founder_str = old.get("cornellian_founder") or ""
    affil_evidence = old.get("affiliation_evidence") or ""
    founders_list = old.get("founders") or []

    cornellians: list[CornellianAffiliation] = []
    # Source URL for provenance
    src_url = (
        old.get("proof_url") or old.get("source_url") or ""
    )

    # If cornellian_founder is missing or "Unknown", fall back to the broader
    # `founders` field, which is often present and useful even when the
    # narrower Cornell-founder field is empty.
    if not founder_str or founder_str.lower() in ("none", "unknown", "n/a"):
        if isinstance(founders_list, str):
            founder_str = founders_list
        elif isinstance(founders_list, list):
            founder_str = ", ".join(str(f) for f in founders_list if f)

    # Combine evidence sources: affiliation_evidence + description + affiliation_type
    desc = old.get("description") or ""
    aff_type = old.get("affiliation_type") or ""
    combined_evidence = (
        f"{affil_evidence} | {desc} | type={aff_type}".strip(" |")
        if (affil_evidence or desc or aff_type)
        else ""
    )

    if founder_str and founder_str.strip().lower() not in ("none", "unknown", "n/a", ""):
        # Sometimes it's "Sandy Weill, Jane Doe" -- split commas/ampersands
        for n in re.split(r",|;| and | & ", founder_str):
            n = n.strip()
            if not n or len(n) < 2 or n.lower() in ("none", "unknown", "n/a"):
                continue
            try:
                aff = CornellianAffiliation(
                    name=n,
                    school=infer_school(combined_evidence),
                    role=infer_role(combined_evidence + " " + aff_type),
                    grad_year=infer_grad_year(combined_evidence),
                    role_at_company=infer_company_role(combined_evidence, default="founder"),
                    evidence_span=(combined_evidence[:240] if combined_evidence else f"{n} (cornellian, migrated)"),
                    source_url=src_url or "https://startups.cornell.edu/",
                )
                cornellians.append(aff)
            except ValidationError:
                continue
    elif affil_evidence:
        # No founder name; capture a single migrated-evidence entry under "unknown"
        try:
            cornellians.append(CornellianAffiliation(
                name="(unspecified)",
                school=infer_school(affil_evidence),
                role=infer_role(affil_evidence),
                grad_year=infer_grad_year(affil_evidence),
                role_at_company="founder",
                evidence_span=affil_evidence[:240],
                source_url=src_url or "https://startups.cornell.edu/",
            ))
        except ValidationError:
            pass

    if not cornellians:
        return None, "no parseable cornellians"

    # Status / public / acquisition
    old_status = old.get("status")
    is_public = old.get("is_public")
    status_norm = infer_status(old_status, is_public)
    if is_public:
        status_norm = "ipo"

    # Pre-coerce "Unknown"/range strings on numeric fields the coercers don't tolerate.
    def _num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return v
        s = str(v).strip()
        if not s or s.lower() in ("unknown", "n/a", "na", "none", "null", "-"):
            return None
        # Employee-count style ranges: "51-200", "11-50", "25+", "5,000-10,000"
        m = re.match(r"^([\d,]+)\s*[-–to]+\s*([\d,]+)\+?$", s)
        if m:
            try:
                lo = int(m.group(1).replace(",", ""))
                hi = int(m.group(2).replace(",", ""))
                return (lo + hi) // 2
            except ValueError:
                return None
        m = re.match(r"^([\d,]+)\+$", s)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return None
        return v  # let the Pydantic coercer try

    # Build kwargs for StartupRecord
    kwargs = dict(
        company_name=name,
        cornellians=cornellians,
        proof_url=canonicalize_url(src_url) or src_url or "https://startups.cornell.edu/",
        description=old.get("description") or None,
        industry=old.get("industry") or None,
        funding_total_usd=_num(old.get("funding_total_usd")),
        funding_stage=normalize_stage(old.get("funding_stage")),
        funding_last_round_year=_num(old.get("funding_last_round_year")),
        founded_year=_num(old.get("founded_year")),
        employee_count=_num(old.get("employee_count")),
        is_public=is_public,
        headquarters=old.get("headquarters"),
        status=status_norm,
        exit_year=_num(old.get("exit_year")),
        acquirer=old.get("acquirer"),
        acquisition_amount_usd=_num(old.get("acquisition_amount_usd")),
        website_url=canonicalize_url(old.get("website_url")) if old.get("website_url") else None,
        linkedin_company_url=canonicalize_url(old.get("linkedin_url")) if old.get("linkedin_url") else None,
        crunchbase_url=canonicalize_url(old.get("crunchbase_url")) if old.get("crunchbase_url") else None,
        tags=industry_to_tags(old.get("industry")),
        non_cornell_cofounder_schools=[],
        first_seen_at=None,
        last_verified_at=None,
        validation_tier=old.get("validation_tier", "weak"),
        validation_issues=old.get("validation_issues", []) + ["migrated-from-v1-schema"],
    )

    try:
        rec = StartupRecord(**kwargs)
    except ValidationError as e:
        return None, f"validation: {e.errors()[0].get('msg', 'unknown')}"
    return rec, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="startup_output/startups_db.json")
    ap.add_argument("--out", default="startup_output_test/startups_db_migrated.json")
    ap.add_argument("--fail-log", default="startup_output_test/migrate_failures.jsonl")
    ap.add_argument("--summary", default="startup_output_test/migrate_summary.json")
    args = ap.parse_args()

    src = Path(args.db)
    out = Path(args.out)
    fail_log = Path(args.fail_log)
    summary_path = Path(args.summary)
    out.parent.mkdir(parents=True, exist_ok=True)
    fail_log.unlink(missing_ok=True)

    with src.open(encoding="utf-8") as f:
        db = json.load(f)
    records = db["records"] if isinstance(db, dict) and "records" in db else list(db.values())
    print(f"loaded {len(records)} legacy records")

    migrated = {}
    failure_reasons = Counter()
    tier_after = Counter()
    school_hist = Counter()
    role_hist = Counter()

    for r in records:
        rec, err = migrate_record(r)
        if rec is None:
            failure_reasons[err or "unknown"] += 1
            with fail_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"company": r.get("company_name", ""), "reason": err}) + "\n")
            continue
        key = rec.company_name.lower().strip()
        migrated[key] = rec.model_dump(mode="json")
        tier_after[rec.validation_tier] += 1
        for c in rec.cornellians:
            school_hist[c.school] += 1
            role_hist[c.role] += 1

    out.write_text(json.dumps(migrated, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "migrated_at": datetime.utcnow().isoformat() + "Z",
        "input_count": len(records),
        "migrated_count": len(migrated),
        "failed_count": sum(failure_reasons.values()),
        "failure_reasons": dict(failure_reasons.most_common()),
        "tier_distribution": dict(tier_after.most_common()),
        "school_distribution": dict(school_hist.most_common()),
        "role_distribution": dict(role_hist.most_common()),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nmigrated:   {len(migrated):,}")
    print(f"failed:     {sum(failure_reasons.values()):,}")
    print(f"top failure reasons: {dict(failure_reasons.most_common(5))}")
    print(f"tiers:      {dict(tier_after)}")
    print(f"schools:    {dict(school_hist)}")
    print(f"roles:      {dict(role_hist)}")
    print(f"\nv2 DB: {out}")
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()
