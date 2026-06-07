"""Discover candidate Cornell-affiliated companies via Wikipedia category traversal.
Pulls members of relevant categories, filters out obvious non-companies, then
checks which are already in the deduped DB (by canonical name).

Outputs:
- startup_output_test/wiki_candidates_raw.json    -- raw category members
- startup_output_test/wiki_candidates_new.json    -- candidates NOT in our DB
- startup_output_test/wiki_candidates_report.md   -- human-readable

This is a NEW-records track, complementing the existing-records ironing track.
No CAPTCHA risk; only Wikipedia API HTTP calls.
"""
from __future__ import annotations
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEDUPED = Path("startup_output_test/startups_db_deduped.json")
OUT_RAW = Path("startup_output_test/wiki_candidates_raw.json")
OUT_NEW = Path("startup_output_test/wiki_candidates_new.json")
OUT_MD = Path("startup_output_test/wiki_candidates_report.md")

USER_AGENT = "startup-research-agent/1.0 (Cornell Startup Directory)"
CAT_API = ("https://en.wikipedia.org/w/api.php?action=query&list=categorymembers"
           "&cmtitle={cat}&cmlimit=500&format=json&cmtype=page")
SUM_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"


# Categories that historically contain Cornell-affiliated companies.
# Wikipedia category names are case-sensitive and exact.
CATEGORIES = [
    "Category:Companies based in Ithaca, New York",
    "Category:American companies established in 2020",   # noisy but useful for recent
    "Category:Software companies based in New York City",
    # The most useful direct categories
    "Category:Cornell University-related lists",
    # Less direct but worth trying
    "Category:Companies established in 2010",
    "Category:Companies established in 2015",
    "Category:Companies established in 2020",
]


def fetch_json(url: str, retries: int = 2) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            time.sleep(1 + attempt)
    return None


def list_category_members(category: str) -> list[str]:
    url = CAT_API.format(cat=urllib.parse.quote(category))
    data = fetch_json(url)
    if not data:
        return []
    members = data.get("query", {}).get("categorymembers", [])
    return [m["title"] for m in members if "title" in m]


def looks_like_company(title: str) -> bool:
    # Heuristic: titles like "List of..." or category-like names aren't companies
    if title.startswith("List of") or title.startswith("Category:"):
        return False
    if " (disambiguation)" in title:
        return False
    return True


def has_cornell_signal(title: str) -> bool:
    """Quick-look: does the article mention Cornell in its summary?"""
    url = SUM_API.format(title=urllib.parse.quote(title))
    data = fetch_json(url)
    if not data:
        return False
    text = ((data.get("description") or "") + " " + (data.get("extract") or "")).lower()
    return "cornell" in text


def canonical_name(name: str) -> str:
    """Match the dedup script's canonicalization."""
    n = re.sub(r"\([^)]*\)", " ", name.lower())
    n = re.sub(r"\b(inc\.?|llc|ltd\.?|corp\.?|corporation|co\.?|company)\b", " ", n)
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n.startswith("the "):
        n = n[4:]
    return n


def main():
    print("loading deduped DB...")
    db = json.loads(DEDUPED.read_text(encoding="utf-8"))
    known = {canonical_name(r.get("company_name", "")) for r in db.values()}
    print(f"  known canonical names: {len(known):,}")

    all_titles: set[str] = set()
    per_category: dict[str, list[str]] = {}

    for cat in CATEGORIES:
        print(f"fetching {cat}...")
        members = list_category_members(cat)
        per_category[cat] = members
        print(f"  {len(members)} members")
        for t in members:
            if looks_like_company(t):
                all_titles.add(t)
        time.sleep(0.5)

    OUT_RAW.write_text(json.dumps({"per_category": per_category,
                                    "company_candidates": sorted(all_titles)},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nfound {len(all_titles)} candidate companies across {len(CATEGORIES)} categories")

    # Filter to new (not in DB)
    new_titles = [t for t in all_titles if canonical_name(t) not in known]
    print(f"  not in deduped DB: {len(new_titles)}")

    # Confirm Cornell signal for the new ones
    confirmed = []
    rejected = []
    for i, t in enumerate(new_titles, 1):
        if has_cornell_signal(t):
            confirmed.append(t)
        else:
            rejected.append(t)
        time.sleep(0.3)
        if i % 25 == 0:
            print(f"  signal-check {i}/{len(new_titles)}  confirmed={len(confirmed)}")

    OUT_NEW.write_text(json.dumps({"confirmed": confirmed,
                                    "rejected_no_signal": rejected,
                                    "total_checked": len(new_titles)},
                                   indent=2, ensure_ascii=False), encoding="utf-8")

    lines = ["# Wikipedia Candidate Discovery", "",
             f"Categories scanned: {len(CATEGORIES)}",
             f"Raw company-shaped titles: {len(all_titles):,}",
             f"Not in deduped DB: {len(new_titles):,}",
             f"**Cornell-signal confirmed: {len(confirmed):,}**", "",
             "## Confirmed new candidates"]
    for t in confirmed:
        lines.append(f"- [{t}](https://en.wikipedia.org/wiki/{urllib.parse.quote(t)})")
    if rejected:
        lines += ["", "## Rejected (no Cornell mention in summary)"]
        for t in rejected[:30]:
            lines.append(f"- {t}")
        if len(rejected) > 30:
            lines.append(f"- ... and {len(rejected) - 30} more")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nconfirmed: {len(confirmed)}  rejected: {len(rejected)}")
    print(f"report: {OUT_MD}")


if __name__ == "__main__":
    main()
