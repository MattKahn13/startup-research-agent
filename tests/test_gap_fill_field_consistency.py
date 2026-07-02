"""Regression tests for the founders / cornellian_founder field-mismatch bug.

Found during a live overnight-run audit (2026-07-02): fill_missing_data() and
gap_report() read/wrote the legacy "founders" key, while the rest of the
pipeline (validate_record, StartupDB.upsert's Pydantic branch, CSV export)
treats "cornellian_founder" as the REQUIRED, authoritative field. A record
with a garbage cornellian_founder ("health providers", correctly flagged by
validate_record's own validation_issues) was never targeted for repair,
because gap_report only checked whether "founders" was empty. When gap-fill
DID run (because "founders" was separately empty) and found a real name via
Gemini + a live search, that name was written to "founders" -- a field nothing
else reads -- leaving "cornellian_founder" permanently wrong. The record
(Conceive, from bigredai/prnewswire) is the real example that surfaced this.
"""
import json
from pathlib import Path

from startup_researcher import StartupDB, fill_missing_data


def _legacy_record(company_name="Conceive", **overrides):
    """Build a legacy dict-shaped record matching what's on disk today."""
    base = {
        "company_name": company_name,
        "cornellian_founder": "health providers",  # garbage -- self-flagged
        "founders": "",  # empty -- this is what gap_report/fill_missing_data check
        "proof_url": "https://example.com/conceive",
        "affiliation_evidence": "health providers from Cornell and CCRM",
        "affiliation_type": "Researcher",
        "validation_issues": [
            "cornellian_founder doesn't look like a full human name: 'health providers'"
        ],
        "all_sources": ["https://example.com/conceive"],
        "verified": False,
    }
    base.update(overrides)
    return base


def test_gap_report_flags_bad_cornellian_founder_even_when_founders_is_filled(tmp_path):
    """Isolates which field gap_report actually checks. The record's legacy
    'founders' field is DELIBERATELY filled with something plausible-looking,
    so a gap_report that (incorrectly) checks only 'founders' would consider
    this record complete. But cornellian_founder -- the field validate_record
    actually requires -- is garbage. gap_report MUST flag it.
    """
    from startup_researcher import _normalise_name

    db = StartupDB(tmp_path / "db.json")
    rec = _legacy_record(founders="Some Person")  # founders looks filled/fine
    assert rec["cornellian_founder"] == "health providers"  # still garbage
    key = _normalise_name(rec["company_name"])
    db.records = {key: rec}

    gap = db.gap_report()
    assert "Conceive" in gap["missing_founders"], (
        "gap_report checked the wrong field: it must flag a garbage "
        "cornellian_founder as needing repair even when the separate "
        "legacy 'founders' field is non-empty"
    )


def test_fill_missing_data_writes_cornellian_founder_not_just_founders(tmp_path, monkeypatch):
    """When gap-fill successfully finds a real founder name via Gemini, the
    result MUST land in cornellian_founder (the field validate_record,
    upsert, and CSV export all actually read) -- not only in the legacy
    'founders' field that nothing downstream consults.
    """
    from startup_researcher import _normalise_name
    import startup_researcher as sr

    db = StartupDB(tmp_path / "db.json")
    rec = _legacy_record()
    key = _normalise_name(rec["company_name"])
    db.records = {key: rec}

    # Mock the network-dependent calls fill_missing_data relies on.
    monkeypatch.setattr(sr, "google_search", lambda driver, query: ["https://real-source.example.com"])
    monkeypatch.setattr(sr, "scrape_page", lambda driver, url, cache: ("Lauren Berson Sugarman founded Conceive.", "ok"))
    monkeypatch.setattr(sr, "call_gemini", lambda prompt, label="": json.dumps({
        "company_name": "Conceive",
        "founders": "Lauren Berson Sugarman",
        "found_useful_info": True,
    }))

    updated = fill_missing_data(
        driver=None, db=db, visited_urls=set(), page_cache={}, prompt="",
        batch_size=5,
    )

    final = db.records[key]
    assert updated >= 1, "fill_missing_data should report at least one record updated"
    assert final["cornellian_founder"] == "Lauren Berson Sugarman", (
        f"expected the found name in cornellian_founder (the authoritative field), "
        f"got: {final.get('cornellian_founder')!r}. "
        f"(founders field was: {final.get('founders')!r})"
    )
