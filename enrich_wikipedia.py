"""Enrich the deduped Cornell-startup DB with Wikipedia data: headquarters,
founded_year, status (active/acquired/ipo/shutdown), website, acquirer,
employee_count (when present in the infobox).

Strategy: for each company in the deduped DB, query Wikipedia's REST/MediaWiki
APIs to find a matching article. Parse the infobox + lead paragraph.
No login, no Selenium, no CAPTCHA risk -- only HTTP to wikipedia.org.

Wikipedia REST endpoints used:
- GET /api/rest_v1/page/summary/{title} -- canonical title, description, image
- GET /w/api.php?action=parse&format=json&page=... -- infobox HTML for parsing

Outputs:
- startup_output_test/wiki_enriched.json  -- per-record enrichment payloads
- startup_output_test/startups_db_enriched.json -- merged result
- startup_output_test/wiki_summary.json
"""
from __future__ import annotations
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

DEDUPED = Path("startup_output_test/startups_db_deduped.json")
ENRICH_PAYLOADS = Path("startup_output_test/wiki_enriched.json")
ENRICHED = Path("startup_output_test/startups_db_enriched.json")
SUMMARY = Path("startup_output_test/wiki_summary.json")

USER_AGENT = "startup-research-agent/1.0 (Cornell Startup Directory; matt@example.com)"
SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
PARSE_URL = ("https://en.wikipedia.org/w/api.php?action=parse&prop=text|properties"
             "&format=json&redirects=1&page={title}")


def fetch(url: str, retries: int = 2) -> dict | None:
    """Fetch URL as JSON. Returns None on 404 or persistent failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(5 + attempt * 5)
                continue
            time.sleep(1 + attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(1 + attempt)
    return None


# Common disambiguation suffixes to try for tech-company titles
_TITLE_VARIANTS = [
    "{name}",
    "{name} (company)",
    "{name} (firm)",
    "{name} Inc.",
    "{name}, Inc.",
    "{name} Corporation",
]


def find_article(name: str) -> dict | None:
    """Try a few Wikipedia title variants. Return the first one that resolves
    to a non-disambiguation article. Accepts company-shaped descriptions and
    falls through to first non-disambig match otherwise."""
    cleaned = name.strip()
    # Strip trailing "Inc." / "LLC" etc -- the title variants try them anyway
    cleaned_short = re.sub(r"[,]?\s*(Inc\.?|LLC|Ltd\.?|Corp\.?|Corporation)$", "",
                            cleaned, flags=re.I).strip()
    candidates = []
    for tmpl in _TITLE_VARIANTS:
        for nm in (cleaned, cleaned_short):
            if not nm:
                continue
            title = tmpl.format(name=nm)
            url = SUMMARY_URL.format(title=urllib.parse.quote(title))
            data = fetch(url)
            if not data or not data.get("title"):
                continue
            if data.get("type") == "disambiguation":
                continue
            candidates.append(data)
            # Strong-confirm via description/extract
            desc = (data.get("description") or "").lower()
            extract = (data.get("extract") or "").lower()
            text = desc + " " + extract[:400]
            if any(kw in text for kw in ("company", "startup", "firm", "corporation",
                                        "founded", "headquartered", "subsidiary",
                                        "acquired", "raised", "venture", "fund",
                                        "bank", "technology", "software", "platform",
                                        "biotech", "pharmaceuticals", "labs")):
                return data
    return candidates[0] if candidates else None


_FOUNDED_RE = re.compile(r"\b(founded|established|incorporated)\s+(?:in\s+)?(\d{4})", re.I)
_ACQUIRED_RE = re.compile(r"\bacquired\s+(?:by|in\s+\d{4}\s+by)?\s+([^.,;]+?)(?:\s+(?:in|for|on)\b|\.|,|;|$)", re.I)
_HQ_RE = re.compile(r"\b(?:headquartered|based)\s+in\s+([^.,;]+?)(?:\.|,|;|$)", re.I)
_IPO_RE = re.compile(r"\b(?:went public|ipo|listed on|initial public offering)\b", re.I)
_SHUTDOWN_RE = re.compile(r"\b(?:shut down|ceased operations|defunct|dissolved|wound down)\b", re.I)


def parse_extract(text: str) -> dict:
    """Pull lightweight facts out of a Wikipedia article summary/lead."""
    out: dict = {}
    if not text:
        return out
    text_l = text.lower()

    m = _FOUNDED_RE.search(text)
    if m:
        try:
            year = int(m.group(2))
            if 1800 <= year <= 2030:
                out["founded_year"] = year
        except ValueError:
            pass

    m = _ACQUIRED_RE.search(text_l)
    if m:
        out["status"] = "acquired"
        # The capture is lowercase, so re-find in original text
        m2 = _ACQUIRED_RE.search(text)
        if m2:
            acquirer = m2.group(1).strip().rstrip(".").rstrip(",")
            if acquirer and len(acquirer) < 80:
                out["acquirer"] = acquirer

    if _IPO_RE.search(text_l):
        out["status"] = "ipo"
        out["is_public"] = True

    if _SHUTDOWN_RE.search(text_l) and "status" not in out:
        out["status"] = "shutdown"

    m = _HQ_RE.search(text)
    if m:
        hq = m.group(1).strip().rstrip(".").rstrip(",")
        if hq and len(hq) < 120:
            out["headquarters"] = hq

    return out


def enrich_one(name: str) -> dict | None:
    article = find_article(name)
    if not article:
        return None
    extract = article.get("extract") or ""
    info = parse_extract(extract)
    info["wiki_title"] = article.get("title")
    info["wiki_url"] = (article.get("content_urls", {})
                       .get("desktop", {}).get("page") or
                       f"https://en.wikipedia.org/wiki/{urllib.parse.quote(article.get('title', name))}")
    info["wiki_description"] = article.get("description")
    info["wiki_extract"] = extract[:600]
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEDUPED), help="source DB to enrich")
    ap.add_argument("--max", type=int, default=0, help="0 = all records")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between requests")
    ap.add_argument("--rerun", action="store_true",
                    help="re-query even records we've already enriched")
    args = ap.parse_args()

    db = json.loads(Path(args.src).read_text(encoding="utf-8"))
    existing_payload = {}
    if ENRICH_PAYLOADS.exists() and not args.rerun:
        existing_payload = json.loads(ENRICH_PAYLOADS.read_text(encoding="utf-8"))

    items = list(db.items())
    if args.max:
        items = items[:args.max]

    hits = 0
    misses = 0
    new_status_set = 0
    new_hq_set = 0
    new_year_set = 0

    payloads = dict(existing_payload)
    for i, (key, rec) in enumerate(items, 1):
        name = rec.get("company_name", "")
        if not name:
            continue
        if name in payloads:
            continue
        info = enrich_one(name)
        if info is None:
            misses += 1
            payloads[name] = {"_miss": True}
        else:
            hits += 1
            payloads[name] = info
            if info.get("status"):
                new_status_set += 1
            if info.get("headquarters"):
                new_hq_set += 1
            if info.get("founded_year"):
                new_year_set += 1
        time.sleep(args.sleep)
        if i % 25 == 0:
            ENRICH_PAYLOADS.write_text(json.dumps(payloads, indent=2,
                                                 ensure_ascii=False), encoding="utf-8")
            print(f"  [{i:>4}/{len(items)}] hits={hits} misses={misses} "
                  f"status+{new_status_set} hq+{new_hq_set} year+{new_year_set}")

    ENRICH_PAYLOADS.write_text(json.dumps(payloads, indent=2, ensure_ascii=False),
                                encoding="utf-8")

    # Merge into a new DB file
    merged = {}
    for key, rec in db.items():
        name = rec.get("company_name", "")
        wiki = payloads.get(name, {})
        if wiki and not wiki.get("_miss"):
            new = dict(rec)
            for f in ("status", "founded_year", "headquarters", "acquirer"):
                old_v = new.get(f)
                new_v = wiki.get(f)
                if new_v and (old_v in (None, "", "unknown", 0)):
                    new[f] = new_v
            if wiki.get("is_public") and not new.get("is_public"):
                new["is_public"] = True
            if wiki.get("wiki_url"):
                new["wikipedia_url"] = wiki["wiki_url"]
            new["validation_issues"] = sorted(set(
                new.get("validation_issues", []) + ["wikipedia-enriched"]
            ))
            merged[key] = new
        else:
            merged[key] = rec
    ENRICHED.write_text(json.dumps(merged, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    summary = {
        "records_queried": len(items),
        "wikipedia_hits": hits,
        "wikipedia_misses": misses,
        "hit_rate": round(hits / max(len(items), 1), 3),
        "fields_filled": {
            "status": new_status_set,
            "headquarters": new_hq_set,
            "founded_year": new_year_set,
        },
    }
    SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nwiki hits: {hits} / {len(items)} ({summary['hit_rate']*100:.1f}%)")
    print(f"status filled: {new_status_set} | hq filled: {new_hq_set} | year filled: {new_year_set}")
    print(f"output: {ENRICHED}")


if __name__ == "__main__":
    main()
