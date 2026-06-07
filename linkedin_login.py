"""Interactive LinkedIn login: opens a visible Chrome window, navigates to the
LinkedIn login page, waits for you to log in, then saves cookies for reuse.

Subsequent runs of the enrichment scripts can load these cookies and skip login.

Cookie file: ~/.linkedin_cookies.json (mode 0o600, per the cookie-persistence
primitive in the web-agent wiki).

Usage:
    python linkedin_login.py            # default: open browser, wait, save
    python linkedin_login.py --verify   # load saved cookies and confirm they work
    python linkedin_login.py --clear    # delete the saved cookies

Marker for "logged in" is the URL containing `/feed/` OR the presence of a
nav element with data-test-app-aware-link, which is only rendered post-auth.
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

from gemini_tool import init_driver, quit_driver
# NOTE: do NOT use gemini_tool.save_cookies / load_cookies here -- they hardcode
# Google domain filters and silently drop all LinkedIn cookies. We have our own
# LinkedIn-specific versions below.


def save_cookies(driver, cookie_file: str) -> int:
    """Save LinkedIn cookies to disk. Returns number saved."""
    all_cookies = driver.get_cookies()
    relevant = [c for c in all_cookies if "linkedin.com" in (c.get("domain") or "")]
    Path(cookie_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cookie_file).write_text(
        json.dumps(relevant, indent=2, default=str), encoding="utf-8"
    )
    return len(relevant)


def load_cookies(driver, cookie_file: str) -> int:
    """Load LinkedIn cookies into a session already navigated to linkedin.com.
    Returns number successfully added."""
    cookies = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
    added = 0
    for c in cookies:
        # Selenium's add_cookie is strict about which keys it accepts
        spec = {k: c[k] for k in ("name", "value", "domain", "path",
                                  "secure", "httpOnly", "sameSite") if k in c}
        if "expiry" in c and c["expiry"] is not None:
            try:
                spec["expiry"] = int(c["expiry"])
            except (TypeError, ValueError):
                pass
        try:
            driver.add_cookie(spec)
            added += 1
        except Exception:
            # Some cookies (different domain root, host-only) may be rejected
            continue
    return added


COOKIE_FILE = str(Path.home() / ".linkedin_cookies.json")
LOGIN_URL = "https://www.linkedin.com/login"
HOME_URL = "https://www.linkedin.com/feed/"
LOGIN_TIMEOUT_S = 600   # 10 minutes for the user to finish logging in
POLL_INTERVAL_S = 2


def _is_logged_in(driver) -> bool:
    """Return True if we appear to be on a LinkedIn page that requires auth."""
    url = driver.current_url or ""
    if "/login" in url or "/checkpoint" in url or "/authwall" in url:
        return False
    if "/feed" in url or "/in/" in url or "/jobs" in url or "/mynetwork" in url:
        return True
    # Fallback: check DOM for a post-auth nav indicator
    try:
        # Real navbar avatar only renders when logged in.
        out = driver.execute_script(
            "return !!document.querySelector('a[href*=\"/in/\"][data-test-app-aware-link]') "
            "|| !!document.querySelector('img.global-nav__me-photo') "
            "|| !!document.querySelector('[data-control-name=\"identity_welcome_message\"]');"
        )
        return bool(out)
    except Exception:
        return False


def login_interactive() -> int:
    print("Opening a visible Chrome window for LinkedIn login...")
    driver = init_driver(headless=False)
    try:
        driver.get(LOGIN_URL)
        print()
        print("=" * 64)
        print("  Log in to LinkedIn in the browser window that just opened.")
        print("  The script will detect login automatically -- no need to press Enter.")
        print(f"  Timeout: {LOGIN_TIMEOUT_S // 60} minutes.")
        print(f"  Cookies will be saved to: {COOKIE_FILE}")
        print("=" * 64)
        print()
        deadline = time.time() + LOGIN_TIMEOUT_S
        announced_progress = set()
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_S)
            if _is_logged_in(driver):
                print(f"\nLogin detected. URL: {driver.current_url}")
                break
            # Light progress nudge every ~30s
            mins = int((time.time() - (deadline - LOGIN_TIMEOUT_S)) / 30)
            if mins not in announced_progress and mins > 0:
                announced_progress.add(mins)
                remaining = int(deadline - time.time())
                print(f"  ... still waiting (current URL: {driver.current_url[:80]}) "
                      f"-- {remaining}s remaining")
        else:
            print("Timed out waiting for login. Try again.")
            return 1

        # Confirm by navigating to /feed/
        driver.get(HOME_URL)
        time.sleep(3)
        if not _is_logged_in(driver):
            print("Reached /feed/ but the page does not look authenticated.")
            print(f"  current URL: {driver.current_url}")
            return 2

        n = save_cookies(driver, COOKIE_FILE)
        try:
            os.chmod(COOKIE_FILE, 0o600)
        except Exception:
            pass
        print(f"\nSaved {n} LinkedIn cookies to {COOKIE_FILE}")
        if n == 0:
            print("WARNING: zero cookies saved. Try logging in again.")
            return 3
        print("You can now run linkedin_login.py --verify to confirm the cookies work.")
        return 0
    finally:
        try:
            quit_driver(driver)
        except Exception:
            pass


def verify() -> int:
    if not Path(COOKIE_FILE).exists():
        print(f"No cookie file at {COOKIE_FILE}. Run without --verify first.")
        return 1
    print(f"Loading cookies from {COOKIE_FILE} and verifying...")
    driver = init_driver(headless=True)
    try:
        # LinkedIn requires being on the domain before cookies can be added.
        driver.get("https://www.linkedin.com/")
        time.sleep(2)
        added = load_cookies(driver, COOKIE_FILE)
        print(f"  loaded {added} cookies")
        driver.get(HOME_URL)
        time.sleep(4)
        url = driver.current_url
        if _is_logged_in(driver):
            print(f"  OK -- cookies valid. Final URL: {url}")
            return 0
        else:
            print(f"  FAIL -- cookies appear stale or rejected. Final URL: {url}")
            print("  Re-run linkedin_login.py without --verify to refresh.")
            return 2
    finally:
        try:
            quit_driver(driver)
        except Exception:
            pass


def clear() -> int:
    p = Path(COOKIE_FILE)
    if p.exists():
        p.unlink()
        print(f"Deleted {COOKIE_FILE}")
    else:
        print(f"No cookie file at {COOKIE_FILE} (nothing to delete).")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="load saved cookies and test")
    ap.add_argument("--clear", action="store_true", help="delete the saved cookie file")
    args = ap.parse_args()
    if args.clear:
        return clear()
    if args.verify:
        return verify()
    return login_interactive()


if __name__ == "__main__":
    sys.exit(main())
