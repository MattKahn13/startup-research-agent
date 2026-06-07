"""Authenticated LinkedIn probe + JSON-LD/voyager parser.

For each target URL:
  1. Fetch with saved cookies via headless Selenium
  2. Dump raw HTML to probe_responses/linkedin/<name>.html for inspection
  3. Parse JSON-LD Organization records + voyager SSR state JSON for structured fields

Outputs:
  - probe_responses/linkedin/<name>.html  (raw)
  - probe_linkedin_parsed.json            (structured results, all targets)
"""
from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup

from gemini_tool import init_driver, quit_driver
from linkedin_login import load_cookies, COOKIE_FILE


TARGETS = [
    ("citigroup",    "https://www.linkedin.com/company/citigroup/about/"),
    ("hyro",         "https://www.linkedin.com/company/hyro-ai/about/"),
    ("nanit",        "https://www.linkedin.com/company/nanit/about/"),
    ("cornell-edu",  "https://www.linkedin.com/school/cornell-university/about/"),
    ("reid-hoffman", "https://www.linkedin.com/in/reidhoffman/"),
]

DUMP_DIR = Path("probe_responses/linkedin")
OUT = Path("probe_linkedin_parsed.json")


def parse_jsonld(soup) -> dict:
    """Extract Organization fields from JSON-LD blocks."""
    out: dict = {}
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = (tag.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        records = data if isinstance(data, list) else [data]
        for r in records:
            t = r.get("@type")
            if t in ("Organization", "Corporation", "EducationalOrganization",
                     "CollegeOrUniversity", "Person"):
                if not out.get("name") and r.get("name"):
                    out["name"] = r["name"]
                if not out.get("url") and r.get("url"):
                    out["url"] = r["url"]
                # Organization
                if r.get("foundingDate"):
                    out["founding_date"] = r["foundingDate"]
                if r.get("description"):
                    out["description"] = r["description"][:500]
                emp = r.get("numberOfEmployees")
                if isinstance(emp, dict):
                    if emp.get("minValue") is not None:
                        out["employees_min"] = emp["minValue"]
                    if emp.get("maxValue") is not None:
                        out["employees_max"] = emp["maxValue"]
                addr = r.get("address")
                if isinstance(addr, dict):
                    out["hq_locality"] = addr.get("addressLocality")
                    out["hq_region"] = addr.get("addressRegion")
                    out["hq_country"] = addr.get("addressCountry")
                if r.get("sameAs"):
                    out["same_as"] = r["sameAs"]
                # Person
                if t == "Person":
                    if r.get("jobTitle"):
                        out["job_title"] = r["jobTitle"]
                    if r.get("worksFor"):
                        out["works_for"] = r["worksFor"]
                    if r.get("alumniOf"):
                        out["alumni_of"] = r["alumniOf"]
    return out


# Voyager SSR JSON regex patterns.
# Note: LinkedIn embeds both a SCHEMA definition `"key":{"type":"long"}` and
# REAL VALUE rows `"key":<value>` in the same blob. Patterns below use literal
# integers / strings to avoid matching schema rows.
_VOYAGER_PATTERNS = {
    # Real-value forms only
    "employee_count":      re.compile(r'"employeeCount"\s*:\s*(\d{1,8})\b'),
    "follower_count":      re.compile(r'"followerCount"\s*:\s*(\d{1,9})\b'),
    "staff_count_range":   re.compile(r'"staffCountRange"\s*:\s*"([A-Z0-9_]+)"'),
    "founded_year":        re.compile(r'"foundedOn"\s*:\s*\{\s*[^\}]*?"year"\s*:\s*(\d{4})'),
    "tagline":             re.compile(r'"tagline"\s*:\s*"([^"]{1,300})"'),
    "name":                re.compile(r'"name"\s*:\s*"([^"]{1,200})"\s*,\s*"tagline"'),
    "website":             re.compile(r'"companyPageUrl"\s*:\s*"(https?://[^"@]+)"'),
    # Company website: exclude URLs containing @ (mis-entered email addresses
    # in LinkedIn's CTA field show up as bogus "url" values) and exclude linkedin.com.
    "company_url":         re.compile(r'"url"\s*:\s*"(https?://(?!www\.linkedin)[^"@]+)"'),
    "specialities":        re.compile(r'"specialities"\s*:\s*\[("[^\]]{0,2000})\]'),
    # Headquarters: find first address with "headquarter":true and back-extract
    "_hq_block":           re.compile(
        r'(\{[^{]*?"\$type"\s*:\s*"com\.linkedin\.voyager\.dash\.organization\.LocationGroup"[^{]*?\})',
        re.DOTALL),
}

# Address-in-context patterns (run inside an HQ block once located)
_ADDR_CITY = re.compile(r'"city"\s*:\s*"([^"]+)"')
_ADDR_REGION = re.compile(r'"geographicArea"\s*:\s*"([^"]+)"')
_ADDR_COUNTRY = re.compile(r'"country"\s*:\s*"([A-Z]{2,3})"')
_ADDR_POSTAL = re.compile(r'"postalCode"\s*:\s*"([^"]+)"')
_ADDR_LINE1 = re.compile(r'"line1"\s*:\s*"([^"]+)"')

# Person-profile fields (for /in/ URLs).
# LinkedIn's nav embeds the logged-in user's own profile, so the FIRST
# "headline":"..." match on every page is the session user's, not the target.
# We collect all headlines and pick the one belonging to the URL's publicIdentifier,
# or fall back to the longest non-session match.
_ANY_HEADLINE = re.compile(r'"headline"\s*:\s*"([^"]{1,400})"')
_PUBLIC_IDENT_HEADLINE = re.compile(
    r'"publicIdentifier"\s*:\s*"{slug}"[^{{]{{0,2000}}?"headline"\s*:\s*"([^"]{{1,400}})"',
)
# Person education/positions
_ALUMNI_OF = re.compile(r'"schoolName"\s*:\s*"([^"]{1,150})"')
_CURRENT_POS = re.compile(r'"companyName"\s*:\s*"([^"]{1,150})"')


def _parse_hq(html: str) -> dict:
    """Find an address dict where 'headquarter' is true and pull its city/region/country/line1."""
    # Look for address blocks with "headquarter":true nearby (within 600 chars).
    # The data is embedded; we slice around each match.
    out: dict = {}
    for m in re.finditer(r'"headquarter"\s*:\s*true', html):
        start = max(0, m.start() - 700)
        end = min(len(html), m.start() + 200)
        ctx = html[start:end]
        city = _ADDR_CITY.search(ctx)
        region = _ADDR_REGION.search(ctx)
        country = _ADDR_COUNTRY.search(ctx)
        postal = _ADDR_POSTAL.search(ctx)
        line1 = _ADDR_LINE1.search(ctx)
        if city or country or line1:
            if city: out["hq_city"] = city.group(1)
            if region: out["hq_region"] = region.group(1)
            if country: out["hq_country"] = country.group(1)
            if postal: out["hq_postal"] = postal.group(1)
            if line1: out["hq_line1"] = line1.group(1)
            return out
    return out


def parse_voyager(html: str, target_url: str = "") -> dict:
    out: dict = {}
    for key, pat in _VOYAGER_PATTERNS.items():
        if key.startswith("_"):
            continue
        m = pat.search(html)
        if not m:
            continue
        val = m.group(1)
        if key in ("employee_count", "follower_count"):
            try:
                val = int(val)
            except ValueError:
                pass
        elif key == "specialities":
            try:
                arr = json.loads("[" + val + "]")
                val = [s for s in arr if isinstance(s, str)][:30]
            except json.JSONDecodeError:
                val = [s.strip().strip('"') for s in val.split(",")][:30]
        out[key] = val
    # Headquarters block parsing
    out.update(_parse_hq(html))
    # Person profile handling (only on /in/ URLs)
    if "/in/" in target_url:
        out.update(_parse_person(html, target_url))
    return out


def _parse_person(html: str, target_url: str) -> dict:
    """Pull headline + schools + current company for a /in/ profile, skipping
    the logged-in session user's data (which appears in the nav of every page)."""
    out: dict = {}
    # Extract slug from URL: /in/<slug>/...
    m = re.search(r"/in/([^/?#]+)", target_url)
    if not m:
        return out
    slug = re.escape(m.group(1))
    # Try the targeted publicIdentifier-near-headline pattern first
    pat = re.compile(_PUBLIC_IDENT_HEADLINE.pattern.format(slug=slug), re.DOTALL)
    pm = pat.search(html)
    if pm:
        out["person_headline"] = pm.group(1)
    else:
        # Fallback: longest headline value in the document (target's bio tends
        # to be far longer than the session user's nav headline).
        candidates = [m.group(1) for m in _ANY_HEADLINE.finditer(html)]
        if candidates:
            longest = max(candidates, key=len)
            out["person_headline"] = longest
    # Schools / "alumniOf" candidates
    schools = []
    for sm in _ALUMNI_OF.finditer(html):
        name = sm.group(1)
        if name and name not in schools and "linkedin" not in name.lower():
            schools.append(name)
        if len(schools) >= 8:
            break
    if schools:
        out["schools"] = schools
    return out


def main():
    if not Path(COOKIE_FILE).exists():
        print(f"No cookies at {COOKIE_FILE}.")
        return 1
    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    print("opening headless Chrome and loading LinkedIn cookies...")
    driver = init_driver(headless=True)
    try:
        driver.set_page_load_timeout(120)
        driver.get("https://www.linkedin.com/")
        time.sleep(2)
        n = load_cookies(driver, COOKIE_FILE)
        print(f"  loaded {n} cookies")
    except Exception as e:
        print(f"setup error: {e}")
        try: quit_driver(driver)
        except Exception: pass
        return 1

    results = {}
    try:
        for name, url in TARGETS:
            print(f"\n=== {name} === {url}")
            t0 = time.time()
            try:
                driver.get(url)
                time.sleep(5)
            except Exception as e:
                print(f"  navigate ERROR: {type(e).__name__}: {e}")
                results[name] = {"error": f"navigate: {e}"}
                continue
            html = driver.page_source
            final = driver.current_url
            (DUMP_DIR / f"{name}.html").write_text(html, encoding="utf-8")
            soup = BeautifulSoup(html, "lxml")
            jsonld = parse_jsonld(soup)
            voyager = parse_voyager(html, target_url=url)
            merged = {
                "final_url": final,
                "bytes": len(html),
                "latency_s": round(time.time() - t0, 1),
                "jsonld": jsonld,
                "voyager": voyager,
            }
            results[name] = merged
            keys = []
            if jsonld: keys.append(f"jsonld:{len(jsonld)}")
            if voyager: keys.append(f"voyager:{len(voyager)}")
            print(f"  bytes={len(html):,}  final={final[:80]}  fields={','.join(keys)}")
            # Highlights
            highlights = []
            for src in (jsonld, voyager):
                for k in ("name", "founding_date", "founded_year", "staff_count",
                         "staff_count_range", "follower_count", "hq_city",
                         "hq_locality", "industry", "industry_v2", "tagline",
                         "headline", "current_position"):
                    if src.get(k):
                        highlights.append(f"{k}={str(src[k])[:50]}")
            if highlights:
                print("    " + " | ".join(highlights[:7]))
    finally:
        try: quit_driver(driver)
        except Exception: pass

    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT}")
    print(f"raw HTML dumps in {DUMP_DIR}")


if __name__ == "__main__":
    main()
