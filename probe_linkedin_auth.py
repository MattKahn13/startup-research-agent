"""Same target set as probe_linkedin.py but using saved LinkedIn cookies.
Goal: show what data unlocks when authenticated vs the logged-out probe.

Writes probe_linkedin_auth_results.jsonl + a side-by-side summary.
"""
from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gemini_tool import init_driver, quit_driver
from linkedin_login import load_cookies, COOKIE_FILE

TARGETS = [
    ("citigroup",     "https://www.linkedin.com/company/citigroup/about/"),
    ("hyro",          "https://www.linkedin.com/company/hyro-ai/about/"),
    ("nanit",         "https://www.linkedin.com/company/nanit/about/"),
    ("cornell-edu",   "https://www.linkedin.com/school/cornell-university/about/"),
    ("reid-hoffman",  "https://www.linkedin.com/in/reidhoffman/"),
]

OUT = Path("probe_linkedin_auth_results.jsonl")
SUMMARY = Path("probe_linkedin_auth_summary.md")

_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_OG = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', re.I)
_FOUNDED = re.compile(r"\bFounded\b\s*[:\-]?\s*(\d{4})", re.I)
_EMP_EXACT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{2,7})\s+employees?", re.I)
_HQ = re.compile(r"\bHeadquarters\b\s*[:\-]?\s*([^\n<]{4,150})", re.I)
_SPECIALTIES = re.compile(r"\bSpecialties\b\s*[:\-]?\s*([^\n<]{4,400})", re.I)
_WEBSITE = re.compile(r'href="(https?://[^"]+)"[^>]*>\s*<span[^>]*>\s*Website', re.I)
_INDUSTRY = re.compile(r"\bIndustry\b\s*[:\-]?\s*([^\n<]{4,80})", re.I)
_COMPANY_SIZE = re.compile(r"\bCompany size\b\s*[:\-]?\s*([^\n<]{4,80})", re.I)
_TYPE = re.compile(r"\bType\b\s*[:\-]?\s*([A-Z][^\n<]{3,60})")


def analyze(html: str) -> dict:
    out = {}
    for name, pat in [
        ("title", _TITLE), ("og_description", _OG),
        ("founded_year", _FOUNDED), ("employees_exact", _EMP_EXACT),
        ("headquarters", _HQ), ("specialties", _SPECIALTIES),
        ("website", _WEBSITE), ("industry", _INDUSTRY),
        ("company_size", _COMPANY_SIZE), ("type", _TYPE),
    ]:
        m = pat.search(html)
        if m:
            out[name] = m.group(1).strip()[:300]
    return out


def main():
    if not Path(COOKIE_FILE).exists():
        print(f"No cookies at {COOKIE_FILE}. Run linkedin_login.py first.")
        return 1

    print("opening headless Chrome and loading LinkedIn cookies...")
    driver = init_driver(headless=True)
    try:
        driver.get("https://www.linkedin.com/")
        time.sleep(2)
        loaded = load_cookies(driver, COOKIE_FILE)
        print(f"  loaded {loaded} cookies")

        results = []
        for name, url in TARGETS:
            print(f"\n=== {name} ===  {url}")
            t0 = time.time()
            try:
                driver.get(url)
                time.sleep(4)
            except Exception as e:
                print(f"  ERROR navigating: {e}")
                continue
            final = driver.current_url
            html = driver.page_source
            sig = analyze(html)
            sig.update({
                "target_name": name, "url": url,
                "final_url": final, "bytes": len(html),
                "latency_s": round(time.time() - t0, 1),
            })
            results.append(sig)
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sig, ensure_ascii=False) + "\n")
            verdict = []
            if "/login" in final or "/authwall" in final:
                verdict.append("AUTH_WALL")
            for k in ("founded_year", "employees_exact", "headquarters", "industry",
                      "company_size", "specialties", "website"):
                if sig.get(k):
                    verdict.append(f"{k}={str(sig[k])[:30]}")
            print(f"  bytes={sig['bytes']:,} -> {' | '.join(verdict) if verdict else '(only meta)'}")
    finally:
        try:
            quit_driver(driver)
        except Exception:
            pass

    # Markdown summary
    lines = ["# LinkedIn auth-mode probe", "",
             "Same target set as the logged-out probe (probe_linkedin_summary.md). "
             "Selenium headless + saved cookies.", "",
             "| Target | Bytes | Founded | Employees | HQ | Industry | Size | Website | Specialties |",
             "|---|---:|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['target_name']} | {r['bytes']:,} | "
            f"{r.get('founded_year','-')} | {r.get('employees_exact','-')} | "
            f"{(r.get('headquarters','-') or '-')[:30]} | "
            f"{(r.get('industry','-') or '-')[:30]} | "
            f"{(r.get('company_size','-') or '-')[:30]} | "
            f"{'Y' if r.get('website') else '-'} | "
            f"{'Y' if r.get('specialties') else '-'} |"
        )
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT} and {SUMMARY}")


if __name__ == "__main__":
    main()
