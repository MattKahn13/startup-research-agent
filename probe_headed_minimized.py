"""Empirical test of the wiki claim:

  > Default to headed. Window can be minimized (--window-size=1,1 +
  > --window-position=-10000,-10000) but the process runs as a real Chrome
  > session. (wiki/anti-patterns/headless-default.md)

If true, the v2 spec's "Q workstream" can stop treating Selenium-Google as the
last-resort rung and treat it as a perfectly viable default. The HTTP engines
become a speed/parallelism win, not a CAPTCHA-avoidance necessity.

Test plan:

  1. Google search x 5 queries via headed-minimized Selenium. Did any trigger
     a CAPTCHA / interstitial? How many returned real results?
  2. LinkedIn /in/ profile x 3 fast visits via headed-minimized Selenium with
     saved cookies. Did the lite-variant throttle fire? Did the headline
     extract reliably?
  3. A control: Google search x 1 query in HEADLESS for direct comparison.

Logs to probe_headed_minimized.jsonl + probe_headed_minimized_summary.md.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gemini_tool import init_driver, quit_driver

from linkedin_login import load_cookies, COOKIE_FILE


GOOGLE_QUERIES = [
    "site:linkedin.com/in/ cornell founder",
    "Ava Labs founder cornell university",
    "Hyro company linkedin",
    '"Cornell University" startup AI funding 2024',
    "Cornell entrepreneurship eship",
]

LINKEDIN_TARGETS = [
    ("nanit", "https://www.linkedin.com/company/nanit/about/"),
    ("reid-hoffman", "https://www.linkedin.com/in/reidhoffman/"),
    ("cornell-edu", "https://www.linkedin.com/school/cornell-university/about/"),
]

RESULTS = Path("probe_headed_minimized.jsonl")
SUMMARY = Path("probe_headed_minimized_summary.md")


# Heuristics
_CAPTCHA = re.compile(
    r"(captcha|unusual traffic|are you a robot|verify (?:you are|that you)|"
    r"to continue, please complete the security check|recaptcha|"
    r"this page checks if|/sorry/index)",
    re.I,
)
_GOOGLE_RESULT = re.compile(r'<div class="g[ \"\'](.+?)</div>', re.S)
_GOOGLE_RESULT_ANCHOR = re.compile(r'<a [^>]*href="(?!/url\?q=|#)(https?://[^"]+)"[^>]*>', re.I)
_LINKEDIN_HEADLINE = re.compile(r'"headline"\s*:\s*"([^"]{1,400})"')


def _log(rec: dict) -> None:
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def probe_google(driver, query: str, mode: str) -> dict:
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    t0 = time.time()
    try:
        driver.get(url)
        time.sleep(3)
    except Exception as e:
        return {"phase": "google", "mode": mode, "query": query,
                "error": f"{type(e).__name__}: {e}",
                "latency_s": round(time.time() - t0, 1)}
    html = driver.page_source
    final = driver.current_url
    captcha = bool(_CAPTCHA.search(html))
    on_sorry = "/sorry/" in final
    # Count "anchor to non-google https" as result-like markers
    anchors = _GOOGLE_RESULT_ANCHOR.findall(html)
    # Filter out google's own internal links
    external = [a for a in anchors if not a.startswith("https://www.google.com")
                and not a.startswith("https://accounts.google.com")
                and not a.startswith("https://maps.google.com")
                and not a.startswith("https://policies.google.com")
                and not a.startswith("https://support.google.com")]
    rec = {
        "phase": "google", "mode": mode, "query": query,
        "final_url": final, "bytes": len(html),
        "latency_s": round(time.time() - t0, 1),
        "captcha": captcha,
        "on_sorry_page": on_sorry,
        "external_anchors": len(external),
        "sample_externals": external[:5],
    }
    _log(rec)
    return rec


def probe_linkedin(driver, name: str, url: str, target_slug: str | None = None) -> dict:
    t0 = time.time()
    try:
        driver.get(url)
        time.sleep(4)
    except Exception as e:
        return {"phase": "linkedin", "target": name, "url": url,
                "error": f"{type(e).__name__}: {e}",
                "latency_s": round(time.time() - t0, 1)}
    final = driver.current_url
    html = driver.page_source
    headlines = _LINKEDIN_HEADLINE.findall(html)
    longest_headline = max(headlines, key=len) if headlines else None
    rec = {
        "phase": "linkedin", "target": name, "url": url,
        "final_url": final, "bytes": len(html),
        "latency_s": round(time.time() - t0, 1),
        "headline_count": len(headlines),
        "longest_headline": (longest_headline or "")[:200],
        "title_match": "<title>" in html and any(t in html for t in (name,)),
    }
    _log(rec)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-google", action="store_true")
    ap.add_argument("--skip-linkedin", action="store_true")
    ap.add_argument("--skip-headless-control", action="store_true")
    args = ap.parse_args()
    RESULTS.unlink(missing_ok=True)

    google_records = []
    linkedin_records = []
    headless_control_record = None

    # ---- 1. Google with HEADED-minimized -------------------------------
    if not args.skip_google:
        print("=== Google headed-minimized ===")
        d = init_driver(headless=False)
        try:
            for q in GOOGLE_QUERIES:
                r = probe_google(d, q, mode="headed_minimized")
                verdict = []
                if r.get("error"):
                    verdict.append(f"ERROR: {r['error']}")
                else:
                    if r["captcha"]: verdict.append("CAPTCHA")
                    if r["on_sorry_page"]: verdict.append("ON_SORRY")
                    verdict.append(f"externals={r['external_anchors']}")
                print(f"  {q!r}: " + " | ".join(verdict))
                google_records.append(r)
                time.sleep(2)
        finally:
            quit_driver(d)

    # ---- 2. LinkedIn with HEADED-minimized + cookies --------------------
    if not args.skip_linkedin and Path(COOKIE_FILE).exists():
        print("\n=== LinkedIn headed-minimized (with auth cookies) ===")
        d = init_driver(headless=False)
        try:
            d.set_page_load_timeout(90)
            d.get("https://www.linkedin.com/")
            time.sleep(2)
            n = load_cookies(d, COOKIE_FILE)
            print(f"  loaded {n} cookies")
            for name, url in LINKEDIN_TARGETS:
                r = probe_linkedin(d, name, url)
                if r.get("error"):
                    verdict = f"ERROR: {r['error']}"
                else:
                    verdict = (f"bytes={r['bytes']:,} headlines={r['headline_count']} "
                               f"longest_head={r['longest_headline'][:60]!r}")
                print(f"  {name}: {verdict}")
                linkedin_records.append(r)
                time.sleep(3)
        finally:
            quit_driver(d)

    # ---- 3. HEADLESS control on Google ---------------------------------
    if not args.skip_headless_control:
        print("\n=== Google HEADLESS control (one query for comparison) ===")
        d = init_driver(headless=True)
        try:
            r = probe_google(d, GOOGLE_QUERIES[0], mode="headless_control")
            verdict = []
            if r.get("error"):
                verdict.append(f"ERROR: {r['error']}")
            else:
                if r["captcha"]: verdict.append("CAPTCHA")
                if r["on_sorry_page"]: verdict.append("ON_SORRY")
                verdict.append(f"externals={r['external_anchors']}")
            print(f"  {GOOGLE_QUERIES[0]!r}: " + " | ".join(verdict))
            headless_control_record = r
        finally:
            quit_driver(d)

    # ---- Summary ---------------------------------------------------------
    lines = ["# Headed-minimized Selenium probe", ""]
    if google_records:
        g_ok = sum(1 for r in google_records
                   if not r.get("captcha") and not r.get("on_sorry_page")
                   and r.get("external_anchors", 0) >= 3)
        lines.append(f"## Google headed-minimized: {g_ok} / {len(google_records)} clean")
        for r in google_records:
            status = "OK" if (not r.get("captcha") and not r.get("on_sorry_page")
                              and r.get("external_anchors", 0) >= 3) else "BAD"
            lines.append(f"- `{r['query'][:60]}` -> {status} (externals={r.get('external_anchors')}, "
                         f"captcha={r.get('captcha')}, on_sorry={r.get('on_sorry_page')})")
        lines.append("")
    if linkedin_records:
        l_ok = sum(1 for r in linkedin_records if r.get("headline_count", 0) > 0)
        lines.append(f"## LinkedIn headed-minimized: {l_ok} / {len(linkedin_records)} returned headlines")
        for r in linkedin_records:
            status = "OK" if r.get("headline_count", 0) > 0 else "STRIPPED"
            lines.append(f"- {r['target']}: {status} ({r.get('headline_count', 0)} headlines, "
                         f"{r.get('bytes', 0):,} bytes)")
        lines.append("")
    if headless_control_record is not None:
        r = headless_control_record
        lines.append("## Headless control")
        lines.append(f"- Same query as Google #1, headless: "
                     f"captcha={r.get('captcha')}, on_sorry={r.get('on_sorry_page')}, "
                     f"externals={r.get('external_anchors')}")
        lines.append("")
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {RESULTS} and {SUMMARY}")


if __name__ == "__main__":
    main()
