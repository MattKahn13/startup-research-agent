import json
from pathlib import Path
from schema import StartupRecord, CornellianAffiliation
from startup_researcher import StartupDB


def _aff(name="A", school="CU"):
    return CornellianAffiliation(
        name=name, school=school, role="alumnus", grad_year=2010,
        role_at_company="founder", evidence_span=name, source_url="https://x",
    )


def _rec(name="Acme", **overrides):
    base = dict(company_name=name, cornellians=[_aff()], proof_url="https://x")
    base.update(overrides)
    return StartupRecord(**base)


def test_first_upsert_creates(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec())
    assert len(db.records) == 1


def test_upsert_unions_cornellians(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec(cornellians=[_aff("Alice")]))
    db.upsert(_rec(cornellians=[_aff("Bob")]))
    names = {a["name"] for a in db.records["acme"]["cornellians"]}
    assert names == {"Alice", "Bob"}


def test_upsert_fills_missing_scalar(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec(founded_year=None))
    db.upsert(_rec(founded_year=2015))
    assert db.records["acme"]["founded_year"] == 2015


def test_upsert_logs_conflict(tmp_path):
    db = StartupDB(tmp_path / "db.json", conflict_log=tmp_path / "conflicts.jsonl")
    db.upsert(_rec(funding_total_usd=1_000_000))
    db.upsert(_rec(funding_total_usd=2_000_000))
    log = (tmp_path / "conflicts.jsonl").read_text().strip()
    assert "funding_total_usd" in log
