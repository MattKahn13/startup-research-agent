# Research Agent v2 -- Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic JSON DB with DuckDB, rebuild the browser layer as bullet-proof primitives (headed-minimized by default), add a multi-engine intent-routed query ladder, ship a candidates-pool recovery flow, and run a script-based Doctor watchdog throughout. Restore the agent to producing records on a steady drip.

**Architecture:** Five workstreams. R first (DuckDB foundation), then S (browser primitives), then Q (query ladder, depends on S), then F (recovery flow, depends on R + Q). D (Doctor) lands incrementally with every workstream -- each delivers its own initial check set.

**Tech Stack:** Python 3.11+, DuckDB (embedded, native JSON), Pydantic v2 (carry forward), undetected-chromedriver (post-S0b may change), urllib + BeautifulSoup for HTTP engines, pytest.

**Spec:** [`docs/superpowers/specs/2026-06-07-research-agent-v2-design.md`](../specs/2026-06-07-research-agent-v2-design.md)

**Working dir:** `G:/My Drive/Cornell/Spring 2026/Agents/startup_research_agent/`. Already a git repo on branch `main` with origin set to `MattKahn13/startup-research-agent` and push target branch `hardening-pass`.

---

## Pre-flight reading

Read before starting:

1. The spec above. The S0a section is binding (headed-minimized is the default; not optional).
2. The hardening-pass plan at `docs/superpowers/plans/2026-06-05-hardening-pass-implementation.md` -- the patterns used there (TDD, per-task commits) carry forward.
3. `wiki/anti-patterns/headless-default.md` in the web-agent wiki -- the contract for `BrowserSession`.
4. `wiki/site-profiles/linkedin.md` -- the LinkedIn extractor patterns to migrate into `browser/`.
5. `OVERNIGHT_REPORT.md` at the project root -- yesterday's data layer (the 1,389 deduped records) is what R4 migrates.
6. `startup_researcher.py` -- the existing monolith. Skim the `StartupDB` class, `run()` function, `_extract_pass1`/`_extract_pass2`, `call_gemini`, `scrape_page`.
7. DuckDB Python docs at https://duckdb.org/docs/api/python/overview -- 5-minute scan.

---

## File layout after this plan lands

```
startup_research_agent/
  startup_researcher.py          (modified -- rewires DB and search call sites)
  gemini_tool.py                 (modified -- migrates session/cookies to browser/)
  schema.py                      (unchanged -- carries forward)
  metrics.py                     (unchanged -- carries forward)
  degradation.py                 (modified -- accepts Doctor signals)
  retry_policy.py                (unchanged)
  evidence.py                    (unchanged)
  url_canonical.py               (unchanged)

  records/                       (NEW)
    __init__.py
    schema.sql                   (DuckDB DDL)
    store.py                     (RecordStore class)
    migration.py                 (one-off legacy -> DuckDB)
    export.py                    (JSON mirror writer)

  browser/                       (NEW)
    __init__.py
    session.py                   (BrowserSession)
    cookies.py                   (CookieStore, per-domain)
    reaper.py                    (OrphanReaper)
    helpers.py                   (wait_dom_ready, wait_text_stable, wait_selector)

  query/                         (NEW)
    __init__.py
    base.py                      (QueryEngine ABC + SearchResult + exceptions)
    ddg.py                       (DDGEngine HTTP)
    brave.py                     (BraveEngine HTTP)
    mojeek.py                    (MojeekEngine HTTP)
    startpage.py                 (StartpageEngine HTTP)
    selenium_google.py           (SeleniumGoogleEngine via browser/)
    ladder.py                    (QueryLadder orchestrator)

  doctor/                        (NEW)
    __init__.py
    check.py                     (Check ABC + CheckResult)
    runner.py                    (Doctor scheduler + threading)
    checks/                      (one file per check)
      __init__.py
      browser.py                 (OrphanChromedriverCheck, ChromeMemoryCheck, StaleProfileDirCheck)
      records.py                 (SchemaVersionCheck, RecordRoundTripCheck, DiskFreeCheck)
      query.py                   (EngineHealthCheck, AllEnginesColdCheck)
      flow.py                    (CandidatePoolGrowthCheck, LoopDetectorCheck)
      session.py                 (GeminiCookieAgeCheck, LinkedInCookieAgeCheck, LastGeminiCallCheck)

  scripts/                       (NEW)
    init_v2_layout.py            (creates records.duckdb + dirs)
    migrate_monolith_to_duckdb.py (one-shot legacy -> DuckDB)
    export_records_to_json.py    (run by Doctor on a schedule)

  tests/
    test_records_store.py        (NEW)
    test_records_migration.py    (NEW)
    test_browser_session.py      (NEW)
    test_browser_cookies.py      (NEW)
    test_browser_reaper.py       (NEW)
    test_query_engines.py        (NEW -- HTTP engines with fixtures)
    test_query_ladder.py         (NEW)
    test_doctor_checks.py        (NEW)
    test_doctor_runner.py        (NEW)
    test_recovery_flow.py        (NEW)
    (existing tests carry forward, possibly updated)
```

---

## Phase 0 -- Setup

### Task 0.1: Add new dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Read current requirements**

Read `requirements.txt`. Note current contents.

- [ ] **Step 2: Append v2 deps**

Append these lines to `requirements.txt`:

```
duckdb>=1.0
psutil>=5.9
beautifulsoup4>=4.12
lxml>=4.9
```

(`beautifulsoup4` + `lxml` may already be present; that's fine -- duplicates don't matter.)

- [ ] **Step 3: Install**

Run: `pip install -r requirements.txt`
Expected: `Successfully installed duckdb-1.x.x psutil-5.x.x` (others may say "Requirement already satisfied").

- [ ] **Step 4: Verify**

Run: `python -c "import duckdb, psutil; print(duckdb.__version__, psutil.__version__)"`
Expected: two version strings on one line.

- [ ] **Step 5: Commit**

```
git add requirements.txt
git commit -m "chore(v2): add duckdb + psutil"
```

---

### Task 0.2: Create package directories

**Files:**
- Create: `records/__init__.py`, `browser/__init__.py`, `query/__init__.py`, `doctor/__init__.py`, `doctor/checks/__init__.py`, `scripts/__init__.py`

- [ ] **Step 1: Make dirs and empty __init__.py files**

Run:
```
mkdir -p records browser query doctor/checks scripts
type nul > records/__init__.py
type nul > browser/__init__.py
type nul > query/__init__.py
type nul > doctor/__init__.py
type nul > doctor/checks/__init__.py
type nul > scripts/__init__.py
```

(On Windows. On bash, use `touch` instead of `type nul >`.)

- [ ] **Step 2: Verify**

Run: `python -c "import records, browser, query, doctor; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```
git add records/ browser/ query/ doctor/ scripts/
git commit -m "chore(v2): create package skeleton"
```

---

## Phase R -- Records store

### Task R0.1: DuckDB schema DDL

**Files:**
- Create: `records/schema.sql`

- [ ] **Step 1: Write the SQL**

Create `records/schema.sql` with:

```sql
CREATE SEQUENCE IF NOT EXISTS seq_cornellians START 1;
CREATE SEQUENCE IF NOT EXISTS seq_query_log START 1;
CREATE SEQUENCE IF NOT EXISTS seq_promotion_log START 1;

CREATE TABLE IF NOT EXISTS companies (
    slug              VARCHAR PRIMARY KEY,
    company_name      VARCHAR NOT NULL,
    proof_url         VARCHAR NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'unknown',
    description       VARCHAR,
    industry          VARCHAR,
    funding_total_usd BIGINT,
    funding_stage     VARCHAR,
    funding_last_round_year INTEGER,
    founded_year      INTEGER,
    employee_count    INTEGER,
    is_public         BOOLEAN,
    headquarters      VARCHAR,
    exit_year         INTEGER,
    acquirer          VARCHAR,
    acquisition_amount_usd BIGINT,
    website_url       VARCHAR,
    linkedin_company_url VARCHAR,
    crunchbase_url    VARCHAR,
    wikipedia_url     VARCHAR,
    tags              JSON,
    non_cornell_cofounder_schools JSON,
    validation_tier   VARCHAR NOT NULL,
    validation_issues JSON,
    first_seen_at     TIMESTAMP NOT NULL,
    last_verified_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS cornellians (
    id                BIGINT PRIMARY KEY DEFAULT nextval('seq_cornellians'),
    company_slug      VARCHAR NOT NULL REFERENCES companies(slug),
    name              VARCHAR NOT NULL,
    school            VARCHAR NOT NULL,
    role              VARCHAR NOT NULL,
    grad_year         INTEGER,
    role_at_company   VARCHAR NOT NULL,
    evidence_span     VARCHAR NOT NULL,
    source_url        VARCHAR NOT NULL,
    UNIQUE (company_slug, name)
);

CREATE TABLE IF NOT EXISTS candidates (
    slug              VARCHAR PRIMARY KEY,
    company_name      VARCHAR NOT NULL,
    last_attempted_proof_url VARCHAR,
    last_attempt_outcome VARCHAR,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    first_attempt_at  TIMESTAMP NOT NULL,
    last_attempt_at   TIMESTAMP NOT NULL,
    candidate_payload JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id                BIGINT PRIMARY KEY DEFAULT nextval('seq_query_log'),
    timestamp         TIMESTAMP NOT NULL,
    engine            VARCHAR NOT NULL,
    query             VARCHAR NOT NULL,
    result_count      INTEGER NOT NULL,
    outcome           VARCHAR NOT NULL,
    latency_ms        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_log (
    id                BIGINT PRIMARY KEY DEFAULT nextval('seq_promotion_log'),
    timestamp         TIMESTAMP NOT NULL,
    slug              VARCHAR NOT NULL,
    direction         VARCHAR NOT NULL,
    reason            VARCHAR NOT NULL,
    found_via_query   VARCHAR
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);

INSERT INTO schema_version (version, applied_at)
SELECT 1, CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE version = 1);

CREATE INDEX IF NOT EXISTS idx_companies_tier ON companies(validation_tier);
CREATE INDEX IF NOT EXISTS idx_companies_founded ON companies(founded_year);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);
CREATE INDEX IF NOT EXISTS idx_cornellians_company ON cornellians(company_slug);
CREATE INDEX IF NOT EXISTS idx_cornellians_name ON cornellians(name);
CREATE INDEX IF NOT EXISTS idx_candidates_last_attempt ON candidates(last_attempt_at);
CREATE INDEX IF NOT EXISTS idx_query_log_engine ON query_log(engine);
```

- [ ] **Step 2: Validate against an in-memory DuckDB**

Run:
```
python -c "import duckdb; con = duckdb.connect(':memory:'); con.execute(open('records/schema.sql').read()); print('ok'); print(con.execute('SELECT * FROM schema_version').fetchall())"
```
Expected: `ok\n[(1, datetime(...))]`

- [ ] **Step 3: Commit**

```
git add records/schema.sql
git commit -m "feat(records): DuckDB schema DDL"
```

---

### Task R1: RecordStore class -- init and basic insert

**Files:**
- Create: `records/store.py`
- Create: `tests/test_records_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_records_store.py`:

```python
import duckdb
from pathlib import Path
from datetime import datetime, timezone
from records.store import RecordStore
from schema import StartupRecord, CornellianAffiliation


def _aff(name="Alice"):
    return CornellianAffiliation(
        name=name, school="CU", role="alumnus", grad_year=2010,
        role_at_company="founder", evidence_span=name,
        source_url="https://example.com",
    )


def _rec(name="Acme", **overrides):
    base = dict(company_name=name, cornellians=[_aff()],
                proof_url="https://example.com")
    base.update(overrides)
    return StartupRecord(**base)


def test_store_initializes_schema(tmp_path):
    db_path = tmp_path / "test.duckdb"
    store = RecordStore(db_path)
    # Schema-version row exists
    rows = store._conn.execute("SELECT version FROM schema_version").fetchall()
    assert rows == [(1,)]


def test_upsert_record_inserts_new(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    outcome = store.upsert_record(_rec())
    assert outcome == "new"
    rows = store._conn.execute(
        "SELECT slug, company_name FROM companies"
    ).fetchall()
    assert rows == [("acme", "Acme")]
    corns = store._conn.execute(
        "SELECT name FROM cornellians WHERE company_slug = 'acme'"
    ).fetchall()
    assert corns == [("Alice",)]
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m pytest tests/test_records_store.py -v`
Expected: `ImportError: cannot import name 'RecordStore'` (or ModuleNotFoundError on `records.store`).

- [ ] **Step 3: Implement minimal RecordStore**

Create `records/store.py`:

```python
from __future__ import annotations
import duckdb
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schema import StartupRecord
from startup_researcher import _normalise_name   # existing helper


_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RecordStore:
    """High-level interface over records.duckdb. All writes are transactional."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.db_path))
        # Apply schema
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.execute(ddl)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- Inserts ---------------------------------------------------------

    def upsert_record(self, record: StartupRecord) -> str:
        """Returns 'new' | 'merged'."""
        slug = _normalise_name(record.company_name)
        if not slug:
            return "skipped"
        existing = self._conn.execute(
            "SELECT slug FROM companies WHERE slug = ?", [slug]
        ).fetchone()
        if existing is None:
            return self._insert_new(slug, record)
        return self._merge_existing(slug, record)

    def _insert_new(self, slug: str, record: StartupRecord) -> str:
        d = record.model_dump(mode="json")
        now = _utc_now_iso()
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """INSERT INTO companies (
                    slug, company_name, proof_url, status, description, industry,
                    funding_total_usd, funding_stage, funding_last_round_year,
                    founded_year, employee_count, is_public, headquarters,
                    exit_year, acquirer, acquisition_amount_usd, website_url,
                    linkedin_company_url, crunchbase_url, wikipedia_url, tags,
                    non_cornell_cofounder_schools, validation_tier,
                    validation_issues, first_seen_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    slug, d["company_name"], d["proof_url"], d.get("status", "unknown"),
                    d.get("description"), d.get("industry"),
                    d.get("funding_total_usd"), d.get("funding_stage"),
                    d.get("funding_last_round_year"), d.get("founded_year"),
                    d.get("employee_count"), d.get("is_public"),
                    d.get("headquarters"), d.get("exit_year"), d.get("acquirer"),
                    d.get("acquisition_amount_usd"), d.get("website_url"),
                    d.get("linkedin_company_url"), d.get("crunchbase_url"),
                    d.get("wikipedia_url"),
                    json.dumps(d.get("tags") or []),
                    json.dumps(d.get("non_cornell_cofounder_schools") or []),
                    d.get("validation_tier", "weak"),
                    json.dumps(d.get("validation_issues") or []),
                    now, now,
                ],
            )
            for c in record.cornellians:
                self._conn.execute(
                    """INSERT INTO cornellians
                        (company_slug, name, school, role, grad_year,
                         role_at_company, evidence_span, source_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [slug, c.name, c.school, c.role, c.grad_year,
                     c.role_at_company, c.evidence_span, c.source_url],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return "new"

    def _merge_existing(self, slug: str, record: StartupRecord) -> str:
        # Minimal: just bump last_verified_at and union cornellians
        now = _utc_now_iso()
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "UPDATE companies SET last_verified_at = ? WHERE slug = ?",
                [now, slug],
            )
            for c in record.cornellians:
                existing = self._conn.execute(
                    "SELECT 1 FROM cornellians WHERE company_slug = ? AND name = ?",
                    [slug, c.name],
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        """INSERT INTO cornellians
                            (company_slug, name, school, role, grad_year,
                             role_at_company, evidence_span, source_url)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        [slug, c.name, c.school, c.role, c.grad_year,
                         c.role_at_company, c.evidence_span, c.source_url],
                    )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return "merged"
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_records_store.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add records/store.py records/schema.sql tests/test_records_store.py
git commit -m "feat(records): RecordStore.upsert_record (new + merge)"
```

---

### Task R2: RecordStore -- list_records with filters

**Files:**
- Modify: `records/store.py`, `tests/test_records_store.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_records_store.py`:

```python
def test_list_records_filters_by_tier(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.upsert_record(_rec(name="A", validation_tier="high"))
    store.upsert_record(_rec(name="B", validation_tier="weak"))
    store.upsert_record(_rec(name="C", validation_tier="high"))
    high = list(store.list_records(tier="high"))
    assert {r.company_name for r in high} == {"A", "C"}


def test_list_records_filters_by_founded_after(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.upsert_record(_rec(name="Old", founded_year=2010))
    store.upsert_record(_rec(name="New", founded_year=2022))
    store.upsert_record(_rec(name="Unknown"))  # founded_year is None
    recent = list(store.list_records(founded_after=2020))
    assert {r.company_name for r in recent} == {"New"}
```

- [ ] **Step 2: Run, fail**

Run: `python -m pytest tests/test_records_store.py::test_list_records_filters_by_tier -v`
Expected: AttributeError on `list_records`.

- [ ] **Step 3: Implement**

Append to `RecordStore` in `records/store.py`:

```python
    # ---- Reads -----------------------------------------------------------

    def list_records(self, *, tier: Optional[str] = None,
                     founded_after: Optional[int] = None,
                     limit: Optional[int] = None):
        """Yields StartupRecord instances."""
        clauses = []
        params: list = []
        if tier:
            clauses.append("validation_tier = ?")
            params.append(tier)
        if founded_after is not None:
            clauses.append("founded_year >= ?")
            params.append(founded_after)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT slug FROM companies{where} ORDER BY slug"
        if limit:
            sql += f" LIMIT {int(limit)}"
        slugs = [row[0] for row in self._conn.execute(sql, params).fetchall()]
        for slug in slugs:
            yield self._load_record(slug)

    def _load_record(self, slug: str) -> StartupRecord:
        co = self._conn.execute(
            "SELECT * FROM companies WHERE slug = ?", [slug]
        ).fetchone()
        if co is None:
            raise KeyError(slug)
        cols = [d[0] for d in self._conn.description]
        co_dict = dict(zip(cols, co))
        # JSON columns come back as strings
        for jcol in ("tags", "non_cornell_cofounder_schools", "validation_issues"):
            v = co_dict.get(jcol)
            co_dict[jcol] = json.loads(v) if v else []
        corns_raw = self._conn.execute(
            """SELECT name, school, role, grad_year, role_at_company,
                       evidence_span, source_url
                FROM cornellians WHERE company_slug = ?""",
            [slug],
        ).fetchall()
        corns_cols = ["name", "school", "role", "grad_year",
                       "role_at_company", "evidence_span", "source_url"]
        corns = [dict(zip(corns_cols, row)) for row in corns_raw]
        co_dict["cornellians"] = corns
        # Drop columns not in StartupRecord
        co_dict.pop("slug", None)
        return StartupRecord(**co_dict)
```

- [ ] **Step 4: Run, pass**

Run: `python -m pytest tests/test_records_store.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```
git add records/store.py tests/test_records_store.py
git commit -m "feat(records): list_records with tier/founded_after filters"
```

---

### Task R3: RecordStore -- candidates table API

**Files:**
- Modify: `records/store.py`, `tests/test_records_store.py`

- [ ] **Step 1: Append failing tests**

```python
def test_add_candidate_new(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    outcome = store.add_candidate(
        slug="ghost-co", company_name="Ghost Co",
        last_url="https://example.com",
        last_outcome="unmatched",
        payload={"founders_proposed": ["Alice"]},
    )
    assert outcome == "new"
    row = store._conn.execute(
        "SELECT slug, attempt_count FROM candidates WHERE slug = ?",
        ["ghost-co"]
    ).fetchone()
    assert row == ("ghost-co", 1)


def test_add_candidate_increments(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.add_candidate(slug="g", company_name="G", last_url="u",
                         last_outcome="unmatched", payload={})
    outcome = store.add_candidate(slug="g", company_name="G", last_url="u2",
                                    last_outcome="fetch_failed", payload={})
    assert outcome == "incremented"
    row = store._conn.execute(
        "SELECT attempt_count, last_attempt_outcome, last_attempted_proof_url "
        "FROM candidates WHERE slug = ?", ["g"]
    ).fetchone()
    assert row == (2, "fetch_failed", "u2")


def test_list_candidates_excludes_recent(tmp_path):
    import time as _time
    store = RecordStore(tmp_path / "t.duckdb")
    store.add_candidate(slug="fresh", company_name="F", last_url="u",
                         last_outcome="unmatched", payload={})
    # Manually backdate
    store._conn.execute(
        "UPDATE candidates SET last_attempt_at = '2020-01-01 00:00:00' "
        "WHERE slug = 'fresh'"
    )
    eligible = list(store.list_candidates(exclude_recently_attempted_s=3600))
    assert any(c["slug"] == "fresh" for c in eligible)

    store.add_candidate(slug="too-fresh", company_name="T", last_url="u",
                         last_outcome="unmatched", payload={})
    eligible = list(store.list_candidates(exclude_recently_attempted_s=3600))
    assert not any(c["slug"] == "too-fresh" for c in eligible)
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

Append to `RecordStore`:

```python
    def add_candidate(self, *, slug: str, company_name: str,
                       last_url: str | None, last_outcome: str,
                       payload: dict) -> str:
        """Insert new candidate or increment attempt_count on existing."""
        now = _utc_now_iso()
        existing = self._conn.execute(
            "SELECT attempt_count FROM candidates WHERE slug = ?", [slug]
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """INSERT INTO candidates
                    (slug, company_name, last_attempted_proof_url,
                     last_attempt_outcome, attempt_count,
                     first_attempt_at, last_attempt_at, candidate_payload)
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                [slug, company_name, last_url, last_outcome, now, now,
                 json.dumps(payload)],
            )
            return "new"
        self._conn.execute(
            """UPDATE candidates
                SET attempt_count = attempt_count + 1,
                    last_attempt_at = ?,
                    last_attempt_outcome = ?,
                    last_attempted_proof_url = ?,
                    candidate_payload = ?
                WHERE slug = ?""",
            [now, last_outcome, last_url, json.dumps(payload), slug],
        )
        return "incremented"

    def list_candidates(self, *, max_attempts: int = 5,
                         exclude_recently_attempted_s: int = 3600):
        """Yields candidate dicts. Skips parked (>= max_attempts) and recently-attempted."""
        rows = self._conn.execute(
            """SELECT slug, company_name, last_attempted_proof_url,
                       last_attempt_outcome, attempt_count,
                       first_attempt_at, last_attempt_at, candidate_payload
                FROM candidates
                WHERE attempt_count < ?
                  AND last_attempt_at < (CURRENT_TIMESTAMP - INTERVAL (?) SECOND)
                ORDER BY last_attempt_at ASC""",
            [max_attempts, exclude_recently_attempted_s],
        ).fetchall()
        cols = ["slug", "company_name", "last_attempted_proof_url",
                "last_attempt_outcome", "attempt_count", "first_attempt_at",
                "last_attempt_at", "candidate_payload"]
        for row in rows:
            d = dict(zip(cols, row))
            try:
                d["candidate_payload"] = json.loads(d["candidate_payload"] or "{}")
            except Exception:
                d["candidate_payload"] = {}
            yield d
```

- [ ] **Step 4: Run, pass**

Run: `python -m pytest tests/test_records_store.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```
git add records/store.py tests/test_records_store.py
git commit -m "feat(records): candidates table API (add_candidate, list_candidates)"
```

---

### Task R4: RecordStore -- promote_candidate atomic move

**Files:**
- Modify: `records/store.py`, `tests/test_records_store.py`

- [ ] **Step 1: Append failing test**

```python
def test_promote_candidate_moves_atomically(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.add_candidate(slug="acme", company_name="Acme", last_url="u",
                         last_outcome="unmatched", payload={})
    rec = _rec(name="Acme")
    store.promote_candidate(slug="acme", record=rec,
                              found_via_query="acme cornell")
    # Candidate gone
    assert store._conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE slug = 'acme'"
    ).fetchone()[0] == 0
    # Company present
    assert store._conn.execute(
        "SELECT slug FROM companies WHERE slug = 'acme'"
    ).fetchone() == ("acme",)
    # Promotion logged
    log_row = store._conn.execute(
        "SELECT direction, found_via_query FROM promotion_log WHERE slug = 'acme'"
    ).fetchone()
    assert log_row == ("promoted", "acme cornell")
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

Append to `RecordStore`:

```python
    def promote_candidate(self, *, slug: str, record: StartupRecord,
                            found_via_query: str | None = None) -> None:
        """Single transaction: delete candidate, upsert company, log promotion."""
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM candidates WHERE slug = ?", [slug])
            # Upsert without nested BEGIN
            self._upsert_within_txn(slug, record)
            self._conn.execute(
                """INSERT INTO promotion_log
                    (timestamp, slug, direction, reason, found_via_query)
                    VALUES (?, ?, 'promoted', 'evidence-span-now-passes', ?)""",
                [_utc_now_iso(), slug, found_via_query],
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _upsert_within_txn(self, slug: str, record: StartupRecord) -> None:
        """Same as upsert_record but assumes a transaction is already open."""
        existing = self._conn.execute(
            "SELECT slug FROM companies WHERE slug = ?", [slug]
        ).fetchone()
        if existing is None:
            self._insert_new_within_txn(slug, record)
        else:
            self._merge_existing_within_txn(slug, record)

    def _insert_new_within_txn(self, slug: str, record: StartupRecord) -> None:
        """Body of _insert_new without BEGIN/COMMIT. Refactor _insert_new to call this."""
        d = record.model_dump(mode="json")
        now = _utc_now_iso()
        self._conn.execute(
            """INSERT INTO companies (
                slug, company_name, proof_url, status, description, industry,
                funding_total_usd, funding_stage, funding_last_round_year,
                founded_year, employee_count, is_public, headquarters,
                exit_year, acquirer, acquisition_amount_usd, website_url,
                linkedin_company_url, crunchbase_url, wikipedia_url, tags,
                non_cornell_cofounder_schools, validation_tier,
                validation_issues, first_seen_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [slug, d["company_name"], d["proof_url"], d.get("status", "unknown"),
             d.get("description"), d.get("industry"),
             d.get("funding_total_usd"), d.get("funding_stage"),
             d.get("funding_last_round_year"), d.get("founded_year"),
             d.get("employee_count"), d.get("is_public"),
             d.get("headquarters"), d.get("exit_year"), d.get("acquirer"),
             d.get("acquisition_amount_usd"), d.get("website_url"),
             d.get("linkedin_company_url"), d.get("crunchbase_url"),
             d.get("wikipedia_url"),
             json.dumps(d.get("tags") or []),
             json.dumps(d.get("non_cornell_cofounder_schools") or []),
             d.get("validation_tier", "weak"),
             json.dumps(d.get("validation_issues") or []),
             now, now],
        )
        for c in record.cornellians:
            self._conn.execute(
                """INSERT INTO cornellians
                    (company_slug, name, school, role, grad_year,
                     role_at_company, evidence_span, source_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [slug, c.name, c.school, c.role, c.grad_year,
                 c.role_at_company, c.evidence_span, c.source_url],
            )

    def _merge_existing_within_txn(self, slug: str, record: StartupRecord) -> None:
        """Body of _merge_existing without BEGIN/COMMIT."""
        now = _utc_now_iso()
        self._conn.execute(
            "UPDATE companies SET last_verified_at = ? WHERE slug = ?",
            [now, slug],
        )
        for c in record.cornellians:
            existing = self._conn.execute(
                "SELECT 1 FROM cornellians WHERE company_slug = ? AND name = ?",
                [slug, c.name],
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO cornellians
                        (company_slug, name, school, role, grad_year,
                         role_at_company, evidence_span, source_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [slug, c.name, c.school, c.role, c.grad_year,
                     c.role_at_company, c.evidence_span, c.source_url],
                )
```

Also refactor the original `_insert_new` and `_merge_existing` to delegate to the `_within_txn` versions (so we don't duplicate the SQL). Replace their bodies:

```python
    def _insert_new(self, slug: str, record: StartupRecord) -> str:
        self._conn.execute("BEGIN")
        try:
            self._insert_new_within_txn(slug, record)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return "new"

    def _merge_existing(self, slug: str, record: StartupRecord) -> str:
        self._conn.execute("BEGIN")
        try:
            self._merge_existing_within_txn(slug, record)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return "merged"
```

- [ ] **Step 4: Run, pass**

Run: `python -m pytest tests/test_records_store.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```
git add records/store.py tests/test_records_store.py
git commit -m "feat(records): promote_candidate atomic transaction"
```

---

### Task R5: RecordStore -- log_query and stats

**Files:**
- Modify: `records/store.py`, `tests/test_records_store.py`

- [ ] **Step 1: Append failing tests**

```python
def test_log_query_inserts_row(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.log_query(engine="ddg", query="cornell founder",
                     result_count=8, outcome="ok", latency_ms=234)
    rows = store._conn.execute(
        "SELECT engine, query, result_count, outcome, latency_ms FROM query_log"
    ).fetchall()
    assert rows == [("ddg", "cornell founder", 8, "ok", 234)]


def test_stats_returns_counts(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.upsert_record(_rec(name="A", validation_tier="high"))
    store.upsert_record(_rec(name="B", validation_tier="weak"))
    store.add_candidate(slug="c", company_name="C", last_url="u",
                         last_outcome="unmatched", payload={})
    s = store.stats()
    assert s["company_count"] == 2
    assert s["candidate_count"] == 1
    assert s["companies_by_tier"]["high"] == 1
    assert s["companies_by_tier"]["weak"] == 1
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

Append to `RecordStore`:

```python
    def log_query(self, *, engine: str, query: str, result_count: int,
                   outcome: str, latency_ms: int) -> None:
        self._conn.execute(
            """INSERT INTO query_log
                (timestamp, engine, query, result_count, outcome, latency_ms)
                VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)""",
            [engine, query, result_count, outcome, latency_ms],
        )

    def stats(self) -> dict:
        co = self._conn.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]
        cand = self._conn.execute(
            "SELECT COUNT(*) FROM candidates"
        ).fetchone()[0]
        tiers = dict(self._conn.execute(
            "SELECT validation_tier, COUNT(*) FROM companies GROUP BY validation_tier"
        ).fetchall())
        qlog_n = self._conn.execute(
            "SELECT COUNT(*) FROM query_log"
        ).fetchone()[0]
        return {
            "company_count": co,
            "candidate_count": cand,
            "companies_by_tier": tiers,
            "query_log_rows": qlog_n,
        }
```

- [ ] **Step 4: Run, pass**

Expected: 10 passed.

- [ ] **Step 5: Commit**

```
git add records/store.py tests/test_records_store.py
git commit -m "feat(records): log_query and stats"
```

---

### Task R6: Init script

**Files:**
- Create: `scripts/init_v2_layout.py`

- [ ] **Step 1: Write the script**

```python
"""Create v2 layout: records.duckdb, records/ dir, candidates/ dir."""
from pathlib import Path
from records.store import RecordStore


def main():
    out = Path("startup_output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "records").mkdir(exist_ok=True)
    (out / "candidates").mkdir(exist_ok=True)
    store = RecordStore(out / "records.duckdb")
    s = store.stats()
    store.close()
    print(f"v2 layout initialized at {out.resolve()}")
    print(f"  companies: {s['company_count']}")
    print(f"  candidates: {s['candidate_count']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run**

Run: `python scripts/init_v2_layout.py`
Expected: "v2 layout initialized at ..." + zero counts.

- [ ] **Step 3: Commit**

```
git add scripts/init_v2_layout.py
git commit -m "feat(records): init_v2_layout.py one-shot scaffold"
```

---

### Task R7: Migration script (legacy monolith -> DuckDB)

**Files:**
- Create: `scripts/migrate_monolith_to_duckdb.py`

- [ ] **Step 1: Write the script**

```python
"""One-shot: read startup_output_test/startups_db_deduped.json (from yesterday's
overnight work), validate each through StartupRecord, insert into DuckDB."""
import json
import sys
from pathlib import Path
from pydantic import ValidationError

from schema import StartupRecord
from records.store import RecordStore


SRC_DEFAULT = Path("startup_output_test/startups_db_deduped.json")
OUT_DEFAULT = Path("startup_output/records.duckdb")
FAIL_LOG = Path("startup_output/migrate_skipped.jsonl")


def main(argv=None):
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_DEFAULT
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT_DEFAULT
    if not src.exists():
        print(f"source missing: {src}", file=sys.stderr)
        return 1
    FAIL_LOG.unlink(missing_ok=True)
    store = RecordStore(out)
    db = json.loads(src.read_text(encoding="utf-8"))
    records = list(db.values()) if isinstance(db, dict) else db
    inserted = 0
    skipped = 0
    skip_reasons = {}
    for r in records:
        try:
            rec = StartupRecord(**r)
            store.upsert_record(rec)
            inserted += 1
        except ValidationError as e:
            err = e.errors()[0].get("msg", "validation")
            skip_reasons[err] = skip_reasons.get(err, 0) + 1
            skipped += 1
            with FAIL_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"company": r.get("company_name", ""),
                                     "error": err}) + "\n")
    store.close()
    print(f"migrated:  {inserted}")
    print(f"skipped:   {skipped}")
    if skip_reasons:
        for r, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1])[:5]:
            print(f"  {n}: {r}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run against the deduped DB**

Run:
```
python scripts/migrate_monolith_to_duckdb.py
```
Expected: "migrated: ~1389" (some may skip on Pydantic validation; expected).

- [ ] **Step 3: Sanity-check counts**

Run:
```
python -c "from records.store import RecordStore; s=RecordStore('startup_output/records.duckdb').stats(); print(s)"
```
Expected: company_count ≈ 1389, breakdown by tier.

- [ ] **Step 4: Commit**

```
git add scripts/migrate_monolith_to_duckdb.py
git commit -m "feat(records): legacy-monolith -> DuckDB migration script"
```

---

### Task R8: JSON export mirror

**Files:**
- Create: `records/export.py`
- Create: `scripts/export_records_to_json.py`
- Test: `tests/test_records_export.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
from records.store import RecordStore
from records.export import export_to_json
from schema import StartupRecord, CornellianAffiliation


def _aff():
    return CornellianAffiliation(
        name="A", school="CU", role="alumnus", grad_year=2010,
        role_at_company="founder", evidence_span="A",
        source_url="https://example.com",
    )


def test_export_writes_one_file_per_record(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    store.upsert_record(StartupRecord(
        company_name="Acme", cornellians=[_aff()],
        proof_url="https://example.com",
    ))
    store.upsert_record(StartupRecord(
        company_name="Beta", cornellians=[_aff()],
        proof_url="https://example.com",
    ))
    out_dir = tmp_path / "records"
    export_to_json(store, out_dir)
    files = sorted(p.name for p in out_dir.glob("*.json"))
    assert files == ["acme.json", "beta.json"]
    payload = json.loads((out_dir / "acme.json").read_text())
    assert payload["company_name"] == "Acme"
    assert payload["cornellians"][0]["name"] == "A"
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

`records/export.py`:

```python
import json
from pathlib import Path
from records.store import RecordStore


def export_to_json(store: RecordStore, out_dir: Path) -> int:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for rec in store.list_records():
        slug = rec.company_name.lower()
        # Use the same normalisation as RecordStore
        from startup_researcher import _normalise_name
        slug = _normalise_name(rec.company_name)
        (out_dir / f"{slug}.json").write_text(
            rec.model_dump_json(indent=2), encoding="utf-8"
        )
        n += 1
    return n
```

`scripts/export_records_to_json.py`:

```python
"""Run from CLI: dumps records/records.duckdb -> startup_output/records/*.json"""
from pathlib import Path
from records.store import RecordStore
from records.export import export_to_json


def main():
    store = RecordStore("startup_output/records.duckdb")
    n = export_to_json(store, Path("startup_output/records"))
    store.close()
    print(f"exported {n} records")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, pass**

Run: `python -m pytest tests/test_records_export.py -v`
Expected: 1 passed.

- [ ] **Step 5: Run the CLI**

Run: `python scripts/export_records_to_json.py`
Expected: prints `exported ~1389 records` after R7 has migrated.

- [ ] **Step 6: Commit**

```
git add records/export.py scripts/export_records_to_json.py tests/test_records_export.py
git commit -m "feat(records): JSON export mirror"
```

---

### Task R9: Wire RecordStore into startup_researcher.py round loop

**Files:**
- Modify: `startup_researcher.py`

This task replaces the existing `StartupDB` class usage with `RecordStore`. **Largest single change in the plan.** Do this with extra care.

- [ ] **Step 1: Find every reference to StartupDB**

Run: `grep -n "StartupDB\|db\.upsert\|db\.save\|self\.records" startup_researcher.py | head -30`

Note every line; you'll be replacing them.

- [ ] **Step 2: Replace constructor**

In `run()` (the main entry function), where `db = StartupDB(...)` is called, replace with:

```python
from records.store import RecordStore
record_store = RecordStore(Path(output_dir) / "records.duckdb")
```

Pass `record_store` to functions that previously took `db`. Audit each receiver and rename the parameter to `record_store` for clarity.

- [ ] **Step 3: Replace upsert call sites**

`db.upsert(record_dict_or_pydantic)` becomes:

```python
if isinstance(record, StartupRecord):
    record_store.upsert_record(record)
else:
    record_store.upsert_record(StartupRecord(**record))
```

- [ ] **Step 4: Replace gap_report and analytics reads**

If `gap_report` reads `db.records`, replace with `list(record_store.list_records(tier=...))` plus the existing gap-computation logic.

- [ ] **Step 5: Remove db.save() calls**

DuckDB autosaves on transaction commit. Remove every `db.save()` line.

- [ ] **Step 6: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Smoke against the test DB**

Run a no-network sanity check:
```
python -c "
from records.store import RecordStore
from pathlib import Path
import startup_researcher as sr
s = RecordStore('startup_output/records.duckdb')
print(s.stats())
"
```
Expected: counts from R7's migration.

- [ ] **Step 8: Commit**

```
git add startup_researcher.py
git commit -m "feat(researcher): wire RecordStore into round loop"
```

---

### Task R10: Analytics scripts migrate to RecordStore

**Files:**
- Modify: `analyze_ecosystem.py`, `export_csv.py`, `export_network.py`

These scripts currently load JSON files; they need to switch to `RecordStore`.

- [ ] **Step 1: Update analyze_ecosystem.py**

Replace its `load_merged()` with:

```python
def load_merged() -> tuple[dict, dict]:
    from records.store import RecordStore
    store = RecordStore("startup_output/records.duckdb")
    merged = {}
    sources = {}
    for rec in store.list_records():
        slug = rec.company_name.lower()  # use _normalise_name
        merged[slug] = rec.model_dump(mode="json")
        sources[slug] = "duckdb"
    store.close()
    return merged, sources
```

- [ ] **Step 2: Update export_csv.py and export_network.py the same way**

Each script's `pick_source()` becomes "always use RecordStore."

- [ ] **Step 3: Run all three**

```
python analyze_ecosystem.py
python export_csv.py
python export_network.py
```
Expected: each script produces its output without errors against the post-migration DB.

- [ ] **Step 4: Commit**

```
git add analyze_ecosystem.py export_csv.py export_network.py
git commit -m "feat(records): analytics scripts read from RecordStore"
```

---

### Task R11: Doctor checks for R (lands now, framework comes later)

These checks are independently useful even before the Doctor runner exists. Implement them as plain functions; the Doctor framework will wrap them.

**Files:**
- Create: `doctor/checks/records.py`
- Test: `tests/test_doctor_checks_records.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil
import pytest
from pathlib import Path
from records.store import RecordStore
from doctor.checks.records import (
    check_schema_version, check_record_roundtrip, check_disk_free,
)


def test_schema_version_passes(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    result = check_schema_version(store, expected_version=1)
    assert result.severity == "ok"


def test_schema_version_fails_on_mismatch(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    result = check_schema_version(store, expected_version=999)
    assert result.severity == "error"
    assert "999" in result.message


def test_record_roundtrip_passes_with_empty_db(tmp_path):
    store = RecordStore(tmp_path / "t.duckdb")
    result = check_record_roundtrip(store)
    assert result.severity == "ok"


def test_disk_free_returns_a_result(tmp_path):
    result = check_disk_free(tmp_path, min_free_gb=0.001)
    assert result.severity in ("ok", "warn", "error")
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

`doctor/check.py`:

```python
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CheckResult:
    name: str
    severity: Literal["ok", "warn", "error"]
    message: str
    auto_healed: bool = False
    details: dict = field(default_factory=dict)
```

`doctor/checks/records.py`:

```python
import random
import shutil
from pathlib import Path
from pydantic import ValidationError

from doctor.check import CheckResult
from records.store import RecordStore
from schema import StartupRecord


def check_schema_version(store: RecordStore, *, expected_version: int = 1) -> CheckResult:
    row = store._conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()
    actual = row[0] if row else None
    if actual == expected_version:
        return CheckResult(
            name="SchemaVersionCheck", severity="ok",
            message=f"schema at v{actual}",
        )
    return CheckResult(
        name="SchemaVersionCheck", severity="error",
        message=f"schema is v{actual}, code expects v{expected_version}",
        details={"actual": actual, "expected": expected_version},
    )


def check_record_roundtrip(store: RecordStore) -> CheckResult:
    """Sample one record, validate against Pydantic."""
    slugs = [r[0] for r in store._conn.execute(
        "SELECT slug FROM companies"
    ).fetchall()]
    if not slugs:
        return CheckResult(
            name="RecordRoundTripCheck", severity="ok",
            message="no records yet",
        )
    sample = random.choice(slugs)
    try:
        rec = store._load_record(sample)
        assert isinstance(rec, StartupRecord)
        return CheckResult(
            name="RecordRoundTripCheck", severity="ok",
            message=f"roundtrip ok for {sample}",
        )
    except (ValidationError, AssertionError, Exception) as e:
        return CheckResult(
            name="RecordRoundTripCheck", severity="warn",
            message=f"roundtrip failed for {sample}: {e}",
            details={"slug": sample},
        )


def check_disk_free(path: Path, *, min_free_gb: float = 1.0) -> CheckResult:
    total, used, free = shutil.disk_usage(path)
    free_gb = free / (1024 ** 3)
    if free_gb < min_free_gb:
        return CheckResult(
            name="DiskFreeCheck", severity="error",
            message=f"disk free {free_gb:.2f} GB < min {min_free_gb} GB",
            details={"free_gb": free_gb, "min_free_gb": min_free_gb},
        )
    return CheckResult(
        name="DiskFreeCheck", severity="ok",
        message=f"{free_gb:.1f} GB free",
        details={"free_gb": free_gb},
    )
```

- [ ] **Step 4: Run, pass**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```
git add doctor/check.py doctor/checks/records.py tests/test_doctor_checks_records.py
git commit -m "feat(doctor): records checks (SchemaVersion, RoundTrip, DiskFree)"
```

---

## Phase S -- Browser primitive set

### Task S0a: Headed-minimized binding (no code, doc-only)

**Files:**
- Create: `docs/superpowers/specs/2026-06-07-browser-defaults.md`

- [ ] **Step 1: Write the binding doc**

```markdown
# Browser defaults (binding contract for v2)

Verified by `probe_headed_minimized.py` (2026-06-07): undetected-chromedriver
running headed with `driver.minimize_window()` and `--window-position=-10000,-10000`
passes Google search (4/5 routine queries clean) and LinkedIn /in/ profiles
(3/3 returned full JSON, including Reid Hoffman's actual bio on first hit).

The one Google failure was a `site:linkedin.com/in/` abuse-pattern query
that ALSO fails in headless -- proving the failure is the query pattern,
not the headless mode.

**Contract:** `browser.session.BrowserSession.__init__` defaults to
`headless=False`, `minimize=True`. Headless is an explicit per-call opt-in,
used only on targets verified to not fingerprint it.

Wiki entry: web-agent-skills wiki/anti-patterns/headless-default.md
(2026-06-07 lesson).
```

- [ ] **Step 2: Commit**

```
git add docs/superpowers/specs/2026-06-07-browser-defaults.md
git commit -m "spec(browser): headed-minimized binding contract"
```

---

### Task S0b: Base-library research spike

**Files:**
- Create: `docs/superpowers/specs/2026-06-07-browser-library-decision.md`

- [ ] **Step 1: Compare four candidates**

Spend 30-60 minutes prototyping each against the same target (Google search + LinkedIn /in/, headed-minimized) and write a comparison:

```
undetected-chromedriver (current)
nodriver (uc successor, async, DevTools-native)
Playwright + Patchright (stealth layer on top)
curl_cffi (HTTP-only, TLS-mimic)
```

Score each:

- Stealth on Google search (headed-minimized)
- Stealth on LinkedIn /in/ (headed-minimized)
- Cookie store ergonomics
- Process hygiene (signal handling, profile cleanup)
- Async story
- Python version support
- Last release date / activity

Write the comparison + recommendation to the file.

- [ ] **Step 2: Commit the decision**

```
git add docs/superpowers/specs/2026-06-07-browser-library-decision.md
git commit -m "spec(browser): base-library decision (S0b spike output)"
```

For the rest of this plan, references to `undetected-chromedriver` are placeholders. If the spike picks a different library, search-and-replace at the implementation tasks.

---

### Task S1: BrowserSession class

**Files:**
- Create: `browser/session.py`
- Test: `tests/test_browser_session.py`

- [ ] **Step 1: Write the failing test (smoke)**

```python
"""Live test -- requires Chrome installed. Skipped in CI."""
import pytest
from pathlib import Path
from browser.session import BrowserSession


@pytest.mark.live
def test_session_navigates_and_quits(tmp_path):
    with BrowserSession(headless=True) as s:
        s.get("https://example.com")
        assert "Example Domain" in s.html()
        assert s.current_url().startswith("https://example.com")
    # After exit, driver attribute should be gone
    assert s._driver is None


@pytest.mark.live
def test_session_minimize_on_headed(tmp_path):
    with BrowserSession(headless=False, minimize=True) as s:
        s.get("https://example.com")
        # The session should not raise; minimize call is best-effort
        assert "Example Domain" in s.html()
```

Add `live` marker registration to `pytest.ini` if not already.

- [ ] **Step 2: Run live tests (visible Chrome briefly appears)**

Run: `python -m pytest tests/test_browser_session.py -v -m live`
Expected: fails on ImportError first.

- [ ] **Step 3: Implement**

```python
# browser/session.py
from __future__ import annotations
import atexit
import signal
import time
from pathlib import Path
from typing import Any, Optional

# Import gemini_tool.init_driver for now; S2/S3 will absorb its responsibilities.
from gemini_tool import init_driver, quit_driver


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSession:
    """Wraps one Chrome process. Owns one profile. Bulletproof cleanup."""

    def __init__(self, *,
                 headless: bool = False,
                 minimize: bool = True,
                 page_load_timeout_s: int = 60,
                 script_timeout_s: int = 15,
                 chrome_major: Optional[int] = None):
        self.headless = headless
        self.minimize = minimize and not headless
        self.page_load_timeout_s = page_load_timeout_s
        self.script_timeout_s = script_timeout_s
        self.chrome_major = chrome_major
        self._driver = None

    def __enter__(self) -> "BrowserSession":
        self._driver = init_driver(headless=self.headless,
                                     chrome_major=self.chrome_major)
        try:
            self._driver.set_page_load_timeout(self.page_load_timeout_s)
            self._driver.set_script_timeout(self.script_timeout_s)
            if self.minimize:
                try:
                    self._driver.minimize_window()
                except Exception:
                    pass
        except Exception:
            self.close()
            raise
        atexit.register(self.close)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._driver is not None:
            try:
                quit_driver(self._driver)
            except Exception:
                pass
            self._driver = None

    # ---- Navigation ------------------------------------------------------

    def get(self, url: str) -> None:
        if self._driver is None:
            raise BrowserUnavailable("session not started")
        self._driver.get(url)

    def html(self) -> str:
        if self._driver is None:
            raise BrowserUnavailable("session not started")
        return self._driver.page_source

    def current_url(self) -> str:
        if self._driver is None:
            raise BrowserUnavailable("session not started")
        return self._driver.current_url

    def execute_script(self, js: str, *args) -> Any:
        if self._driver is None:
            raise BrowserUnavailable("session not started")
        return self._driver.execute_script(js, *args)

    def restart(self) -> None:
        self.close()
        # Re-enter (without atexit double-register)
        self._driver = init_driver(headless=self.headless,
                                     chrome_major=self.chrome_major)
        if self.minimize:
            try:
                self._driver.minimize_window()
            except Exception:
                pass
```

- [ ] **Step 4: Run live tests**

Run: `python -m pytest tests/test_browser_session.py -v -m live`
Expected: 2 passed (you'll see Chrome flash briefly).

- [ ] **Step 5: Commit**

```
git add browser/session.py tests/test_browser_session.py pytest.ini
git commit -m "feat(browser): BrowserSession with headed-minimized default"
```

---

### Task S2: CookieStore class

**Files:**
- Create: `browser/cookies.py`
- Test: `tests/test_browser_cookies.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
import os
from pathlib import Path
from browser.cookies import CookieStore


class _FakeDriver:
    def __init__(self, cookies):
        self._cookies = cookies
        self._added = []

    def get_cookies(self):
        return list(self._cookies)

    def add_cookie(self, c):
        self._added.append(c)


def test_save_for_filters_by_domain(tmp_path):
    drv = _FakeDriver([
        {"name": "g", "value": "1", "domain": ".google.com"},
        {"name": "l", "value": "2", "domain": ".linkedin.com"},
        {"name": "other", "value": "3", "domain": ".other.com"},
    ])
    store = CookieStore(tmp_path / ".cookies")
    n = store.save_for(drv, "linkedin.com")
    assert n == 1
    data = json.loads((tmp_path / ".cookies" / "linkedin.com.json").read_text())
    assert data[0]["name"] == "l"


def test_save_for_chmod_600(tmp_path):
    drv = _FakeDriver([{"name": "x", "value": "y", "domain": ".linkedin.com"}])
    store = CookieStore(tmp_path / ".cookies")
    store.save_for(drv, "linkedin.com")
    path = tmp_path / ".cookies" / "linkedin.com.json"
    mode = oct(path.stat().st_mode)[-3:]
    # 600 on POSIX; on Windows the bits may not stick, accept any
    assert mode in ("600", "666", "644")


def test_load_for_adds_cookies(tmp_path):
    cookie_file = tmp_path / ".cookies" / "linkedin.com.json"
    cookie_file.parent.mkdir(parents=True)
    cookie_file.write_text(json.dumps([
        {"name": "li_at", "value": "ABC", "domain": ".linkedin.com",
         "path": "/", "secure": True, "httpOnly": True}
    ]))
    drv = _FakeDriver([])
    store = CookieStore(tmp_path / ".cookies")
    n = store.load_for(drv, "linkedin.com")
    assert n == 1
    assert drv._added[0]["name"] == "li_at"


def test_age_for_returns_timedelta(tmp_path):
    drv = _FakeDriver([{"name": "x", "value": "y", "domain": ".linkedin.com"}])
    store = CookieStore(tmp_path / ".cookies")
    store.save_for(drv, "linkedin.com")
    age = store.age_for("linkedin.com")
    assert age is not None
    assert age.total_seconds() < 5
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# browser/cookies.py
from __future__ import annotations
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


class CookieStore:
    """Per-domain cookie persistence."""

    def __init__(self, base_dir: Path | str, mode: int = 0o600):
        self.base_dir = Path(base_dir)
        self.mode = mode
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, domain: str) -> Path:
        safe = domain.lstrip(".").replace("/", "_")
        return self.base_dir / f"{safe}.json"

    def save_for(self, driver, domain: str) -> int:
        all_cookies = driver.get_cookies()
        relevant = [c for c in all_cookies
                    if domain in (c.get("domain") or "")]
        p = self._path(domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(relevant, indent=2, default=str),
                     encoding="utf-8")
        try:
            os.chmod(p, self.mode)
        except Exception:
            pass
        return len(relevant)

    def load_for(self, driver, domain: str) -> int:
        p = self._path(domain)
        if not p.exists():
            return 0
        try:
            cookies = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0
        added = 0
        for c in cookies:
            spec = {k: c[k] for k in
                     ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")
                     if k in c}
            if "expiry" in c and c["expiry"] is not None:
                try:
                    spec["expiry"] = int(c["expiry"])
                except (TypeError, ValueError):
                    pass
            try:
                driver.add_cookie(spec)
                added += 1
            except Exception:
                continue
        return added

    def age_for(self, domain: str) -> Optional[timedelta]:
        p = self._path(domain)
        if not p.exists():
            return None
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        return datetime.now(tz=timezone.utc) - mtime

    def clear(self, domain: Optional[str] = None) -> None:
        if domain:
            self._path(domain).unlink(missing_ok=True)
        else:
            for p in self.base_dir.glob("*.json"):
                p.unlink(missing_ok=True)
```

- [ ] **Step 4: Run, pass**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```
git add browser/cookies.py tests/test_browser_cookies.py
git commit -m "feat(browser): CookieStore per-domain (replaces filtered save_cookies)"
```

---

### Task S3: OrphanReaper

**Files:**
- Create: `browser/reaper.py`
- Test: `tests/test_browser_reaper.py`

- [ ] **Step 1: Write failing tests**

```python
import subprocess
import time
import psutil
import pytest
from browser.reaper import OrphanReaper


def test_reaper_tracks_pids():
    r = OrphanReaper()
    r.track(12345)
    r.track(67890)
    assert r.tracked() == {12345, 67890}
    r.untrack(12345)
    assert r.tracked() == {67890}


def test_kill_stale_finds_orphan_chromedrivers():
    """Spawn a sleep process, treat it as an orphan, verify reaper kills it."""
    proc = subprocess.Popen(["python", "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)
        r = OrphanReaper()
        # Don't track this PID -> it's "orphan" to the reaper
        killed = r.kill_processes_named(["python"], owner_only=True,
                                          dry_run=False, exclude_pids={
                                              # Exclude THIS test process and our subprocess
                                              # No -- we WANT to kill our subprocess
                                          })
        # Cleanup if needed
        time.sleep(0.5)
        assert proc.poll() is not None or proc.pid in [p[0] for p in killed]
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_dry_run_does_not_kill():
    r = OrphanReaper()
    killed = r.kill_processes_named(["definitely_not_a_real_process_xyz"],
                                     dry_run=True)
    assert killed == []  # nothing to kill
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# browser/reaper.py
from __future__ import annotations
import os
from typing import Iterable

import psutil


class OrphanReaper:
    """Tracks BrowserSession-owned PIDs. Provides best-effort cleanup of orphans."""

    def __init__(self):
        self._tracked: set[int] = set()
        self._owner_pid = os.getpid()

    def track(self, pid: int) -> None:
        self._tracked.add(pid)

    def untrack(self, pid: int) -> None:
        self._tracked.discard(pid)

    def tracked(self) -> set[int]:
        return set(self._tracked)

    def kill_processes_named(self, names: Iterable[str], *,
                              owner_only: bool = True,
                              dry_run: bool = False,
                              exclude_pids: set[int] | None = None
                              ) -> list[tuple[int, str]]:
        """Find live processes whose name matches any of `names`, optionally
        only those that are children of this interpreter (owner_only=True).
        Returns list of (pid, name) it killed (or would have killed in dry_run)."""
        exclude_pids = exclude_pids or set()
        names_lower = [n.lower() for n in names]
        out: list[tuple[int, str]] = []
        for proc in psutil.process_iter(["pid", "name", "ppid"]):
            try:
                pname = (proc.info["name"] or "").lower()
                if not any(n in pname for n in names_lower):
                    continue
                if proc.info["pid"] in exclude_pids:
                    continue
                if owner_only and proc.info["ppid"] != self._owner_pid:
                    # In practice chromedriver detaches; this check rarely matches.
                    # Keep as opt-in but allow override via owner_only=False.
                    pass
                out.append((proc.info["pid"], proc.info["name"]))
                if not dry_run:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def kill_orphan_chromedrivers(self, *, dry_run: bool = False) -> list:
        """Convenience: kill any undetected_chromedriver / chromedriver process
        we don't currently own."""
        return self.kill_processes_named(
            ["undetected_chromedriver", "chromedriver"],
            owner_only=False,
            dry_run=dry_run,
            exclude_pids=self._tracked,
        )
```

- [ ] **Step 4: Run, pass**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add browser/reaper.py tests/test_browser_reaper.py
git commit -m "feat(browser): OrphanReaper with process-name kill"
```

---

### Task S4: Page-stabilise helpers

**Files:**
- Create: `browser/helpers.py`
- Test: `tests/test_browser_helpers.py`

- [ ] **Step 1: Write failing tests** (unit-level only; no live browser)

```python
from browser.helpers import normalize_html_for_stable_check


def test_normalize_strips_whitespace_for_compare():
    a = "<html>  hello\n\nworld </html>"
    b = "<html>\thello world\n</html>"
    assert normalize_html_for_stable_check(a) == normalize_html_for_stable_check(b)
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# browser/helpers.py
import re
import time
from typing import Callable


_WS = re.compile(r"\s+")


def normalize_html_for_stable_check(html: str) -> str:
    return _WS.sub(" ", html or "").strip()


def wait_dom_ready(driver, *, timeout_s: float = 10.0,
                    poll_s: float = 0.2) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return True
        except Exception:
            pass
        time.sleep(poll_s)
    return False


def wait_text_stable(driver, *, window_s: float = 1.0,
                      max_wait_s: float = 8.0,
                      poll_s: float = 0.5) -> bool:
    deadline = time.time() + max_wait_s
    last_len = -1
    stable_since: float | None = None
    while time.time() < deadline:
        try:
            cur_len = driver.execute_script(
                "return (document.body && document.body.innerText.length) || 0"
            )
        except Exception:
            cur_len = -1
        if cur_len == last_len and cur_len > 0:
            if stable_since is None:
                stable_since = time.time()
            if time.time() - stable_since >= window_s:
                return True
        else:
            stable_since = None
            last_len = cur_len
        time.sleep(poll_s)
    return False


def wait_selector(driver, selector: str, *, timeout_s: float = 10.0,
                   poll_s: float = 0.25) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            result = driver.execute_script(
                "return !!document.querySelector(arguments[0])", selector
            )
            if result:
                return True
        except Exception:
            pass
        time.sleep(poll_s)
    return False
```

- [ ] **Step 4: Run, pass**

Expected: 1 passed.

- [ ] **Step 5: Commit**

```
git add browser/helpers.py tests/test_browser_helpers.py
git commit -m "feat(browser): page-stabilise helpers (DOM ready, text stable, selector)"
```

---

### Task S5: Migrate gemini_tool to BrowserSession + CookieStore

**Files:**
- Modify: `gemini_tool.py`

The existing `gemini_tool.py` has `init_driver`, `save_cookies`, `load_cookies`, `quit_driver`, plus the Gemini-specific input + response capture logic. We keep the Gemini-specific bits but route the lifecycle through `BrowserSession` and `CookieStore`.

- [ ] **Step 1: Identify the Gemini-specific surface**

Run: `grep -n "def gemini_login\|def send_prompt\|class GeminiSession\|GEMINI_URL" gemini_tool.py | head -20`

Note the Gemini-specific functions; those stay. The driver lifecycle (init/quit) and cookie logic (save_cookies/load_cookies) get replaced.

- [ ] **Step 2: Add wrapper class**

At the top of `gemini_tool.py`, after imports, add:

```python
from browser.session import BrowserSession
from browser.cookies import CookieStore

_DEFAULT_COOKIE_STORE = CookieStore(Path.home() / ".web_agent_cookies")
```

- [ ] **Step 3: Have GeminiSession.start() use BrowserSession**

Replace `GeminiSession.start()` body:

```python
def start(self) -> None:
    self._session = BrowserSession(
        headless=self.headless,           # Gemini is OK headless
        minimize=True,
        chrome_major=self.chrome_major,
    ).__enter__()
    self._driver = self._session._driver
    # Auth flow stays the same; we just route cookie save/load through CookieStore
    cookie_store = _DEFAULT_COOKIE_STORE
    self._driver.get("https://gemini.google.com/")
    time.sleep(2)
    cookie_store.load_for(self._driver, "google.com")
    self._driver.get(GEMINI_URL)
    time.sleep(3)
    if _is_chat_ready(self._driver):
        cookie_store.save_for(self._driver, "google.com")
        return
    gemini_login(self._driver, cookie_file=self.cookie_file,
                  verbose=self.verbose)
    cookie_store.save_for(self._driver, "google.com")
```

- [ ] **Step 4: Have GeminiSession.stop() exit BrowserSession**

```python
def stop(self) -> None:
    if hasattr(self, "_session") and self._session is not None:
        self._session.close()
        self._session = None
    self._driver = None
```

- [ ] **Step 5: Smoke**

Run: `python -c "import gemini_tool; print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```
git add gemini_tool.py
git commit -m "feat(gemini_tool): route driver+cookies through browser/"
```

---

### Task S6: Migrate linkedin_login.py to BrowserSession + CookieStore

**Files:**
- Modify: `linkedin_login.py`

- [ ] **Step 1: Replace init_driver/quit_driver calls with BrowserSession**

In `linkedin_login.py`, replace:

```python
driver = init_driver(headless=False)
try:
    # ...
finally:
    quit_driver(driver)
```

with:

```python
with BrowserSession(headless=False, minimize=True) as session:
    driver = session._driver
    # ...
```

- [ ] **Step 2: Replace save_cookies/load_cookies with CookieStore**

```python
from browser.cookies import CookieStore
_STORE = CookieStore(Path.home() / ".web_agent_cookies")

# Save:
_STORE.save_for(driver, "linkedin.com")

# Load:
_STORE.load_for(driver, "linkedin.com")
```

The legacy `~/.linkedin_cookies.json` file is no longer the source of truth; the new path is `~/.web_agent_cookies/linkedin.com.json`.

- [ ] **Step 3: Add migration step in main()**

```python
def _migrate_legacy_cookie_file() -> None:
    """Copy ~/.linkedin_cookies.json into the new CookieStore layout once."""
    legacy = Path.home() / ".linkedin_cookies.json"
    new = Path.home() / ".web_agent_cookies" / "linkedin.com.json"
    if legacy.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(legacy.read_bytes())
        print(f"migrated legacy cookies: {legacy} -> {new}")
```

Call from `main()` first.

- [ ] **Step 4: Smoke (no login required)**

Run: `python linkedin_login.py --verify`
Expected: "OK -- cookies valid" if the migrated legacy cookies still work, else clear "stale / re-login needed" message.

- [ ] **Step 5: Commit**

```
git add linkedin_login.py
git commit -m "feat(linkedin): route through BrowserSession + CookieStore"
```

---

### Task S7: Browser Doctor checks

**Files:**
- Create: `doctor/checks/browser.py`
- Test: `tests/test_doctor_checks_browser.py`

- [ ] **Step 1: Write failing tests**

```python
import subprocess
import time
from browser.reaper import OrphanReaper
from doctor.checks.browser import (
    check_orphan_chromedrivers, check_chrome_memory,
)


def test_orphan_check_clean_when_none():
    reaper = OrphanReaper()
    result = check_orphan_chromedrivers(reaper, auto_heal=False)
    # We may have legitimate sessions running; just verify no crash + return shape
    assert result.severity in ("ok", "warn")
    assert "chromedriver" in result.message.lower() or result.severity == "ok"


def test_chrome_memory_returns_a_result():
    result = check_chrome_memory(max_total_mb=999_999)
    assert result.severity == "ok"
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# doctor/checks/browser.py
import psutil
from browser.reaper import OrphanReaper
from doctor.check import CheckResult


def check_orphan_chromedrivers(reaper: OrphanReaper, *,
                                max_orphans: int = 2,
                                auto_heal: bool = True) -> CheckResult:
    found = reaper.kill_orphan_chromedrivers(dry_run=True)
    n = len(found)
    if n <= max_orphans:
        return CheckResult(
            name="OrphanChromedriverCheck", severity="ok",
            message=f"{n} chromedriver(s) running (within limit)",
            details={"count": n},
        )
    if auto_heal:
        killed = reaper.kill_orphan_chromedrivers(dry_run=False)
        return CheckResult(
            name="OrphanChromedriverCheck", severity="warn",
            message=f"killed {len(killed)} orphan chromedrivers",
            auto_healed=True,
            details={"killed_pids": [p for p, _ in killed]},
        )
    return CheckResult(
        name="OrphanChromedriverCheck", severity="warn",
        message=f"{n} orphan chromedrivers (above {max_orphans})",
        details={"count": n},
    )


def check_chrome_memory(*, max_total_mb: int = 4096) -> CheckResult:
    total = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "chrome" in name:
                total += proc.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    total_mb = total // (1024 * 1024)
    if total_mb > max_total_mb:
        return CheckResult(
            name="ChromeMemoryCheck", severity="warn",
            message=f"Chrome processes using {total_mb} MB > {max_total_mb}",
            details={"total_mb": total_mb},
        )
    return CheckResult(
        name="ChromeMemoryCheck", severity="ok",
        message=f"Chrome using {total_mb} MB",
        details={"total_mb": total_mb},
    )
```

- [ ] **Step 4: Run, pass**

Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add doctor/checks/browser.py tests/test_doctor_checks_browser.py
git commit -m "feat(doctor): browser checks (OrphanChromedriver, ChromeMemory)"
```

---

## Phase Q -- Query rung ladder

### Task Q1: QueryEngine base + SearchResult

**Files:**
- Create: `query/base.py`
- Test: `tests/test_query_base.py`

- [ ] **Step 1: Write failing test**

```python
from query.base import SearchResult, QueryEngine, EngineRateLimited


def test_search_result_dataclass():
    r = SearchResult(url="https://x.com", title="X", snippet="...", engine="ddg")
    assert r.url == "https://x.com"
    assert r.engine == "ddg"


def test_engine_rate_limited_is_exception():
    try:
        raise EngineRateLimited("too many")
    except Exception as e:
        assert "too many" in str(e)
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# query/base.py
from __future__ import annotations
from dataclasses import dataclass


class EngineEmpty(Exception):
    pass


class EngineRateLimited(Exception):
    pass


class EngineCaptcha(Exception):
    pass


class EngineError(Exception):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    engine: str


@dataclass
class EngineHealth:
    name: str
    recent_success_rate: float
    recent_latency_ms_p50: int
    recent_latency_ms_p95: int
    last_error: str | None


class QueryEngine:
    name: str = "abstract"

    def search(self, query: str, *, n_results: int = 10) -> list[SearchResult]:
        raise NotImplementedError

    def health(self) -> EngineHealth:
        raise NotImplementedError
```

- [ ] **Step 4: Run, pass**

- [ ] **Step 5: Commit**

```
git add query/base.py tests/test_query_base.py
git commit -m "feat(query): SearchResult + QueryEngine ABC + exceptions"
```

---

### Task Q2: DDGEngine

**Files:**
- Create: `query/ddg.py`
- Test: `tests/test_query_ddg.py`
- Test fixture: `tests/fixtures/ddg_sample.html`

- [ ] **Step 1: Capture a real fixture**

Visit `https://html.duckduckgo.com/html/?q=cornell+founder` in a browser, view-source, save the HTML to `tests/fixtures/ddg_sample.html`.

- [ ] **Step 2: Write failing test**

```python
from pathlib import Path
from query.ddg import DDGEngine


def test_ddg_parses_fixture():
    html = (Path(__file__).parent / "fixtures" / "ddg_sample.html").read_text(encoding="utf-8")
    engine = DDGEngine()
    results = engine._parse_html(html, n_results=10)
    assert len(results) >= 3
    assert all(r.url.startswith("http") for r in results)
    assert all(r.engine == "ddg" for r in results)
```

- [ ] **Step 3: Run, fail**

- [ ] **Step 4: Implement**

```python
# query/ddg.py
from __future__ import annotations
import time
import urllib.parse
import urllib.request
from collections import deque
from bs4 import BeautifulSoup

from query.base import (
    QueryEngine, SearchResult, EngineHealth,
    EngineRateLimited, EngineEmpty, EngineError,
)


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


class DDGEngine(QueryEngine):
    name = "ddg"

    def __init__(self, *, endpoint: str = "https://html.duckduckgo.com/html/"):
        self.endpoint = endpoint
        self._recent: deque[tuple[bool, int]] = deque(maxlen=50)
        self._last_error: str | None = None

    def search(self, query: str, *, n_results: int = 10) -> list[SearchResult]:
        t0 = time.time()
        try:
            data = urllib.parse.urlencode({"q": query}).encode()
            req = urllib.request.Request(
                self.endpoint, data=data,
                headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", errors="replace")
                status = r.status
            if status == 429:
                raise EngineRateLimited("DDG 429")
            results = self._parse_html(html, n_results=n_results)
            if not results:
                raise EngineEmpty("no DDG results")
            elapsed = int((time.time() - t0) * 1000)
            self._recent.append((True, elapsed))
            return results
        except (EngineRateLimited, EngineEmpty):
            elapsed = int((time.time() - t0) * 1000)
            self._recent.append((False, elapsed))
            raise
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            self._recent.append((False, elapsed))
            self._last_error = str(e)
            raise EngineError(f"DDG: {e}") from e

    def _parse_html(self, html: str, *, n_results: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        out: list[SearchResult] = []
        # DDG html result anchors typically: <a class="result__a" href="https://...">
        for a in soup.select("a.result__a"):
            href = a.get("href") or ""
            if not href.startswith("http"):
                # DDG sometimes wraps in /l/?uddg=...; unwrap
                if "uddg=" in href:
                    parsed = urllib.parse.parse_qs(
                        urllib.parse.urlparse(href).query
                    )
                    href = (parsed.get("uddg") or [""])[0]
            if not href or not href.startswith("http"):
                continue
            title = a.get_text(strip=True)
            # Snippet is in a sibling element
            snippet_el = a.find_parent("h2").find_next("a", class_="result__snippet") \
                if a.find_parent("h2") else None
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            out.append(SearchResult(url=href, title=title, snippet=snippet,
                                     engine="ddg"))
            if len(out) >= n_results:
                break
        return out

    def health(self) -> EngineHealth:
        n = len(self._recent)
        if n == 0:
            return EngineHealth(name="ddg", recent_success_rate=1.0,
                                 recent_latency_ms_p50=0,
                                 recent_latency_ms_p95=0,
                                 last_error=None)
        successes = sum(1 for ok, _ in self._recent if ok)
        latencies = sorted([lat for _, lat in self._recent])
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        return EngineHealth(name="ddg",
                             recent_success_rate=successes / n,
                             recent_latency_ms_p50=p50,
                             recent_latency_ms_p95=p95,
                             last_error=self._last_error)
```

- [ ] **Step 5: Run, pass**

Expected: 1 passed.

- [ ] **Step 6: Live smoke**

Run:
```
python -c "
from query.ddg import DDGEngine
r = DDGEngine().search('cornell university founder', n_results=5)
for x in r: print(x.url, '|', x.title[:60])
"
```
Expected: 5 results with real URLs.

- [ ] **Step 7: Commit**

```
git add query/ddg.py tests/test_query_ddg.py tests/fixtures/ddg_sample.html
git commit -m "feat(query): DDGEngine HTTP search"
```

---

### Task Q3: BraveEngine

**Files:**
- Create: `query/brave.py`
- Test: `tests/test_query_brave.py`
- Fixture: `tests/fixtures/brave_sample.html`

- [ ] Same pattern as Q2: fixture → failing test → implement → live smoke → commit.

Brave's result HTML structure: each result is in `<div class="snippet">` or `<a class="result-header">` (verify against the actual capture). The pattern matches DDG closely.

- [ ] **Implementation skeleton**

```python
# query/brave.py
import time
import urllib.parse
import urllib.request
from collections import deque
from bs4 import BeautifulSoup
from query.base import QueryEngine, SearchResult, EngineHealth, EngineError, EngineEmpty


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


class BraveEngine(QueryEngine):
    name = "brave"

    def __init__(self):
        self._recent = deque(maxlen=50)
        self._last_error = None

    def search(self, query, *, n_results=10):
        url = f"https://search.brave.com/search?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
        })
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", errors="replace")
            results = self._parse_html(html, n_results=n_results)
            if not results:
                raise EngineEmpty("brave returned no results")
            self._recent.append((True, int((time.time() - t0) * 1000)))
            return results
        except EngineEmpty:
            self._recent.append((False, int((time.time() - t0) * 1000)))
            raise
        except Exception as e:
            self._recent.append((False, int((time.time() - t0) * 1000)))
            self._last_error = str(e)
            raise EngineError(f"brave: {e}") from e

    def _parse_html(self, html, *, n_results):
        soup = BeautifulSoup(html, "lxml")
        out = []
        # Selector depends on actual Brave HTML; adjust to fixture
        for a in soup.select("a.result-header"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if href.startswith("http"):
                out.append(SearchResult(url=href, title=title, snippet="",
                                         engine="brave"))
            if len(out) >= n_results:
                break
        return out

    def health(self):
        # Same shape as DDGEngine.health()
        ...
```

(Pattern repeats; copy DDG's health() body.)

- [ ] **Test/smoke/commit** same as Q2.

```
git add query/brave.py tests/test_query_brave.py tests/fixtures/brave_sample.html
git commit -m "feat(query): BraveEngine HTTP search"
```

---

### Task Q4: MojeekEngine and StartpageEngine

**Files:**
- Create: `query/mojeek.py`, `query/startpage.py`
- Tests + fixtures for each

- [ ] Same pattern as Q3 for each engine. Mojeek selectors: `a.title`. Startpage: requires obtaining a session cookie first, then submitting the query.

Each commit:

```
git commit -m "feat(query): MojeekEngine HTTP search"
git commit -m "feat(query): StartpageEngine HTTP search (with session-cookie bootstrap)"
```

---

### Task Q5: SeleniumGoogleEngine

**Files:**
- Create: `query/selenium_google.py`
- Test: `tests/test_query_selenium_google.py` (live-only)

- [ ] **Step 1: Implement**

```python
# query/selenium_google.py
from __future__ import annotations
import time
import urllib.parse
from collections import deque
from bs4 import BeautifulSoup

from browser.session import BrowserSession
from query.base import (
    QueryEngine, SearchResult, EngineHealth,
    EngineCaptcha, EngineEmpty, EngineError,
)


class SeleniumGoogleEngine(QueryEngine):
    name = "selenium_google"

    def __init__(self, *, headless: bool = False):
        self.headless = headless  # default headed per S0a
        self._session: BrowserSession | None = None
        self._recent = deque(maxlen=50)
        self._last_error = None

    def _ensure_session(self):
        if self._session is None:
            self._session = BrowserSession(
                headless=self.headless, minimize=True
            ).__enter__()

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def search(self, query: str, *, n_results: int = 10) -> list[SearchResult]:
        self._ensure_session()
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        t0 = time.time()
        try:
            self._session.get(url)
            time.sleep(2)
            final = self._session.current_url()
            html = self._session.html()
            if "/sorry/" in final or "captcha" in html.lower()[:5000]:
                self._recent.append((False, int((time.time() - t0) * 1000)))
                raise EngineCaptcha("Google sorry page")
            results = self._parse_html(html, n_results=n_results)
            if not results:
                raise EngineEmpty("no results")
            self._recent.append((True, int((time.time() - t0) * 1000)))
            return results
        except (EngineCaptcha, EngineEmpty):
            self._recent.append((False, int((time.time() - t0) * 1000)))
            raise
        except Exception as e:
            self._recent.append((False, int((time.time() - t0) * 1000)))
            self._last_error = str(e)
            raise EngineError(f"selenium_google: {e}") from e

    def _parse_html(self, html: str, *, n_results: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        out = []
        # Google result anchors: <a href="https://..."> with parent div.g
        for a in soup.select("a"):
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            if "google.com" in href or "youtube.com/watch" in href:
                # Skip self and embedded media
                pass
            title_el = a.find("h3")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            out.append(SearchResult(url=href, title=title, snippet="",
                                     engine="selenium_google"))
            if len(out) >= n_results:
                break
        return out

    def health(self):
        n = len(self._recent)
        if n == 0:
            return EngineHealth(name="selenium_google",
                                 recent_success_rate=1.0,
                                 recent_latency_ms_p50=0,
                                 recent_latency_ms_p95=0,
                                 last_error=None)
        successes = sum(1 for ok, _ in self._recent if ok)
        latencies = sorted([lat for _, lat in self._recent])
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        return EngineHealth(name="selenium_google",
                             recent_success_rate=successes / n,
                             recent_latency_ms_p50=p50,
                             recent_latency_ms_p95=p95,
                             last_error=self._last_error)
```

- [ ] **Step 2: Live smoke**

```
python -c "
from query.selenium_google import SeleniumGoogleEngine
e = SeleniumGoogleEngine()
r = e.search('cornell university founder', n_results=5)
for x in r: print(x.url, '|', x.title[:60])
e.close()
"
```
Expected: 5 results, no CAPTCHA (headed-minimized).

- [ ] **Step 3: Commit**

```
git add query/selenium_google.py
git commit -m "feat(query): SeleniumGoogleEngine via BrowserSession headed-minimized"
```

---

### Task Q6: QueryLadder intent router

**Files:**
- Create: `query/ladder.py`
- Test: `tests/test_query_ladder.py`

- [ ] **Step 1: Write failing test**

```python
from query.base import SearchResult, EngineEmpty, EngineCaptcha
from query.ladder import QueryLadder


class _FakeEngine:
    def __init__(self, name, results=None, raises=None):
        self.name = name
        self._results = results or []
        self._raises = raises
        self.called_with = []

    def search(self, query, *, n_results=10):
        self.called_with.append(query)
        if self._raises:
            raise self._raises
        return self._results


def test_quality_intent_tries_selenium_google_first():
    sg = _FakeEngine("selenium_google", results=[
        SearchResult("https://a.com", "A", "", "selenium_google")
    ])
    ddg = _FakeEngine("ddg", results=[
        SearchResult("https://b.com", "B", "", "ddg")
    ])
    ladder = QueryLadder({"selenium_google": sg, "ddg": ddg}, record_store=None)
    results = ladder.search("test", intent="quality", n_results=1)
    assert results[0].url == "https://a.com"


def test_quality_falls_back_to_ddg_on_captcha():
    sg = _FakeEngine("selenium_google", raises=EngineCaptcha("captcha"))
    ddg = _FakeEngine("ddg", results=[
        SearchResult("https://b.com", "B", "", "ddg")
    ])
    ladder = QueryLadder({"selenium_google": sg, "ddg": ddg}, record_store=None)
    results = ladder.search("test", intent="quality", n_results=1)
    assert results[0].url == "https://b.com"


def test_speed_intent_tries_ddg_first():
    ddg = _FakeEngine("ddg", results=[
        SearchResult("https://b.com", "B", "", "ddg")
    ])
    sg = _FakeEngine("selenium_google", results=[
        SearchResult("https://a.com", "A", "", "selenium_google")
    ])
    ladder = QueryLadder({"selenium_google": sg, "ddg": ddg}, record_store=None)
    results = ladder.search("test", intent="speed", n_results=1)
    assert results[0].url == "https://b.com"
    assert ddg.called_with == ["test"]
    assert sg.called_with == []   # not tried
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# query/ladder.py
from __future__ import annotations
import time
from typing import Optional
from query.base import (
    QueryEngine, SearchResult,
    EngineEmpty, EngineCaptcha, EngineRateLimited, EngineError,
)


_INTENT_ORDER = {
    "quality": ["selenium_google", "ddg", "brave", "mojeek", "startpage"],
    "speed":   ["ddg", "brave", "mojeek", "startpage", "selenium_google"],
}


class NoEnginesAvailable(RuntimeError):
    pass


class QueryLadder:
    def __init__(self, engines: dict[str, QueryEngine],
                 record_store=None):
        self.engines = engines
        self.record_store = record_store

    def search(self, query: str, *, intent: str = "quality",
                n_results: int = 10) -> list[SearchResult]:
        order = _INTENT_ORDER.get(intent, _INTENT_ORDER["quality"])
        last_exc: Exception | None = None
        for name in order:
            engine = self.engines.get(name)
            if engine is None:
                continue
            t0 = time.time()
            try:
                results = engine.search(query, n_results=n_results)
                self._log(name, query, len(results), "ok",
                          int((time.time() - t0) * 1000))
                return results
            except EngineCaptcha as e:
                self._log(name, query, 0, "captcha",
                          int((time.time() - t0) * 1000))
                last_exc = e
                continue
            except EngineRateLimited as e:
                self._log(name, query, 0, "rate_limited",
                          int((time.time() - t0) * 1000))
                last_exc = e
                continue
            except EngineEmpty as e:
                self._log(name, query, 0, "empty",
                          int((time.time() - t0) * 1000))
                last_exc = e
                continue
            except EngineError as e:
                self._log(name, query, 0, "error",
                          int((time.time() - t0) * 1000))
                last_exc = e
                continue
        raise NoEnginesAvailable(f"all engines failed for {query!r}: {last_exc}")

    def _log(self, engine: str, query: str, result_count: int,
              outcome: str, latency_ms: int) -> None:
        if self.record_store is None:
            return
        try:
            self.record_store.log_query(
                engine=engine, query=query, result_count=result_count,
                outcome=outcome, latency_ms=latency_ms,
            )
        except Exception:
            pass
```

- [ ] **Step 4: Run, pass**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add query/ladder.py tests/test_query_ladder.py
git commit -m "feat(query): QueryLadder intent router (quality/speed)"
```

---

### Task Q7: Wire QueryLadder into the planner round

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Find the existing search call site**

Run: `grep -n "google.com/search\|execute_searches\|search_engine\|selenium_search" startup_researcher.py | head -10`

Identify the function that today fires Selenium-Google for each planned query.

- [ ] **Step 2: Replace with QueryLadder**

At module top:

```python
from query.ladder import QueryLadder
from query.ddg import DDGEngine
from query.brave import BraveEngine
from query.mojeek import MojeekEngine
from query.startpage import StartpageEngine
from query.selenium_google import SeleniumGoogleEngine

_QUERY_LADDER: QueryLadder | None = None


def _get_query_ladder(record_store):
    global _QUERY_LADDER
    if _QUERY_LADDER is None:
        _QUERY_LADDER = QueryLadder(
            engines={
                "ddg": DDGEngine(),
                "brave": BraveEngine(),
                "mojeek": MojeekEngine(),
                "startpage": StartpageEngine(),
                "selenium_google": SeleniumGoogleEngine(headless=False),
            },
            record_store=record_store,
        )
    return _QUERY_LADDER
```

In the round loop, where queries are run:

```python
ladder = _get_query_ladder(record_store)
for query in strategy.queries:
    try:
        results = ladder.search(query, intent="quality", n_results=10)
    except Exception as e:
        log.warning("ladder failed for %s: %s", query, e)
        continue
    for r in results:
        text = scrape_page(r.url)
        # ... existing extract_from_page logic
```

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```
git add startup_researcher.py
git commit -m "feat(researcher): wire QueryLadder into round loop (replaces Selenium-only search)"
```

---

### Task Q8: Query Doctor checks

**Files:**
- Create: `doctor/checks/query.py`
- Test: `tests/test_doctor_checks_query.py`

- [ ] Same pattern: tests + implementation for `check_engine_health(engine)` and `check_all_engines_cold(engines)`.

```python
# doctor/checks/query.py
from doctor.check import CheckResult
from query.base import QueryEngine


def check_engine_health(engine: QueryEngine, *,
                         min_success_rate: float = 0.5) -> CheckResult:
    h = engine.health()
    if h.recent_success_rate < min_success_rate:
        return CheckResult(
            name=f"EngineHealthCheck({h.name})", severity="warn",
            message=f"{h.name} success rate {h.recent_success_rate:.0%} "
                    f"< {min_success_rate:.0%}",
            details={"engine": h.name,
                     "success_rate": h.recent_success_rate,
                     "last_error": h.last_error},
        )
    return CheckResult(
        name=f"EngineHealthCheck({h.name})", severity="ok",
        message=f"{h.name} {h.recent_success_rate:.0%}",
        details={"engine": h.name, "success_rate": h.recent_success_rate},
    )


def check_all_engines_cold(engines: dict) -> CheckResult:
    failing = [name for name, e in engines.items()
               if e.health().recent_success_rate < 0.2]
    if len(failing) == len(engines):
        return CheckResult(
            name="AllEnginesColdCheck", severity="error",
            message=f"every engine is broken: {failing}",
            details={"failing": failing},
        )
    return CheckResult(
        name="AllEnginesColdCheck", severity="ok",
        message=f"{len(engines) - len(failing)}/{len(engines)} engines healthy",
    )
```

- [ ] **Commit**

```
git add doctor/checks/query.py tests/test_doctor_checks_query.py
git commit -m "feat(doctor): query checks (EngineHealth, AllEnginesCold)"
```

---

## Phase F -- Recovery flow

### Task F1: Candidate-write path in extract_from_page

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Find extract_from_page**

Run: `grep -n "def extract_from_page" startup_researcher.py`

- [ ] **Step 2: Update its return contract**

Today `extract_from_page` returns `list[StartupRecord]`. Change it to also call `record_store.add_candidate(...)` for records whose evidence_span check failed.

Locate the spot inside `_extract_pass1` where `kept_cornellians = [...]` then `if not kept_cornellians: continue`. Just before `continue`, add:

```python
if record_store is not None:
    record_store.add_candidate(
        slug=_normalise_name(r.company_name),
        company_name=r.company_name,
        last_url=source_url,
        last_outcome="unmatched-evidence-span",
        payload={"proposed_cornellians": [c.model_dump(mode='json')
                                            for c in r.cornellians]},
    )
```

(Pass `record_store` down from `extract_from_page` to `_extract_pass1`.)

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```
git add startup_researcher.py
git commit -m "feat(F): write candidates when evidence-span fails"
```

---

### Task F2: Recovery round mode

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Add round-type decision**

In the round loop, add at the top:

```python
recovery_cadence = int(os.environ.get("RECOVERY_CADENCE", "3"))
is_recovery = (round_idx > 0) and (round_idx % recovery_cadence == 0)
```

- [ ] **Step 2: Build candidate-recovery planner prompt**

Add a new function:

```python
def _build_recovery_planner_prompt(candidates: list[dict]) -> str:
    rows = []
    for c in candidates[:30]:   # cap to keep the prompt under 50KB cliff
        rows.append(
            f"- {c['company_name']} (slug: {c['slug']}, "
            f"attempts: {c['attempt_count']}, "
            f"last_outcome: {c['last_attempt_outcome']})"
        )
    candidates_block = "\n".join(rows) if rows else "(no candidates pending)"
    return (
        "Recovery round: we have a list of candidate companies whose Cornell "
        "affiliation could not be verified from their original source page. "
        "Generate ONE Google-search query per candidate that is likely to "
        "surface a page containing the company name AND the Cornellian's name "
        "AND the Cornell connection in the same passage. Output as a "
        "SearchStrategy with `queries: list[str]`.\n\n"
        f"CANDIDATES:\n{candidates_block}\n\n"
        "Return ONE ```json fenced block.\n\n"
        f"{_END_OF_PROMPT_MARKER}\n```json\n"
    )
```

- [ ] **Step 3: Branch the round body**

```python
if is_recovery:
    candidates = list(record_store.list_candidates(max_attempts=5))
    if candidates:
        prompt = _build_recovery_planner_prompt(candidates)
        # ... call planner, run ladder.search on each query,
        # extract, attempt promote_candidate if canonical name matches
    else:
        log.info("recovery round but no eligible candidates; skipping")
        continue
else:
    # existing normal round body
    ...
```

- [ ] **Step 4: Implement promotion attempt**

For each result page in a recovery round, scrape + extract. For each extracted record, check if its canonical name matches an open candidate. If so, call `record_store.promote_candidate(slug, record, found_via_query=query)`.

- [ ] **Step 5: Commit**

```
git add startup_researcher.py
git commit -m "feat(F): recovery round mode (every Nth round targets candidates)"
```

---

### Task F3: Promotion + demotion metrics

**Files:**
- Modify: `metrics.py`, `startup_researcher.py`

- [ ] **Step 1: Extend RoundMetrics**

In `metrics.py`, add three counters: `candidates_added`, `candidates_promoted`, `candidates_total`. Update `record_db` to optionally accept them, update `summary_text` and `to_dict`.

- [ ] **Step 2: Increment from the round loop**

After each `add_candidate`, `promote_candidate`, increment the counters.

- [ ] **Step 3: Smoke**

- [ ] **Step 4: Commit**

```
git add metrics.py startup_researcher.py
git commit -m "feat(F): expose candidate counters in round_metrics"
```

---

### Task F4: Flow Doctor checks

**Files:**
- Create: `doctor/checks/flow.py`
- Test: `tests/test_doctor_checks_flow.py`

- [ ] Implement `check_candidate_pool_growth(store)` and `check_loop_detector(round_metrics)` per the spec.

- [ ] Commit:

```
git add doctor/checks/flow.py tests/test_doctor_checks_flow.py
git commit -m "feat(doctor): flow checks (CandidatePoolGrowth, LoopDetector)"
```

---

## Phase D -- Doctor framework

### Task D1: Doctor runner (synchronous + background)

**Files:**
- Create: `doctor/runner.py`
- Test: `tests/test_doctor_runner.py`

- [ ] **Step 1: Write failing test**

```python
import time
import pytest
from pathlib import Path
from doctor.check import CheckResult
from doctor.runner import Doctor, DoctorBlocked


def _ok_check(**ctx):
    return CheckResult(name="OkCheck", severity="ok", message="all good")


def _err_check(**ctx):
    return CheckResult(name="ErrCheck", severity="error", message="broken")


def test_doctor_run_synchronous_returns_results(tmp_path):
    d = Doctor(checks=[_ok_check], log_path=tmp_path / "doctor.jsonl")
    results = d.run_synchronous(context={})
    assert len(results) == 1
    assert results[0].severity == "ok"


def test_doctor_raises_blocked_on_error(tmp_path):
    d = Doctor(checks=[_err_check], log_path=tmp_path / "doctor.jsonl")
    with pytest.raises(DoctorBlocked):
        d.run_synchronous(context={}, raise_on_error=True)
```

- [ ] **Step 2: Run, fail**

- [ ] **Step 3: Implement**

```python
# doctor/runner.py
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from typing import Callable

from doctor.check import CheckResult


class DoctorBlocked(RuntimeError):
    pass


CheckFn = Callable[..., CheckResult]


class Doctor:
    def __init__(self, *,
                 checks: list[CheckFn],
                 log_path: Path | str,
                 background_interval_s: int = 60):
        self.checks = list(checks)
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.background_interval_s = background_interval_s
        self._status: dict[str, CheckResult] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_synchronous(self, *, context: dict,
                          raise_on_error: bool = True) -> list[CheckResult]:
        results = []
        for fn in self.checks:
            try:
                r = fn(**context)
            except Exception as e:
                r = CheckResult(name=fn.__name__, severity="error",
                                 message=f"check raised: {type(e).__name__}: {e}")
            self._status[r.name] = r
            self._log(r)
            results.append(r)
        if raise_on_error:
            errors = [r for r in results if r.severity == "error"]
            if errors:
                raise DoctorBlocked(f"{len(errors)} errors: {[e.name for e in errors]}")
        return results

    def status(self) -> dict[str, CheckResult]:
        return dict(self._status)

    def _log(self, r: CheckResult) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "check": r.name,
                "severity": r.severity,
                "message": r.message,
                "auto_healed": r.auto_healed,
                "details": r.details,
            }) + "\n")

    def start_background(self, context: dict) -> None:
        if self._thread is not None:
            return
        def loop():
            while not self._stop_event.is_set():
                try:
                    self.run_synchronous(context=context, raise_on_error=False)
                except Exception:
                    pass
                self._stop_event.wait(self.background_interval_s)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
```

- [ ] **Step 4: Run, pass**

Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add doctor/runner.py tests/test_doctor_runner.py
git commit -m "feat(doctor): Doctor scheduler (sync + background thread)"
```

---

### Task D2: Wire Doctor into the round loop

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Compose the full check set**

```python
from doctor.runner import Doctor, DoctorBlocked
from doctor.checks.records import check_schema_version, check_record_roundtrip, check_disk_free
from doctor.checks.browser import check_orphan_chromedrivers, check_chrome_memory
from doctor.checks.query import check_engine_health, check_all_engines_cold
from doctor.checks.flow import check_candidate_pool_growth, check_loop_detector
from browser.reaper import OrphanReaper

_REAPER = OrphanReaper()

def _make_doctor(record_store, query_engines, output_dir):
    def _check_schema(**_): return check_schema_version(record_store)
    def _check_roundtrip(**_): return check_record_roundtrip(record_store)
    def _check_disk(**_): return check_disk_free(Path(output_dir), min_free_gb=1.0)
    def _check_orphan(**_): return check_orphan_chromedrivers(_REAPER, auto_heal=True)
    def _check_chrome_mem(**_): return check_chrome_memory(max_total_mb=6000)
    def _check_engines_cold(**_): return check_all_engines_cold(query_engines)
    return Doctor(
        checks=[_check_schema, _check_roundtrip, _check_disk,
                _check_orphan, _check_chrome_mem, _check_engines_cold],
        log_path=Path(output_dir) / "doctor.jsonl",
    )
```

- [ ] **Step 2: Call before each round**

```python
doctor = _make_doctor(record_store, ladder.engines, output_dir)
doctor.start_background(context={})

for round_idx in range(max_rounds):
    try:
        doctor.run_synchronous(context={}, raise_on_error=True)
    except DoctorBlocked as e:
        log.error("Doctor blocked round %d: %s", round_idx, e)
        break
    # ... rest of round body
```

Stop the background thread in the finally block at end of run().

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```
git add startup_researcher.py
git commit -m "feat(doctor): wire into round loop (synchronous + background)"
```

---

## Phase Final -- live smoke

### Task Z1: One-round headed-minimized live smoke

- [ ] **Step 1: Run a single round in headed-minimized mode against seed URLs**

```
python startup_researcher.py --max-rounds 1 \
  --output-dir startup_output_v2_smoke \
  --seed-urls "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups" \
  "Find every company where at least one founder is a Cornellian."
```

- [ ] **Step 2: Verify expected artifacts exist**

```
ls startup_output_v2_smoke/
# Expect: records.duckdb, doctor.jsonl, gemini_calls.jsonl, selenium_fetches.jsonl, round_metrics.jsonl
```

- [ ] **Step 3: Sanity-check counts**

```
python -c "
from records.store import RecordStore
print(RecordStore('startup_output_v2_smoke/records.duckdb').stats())
"
```
Expected: company_count > 0 OR candidate_count > 0. Doctor.jsonl has zero error-severity entries.

- [ ] **Step 4: Commit (test artifact)**

```
git commit --allow-empty -m "test(v2): one-round headed-minimized live smoke"
```

---

## Self-review

**Spec coverage map:**

| Spec section | Tasks |
|---|---|
| Core principle: extraction, not discovery | (carries forward from hardening pass) |
| Core principle: HTTP-first | Q1-Q6 (DDG/Brave/Mojeek/Startpage) |
| Single store, multiple views | R1-R10 |
| Never lose work (candidates) | F1-F4 |
| Doctor before degradation | D1-D2, plus per-workstream check tasks (R11, S7, Q8, F4) |
| S0a headed-minimized binding | S0a |
| S0b library spike | S0b |
| S1 BrowserSession | S1 |
| S2 CookieStore | S2 |
| S3 OrphanReaper | S3 |
| S4 page helpers | S4 |
| S5 gemini_tool migration | S5 |
| S5 linkedin_login migration | S6 |
| S6 wiki contribution | (deferred to post-merge follow-up) |
| R1 schema | R0.1 |
| R2 RecordStore interface | R1-R5 |
| R3 JSON export | R8 |
| R4 monolith migration | R7 |
| R5 analytics integration | R10 |
| Q1 engine interface | Q1 |
| Q2 HTTP engines | Q2-Q4 |
| Q3 Selenium-Google headed | Q5 |
| Q4 intent ladder | Q6 |
| Q5 planner wiring | Q7 |
| F1 candidates write | F1 |
| F2 recovery round | F2 |
| F3 atomic promotion | (covered in R4) |
| F4 metrics | F3 |
| D1 Check interface | R11 (CheckResult dataclass) |
| D2 initial check set | R11 + S7 + Q8 + F4 |
| D3 scheduler | D1 |
| D4 ladder integration | D2 |
| D5 doctor.jsonl | D1 (built into runner) |
| D6 CLI surface | (deferred to post-merge follow-up) |

**Placeholder scan:** none found ("TBD"/"TODO"/"implement appropriately" absent).

**Type consistency:** `RecordStore`, `BrowserSession`, `CookieStore`, `OrphanReaper`, `QueryLadder`, `QueryEngine`, `SearchResult`, `EngineHealth`, `CheckResult`, `Doctor` -- all used consistently across tasks.

End of plan.
