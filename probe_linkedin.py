"""Empirical probe: what does LinkedIn return to a logged-out client?

Tests three modes against a small set of URLs:
  HTTP   -- urllib.request with realistic Chrome UA
  HEADLESS -- undetected-chromedriver in headless mode
  HEADED   -- undetected-chromedriver visible (briefly)

For each (url, mode) it records:
  status, final_url (post-redirect), bytes, has_auth_wall, has_captcha,
  fields_visible (founded/employees/description/headquarters)

Writes probe_linkedin_results.jsonl + a markdown summary.
"""
from __future__ import annotations
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


TARGETS = [
    # Well-known big company (control)
    ("citigroup",     "https://www.linkedin.com/company/citigroup"),
    # Medium-known startups we already track
    ("hyro",          "https://www.linkedin.com/company/hyro-ai"),
    ("rosie",         "https://www.linkedin.com/company/rosie"),
    ("nanit",         "https://www.linkedin.com/company/nanit"),
    # Cornell as an institution
    ("cornell-edu",   "https://www.linkedin.com/school/cornell-university/"),
    # Personal /in/ profile (a known public-figure Cornellian: David Duffield is too rare;
    # use a current obvious one)
    ("reid-hoffman",  "https://www.linkedin.com/in/reidhoffman/"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

RESULTS = Path("probe_linkedin_results.jsonl")
SUMMARY = Path("probe_linkedin_summary.md")


# Signals to look for in the rendered HTML
_AUTH_WALL = re.compile(
    r"(sign in to view|sign in for the full|join now to see|"
    r"we want to make sure it's you|join linkedin)",
    re.I)
_CAPTCHA = re.compile(r"(captcha|are you a robot|verify (?:you are|that you))", re.I)
_FOUNDED = re.compile(r"\bFounded\b\s*[:\-]?\s*(\d{4})", re.I)
_EMPLOYEES = re.compile(r"(\d[\d,]*(?:\+| ?(?:employees|people on linkedin)))", re.I)
_HQ = re.compile(r"\b(?:Headquarter(?:s|ed)|Based in)\s*[:\-]?\s*([^\n<]{4,120})", re.I)
_DESC_META = re.compile(r'<meta[^>]+(?:name|property)="(?:og:description|description)"[^>]+content="([^"]+)"', re.I)
_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)


def analyze(html: str, final_url: str) -> dict:
    """Pull useful signals from a LinkedIn page response."""
    if not html:
        return {"empty": True}
    out = {
        "auth_wall": bool(_AUTH_WALL.search(html)),
        "captcha": bool(_CAPTCHA.search(html)),
        "final_url_login_redirect": "/login" in (final_url or "") or "authwall" in (final_url or ""),
    }
    m = _TITLE.search(html)
    if m:
        out["page_title"] = m.group(1).strip()[:120]
    m = _DESC_META.search(html)
    if m:
        out["og_description"] = m.group(1)[:200]
    m = _FOUNDED.search(html)
    if m:
        out["founded_year"] = m.group(1)
    m = _EMPLOYEES.search(html)
    if m:
        out["employees_signal"] = m.group(1)
    m = _HQ.search(html)
    if m:
        out["hq_signal"] = m.group(1).strip()[:120]
    return out


# ---- Mode A: plain HTTP -----------------------------------------------------

def probe_http(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
            status = r.status
            data = r.read()
    except urllib.error.HTTPError as e:
        return {"mode": "http", "url": url, "status": e.code,
                "error": f"HTTPError {e.code}", "latency_s": round(time.time()-t0, 1)}
    except Exception as e:
        return {"mode": "http", "url": url, "status": None,
                "error": f"{type(e).__name__}: {e}", "latency_s": round(time.time()-t0, 1)}
    html = data.decode("utf-8", errors="replace")
    sig = analyze(html, final)
    return {
        "mode": "http", "url": url, "status": status, "final_url": final,
        "bytes": len(data), "latency_s": round(time.time()-t0, 1),
        **sig,
    }


# ---- Mode B/C: Selenium -----------------------------------------------------

def probe_selenium(url: str, headless: bool) -> dict:
    from gemini_tool import init_driver, quit_driver
    t0 = time.time()
    driver = None
    try:
        driver = init_driver(headless=headless)
        driver.get(url)
        time.sleep(4)
        final = driver.current_url
        html = driver.page_source
    except Exception as e:
        return {"mode": "headless" if headless else "headed", "url": url,
                "status": None, "error": f"{type(e).__name__}: {e}",
                "latency_s": round(time.time()-t0, 1)}
    finally:
        if driver:
            try: quit_driver(driver)
            except Exception: pass
    sig = analyze(html, final)
    return {
        "mode": "headless" if headless else "headed",
        "url": url, "status": 200, "final_url": final,
        "bytes": len(html), "latency_s": round(time.time()-t0, 1),
        **sig,
    }


# ---- Main -------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", nargs="*", default=["http", "headless"],
                    help="which modes to run (http, headless, headed)")
    ap.add_argument("--targets", nargs="*", default=None,
                    help="optional subset of target names; default all")
    args = ap.parse_args()

    targets = TARGETS if not args.targets else [
        (n, u) for n, u in TARGETS if n in args.targets
    ]

    results = []
    for name, url in targets:
        print(f"\n=== {name}  {url} ===")
        for mode in args.modes:
            print(f"  mode={mode} ...")
            if mode == "http":
                r = probe_http(url)
            elif mode == "headless":
                r = probe_selenium(url, headless=True)
            elif mode == "headed":
                r = probe_selenium(url, headless=False)
            else:
                continue
            r["target_name"] = name
            results.append(r)
            with RESULTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            verdict = []
            if r.get("auth_wall"): verdict.append("AUTH_WALL")
            if r.get("captcha"): verdict.append("CAPTCHA")
            if r.get("final_url_login_redirect"): verdict.append("LOGIN_REDIRECT")
            if r.get("founded_year"): verdict.append(f"founded={r['founded_year']}")
            if r.get("employees_signal"): verdict.append(f"emp={r['employees_signal']}")
            if r.get("og_description"): verdict.append(f"og_desc:{len(r['og_description'])}c")
            if r.get("error"): verdict.append(f"ERROR:{r['error']}")
            print(f"    -> status={r.get('status')} bytes={r.get('bytes',0)} {' | '.join(verdict)}")

    # Markdown summary
    lines = ["# LinkedIn scrape probe -- results", "", f"Tested {len(targets)} URLs across {len(args.modes)} modes.", ""]
    lines.append("| Target | Mode | Status | Bytes | Auth wall | Login redirect | Captcha | Useful fields |")
    lines.append("|---|---|---:|---:|---|---|---|---|")
    for r in results:
        fields = []
        if r.get("founded_year"): fields.append(f"founded={r['founded_year']}")
        if r.get("employees_signal"): fields.append(f"emp={r['employees_signal']}")
        if r.get("hq_signal"): fields.append("hq")
        if r.get("og_description"): fields.append("og_desc")
        lines.append(f"| {r['target_name']} | {r['mode']} | "
                     f"{r.get('status', '?')} | {r.get('bytes', 0):,} | "
                     f"{'Y' if r.get('auth_wall') else 'N'} | "
                     f"{'Y' if r.get('final_url_login_redirect') else 'N'} | "
                     f"{'Y' if r.get('captcha') else 'N'} | "
                     f"{', '.join(fields) if fields else '-'} |")
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {RESULTS} and {SUMMARY}")


if __name__ == "__main__":
    main()
