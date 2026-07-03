"""Regression test for an unhandled Selenium/chromedriver crash inside
`fill_missing_data` that killed the overnight run (2026-07-02 ~22:33 UTC,
PID 26408).

Real traceback: the search browser's chromedriver process died mid-run
(`urllib3.exceptions.MaxRetryError` / `ConnectionRefusedError`, "target
machine actively refused it") while `fill_missing_data`'s gap-fill loop
called `google_search(driver, query)` (startup_researcher.py line ~3153).
That call sits OUTSIDE the try/except that already wraps the Gemini
extraction step a few lines below it (which already survives failures via
`except Exception as e: UI.warn(...)`), so the exception propagated all the
way up through `run()` to the top-level `<module>` and killed the whole
detached process -- even though three full rounds (273 -> 495 records) had
already completed cleanly beforehand, so nothing was lost, but the run
still had to be manually restarted.

This is a genuinely different failure category from tonight's earlier three
bugs (all pure JSON/field-shape logic bugs): this one is an external
infrastructure failure (the browser process itself died), and the fix
follows this file's own existing pattern a few lines below the crash site
-- treat a Selenium failure as "no results for this query" and move on,
rather than letting it escape uncaught. Matches the project's own locked
"Degradation, not stop" principle.
"""
from startup_researcher import StartupDB, fill_missing_data
import startup_researcher as sr


def _legacy_record(company_name="Sourcegraph", **overrides):
    base = {
        "company_name": company_name,
        "cornellian_founder": "",
        "founders": "",
        "proof_url": "",
        "affiliation_evidence": "some prior evidence text long enough",
        "affiliation_type": "Researcher",
        "validation_issues": ["cornellian_founder is empty"],
        "all_sources": [],
        "verified": False,
    }
    base.update(overrides)
    return base


def test_dead_driver_during_google_search_does_not_crash_gap_fill(tmp_path, monkeypatch):
    """The exact real-world crash: google_search raises when the browser
    process has died. fill_missing_data must survive it (log and move on),
    not propagate the exception up to the caller."""
    from startup_researcher import _normalise_name

    db = StartupDB(tmp_path / "db.json")
    rec = _legacy_record()
    key = _normalise_name(rec["company_name"])
    db.records = {key: rec}

    def _dead_driver_google_search(driver, query):
        raise Exception(
            "HTTPConnectionPool(host='localhost', port=61348): Max retries "
            "exceeded (Caused by NewConnectionError(...target machine "
            "actively refused it...))"
        )

    monkeypatch.setattr(sr, "google_search", _dead_driver_google_search)

    # Must not raise.
    updated = fill_missing_data(
        driver=None, db=db, visited_urls=set(), page_cache={}, prompt="",
        batch_size=5,
    )
    assert updated == 0


def test_dead_driver_during_scrape_page_does_not_crash_gap_fill(tmp_path, monkeypatch):
    """Same crash category, one call later in the loop: scrape_page raising
    after google_search succeeds must also be survived."""
    from startup_researcher import _normalise_name

    db = StartupDB(tmp_path / "db.json")
    rec = _legacy_record(company_name="OtherCo")
    key = _normalise_name(rec["company_name"])
    db.records = {key: rec}

    monkeypatch.setattr(sr, "google_search", lambda driver, query: ["https://example.com/otherco"])

    def _dead_driver_scrape_page(driver, url, cache):
        raise Exception("chromedriver connection refused")

    monkeypatch.setattr(sr, "scrape_page", _dead_driver_scrape_page)

    updated = fill_missing_data(
        driver=None, db=db, visited_urls=set(), page_cache={}, prompt="",
        batch_size=5,
    )
    assert updated == 0
