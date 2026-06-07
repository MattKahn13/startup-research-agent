# Research Agent v2 -- Design

**Date:** 2026-06-07
**Author:** Matt + Claude
**Spec status:** draft for user review
**Predecessor:** [2026-06-05-hardening-pass-design.md](./2026-06-05-hardening-pass-design.md)

## Goal

The hardening pass made the agent honest. The evidence-span filter and Pydantic schema cut hallucinations to zero, but the agent now produces zero records on a fresh run because the upstream layers it depends on are broken:

- Google search through Selenium hits CAPTCHA on every query in headless mode.
- Selenium itself fails in a dozen small ways across long runs: orphan chromedrivers, page-load timeouts on heavy pages, a silent cookie-filter bug, and inconsistent stealth.
- The monolithic `startups_db.json` is fragile to concurrent writes and offers no SQL-style querying.
- Records that fail evidence-span vanish into `reextract_unmatched.jsonl` with no recovery loop.

v2 fixes all four, plus adds a script-based health watchdog so failures surface in seconds instead of after a 7-hour CAPTCHA loop.

After this pass:

1. The browser layer is a small set of bullet-proof primitives, audited and published to `web-agent-skills` so other projects benefit.
2. Search is HTTP-first via a three-rung engine ladder. Selenium-Google is the last resort, used rarely.
3. Records live in DuckDB with a JSON export mirror for git diffability and human inspection.
4. Records that fail evidence-span land in a candidates pool. Subsequent rounds target those candidates with planner-generated DDG queries. Promotion is automatic when better evidence is found.
5. A `Doctor` module runs script-based checks every round and every 60 seconds, auto-heals what it can, escalates what it can't, and feeds the degradation ladder.

## Non-goals

- LLM-based health checks. The Doctor is explicitly script-based.
- Replacing Pydantic models, the two-pass extraction prompts, or the degradation ladder. All carry forward from the hardening pass.
- Building new analytics dashboards. The existing markdown reports and CSV exports stay.
- Replacing browser-Gemini with the Anthropic / Gemini API. Out of scope by user direction.
- Migrating the existing 1,389 deduped records to the new DuckDB schema. v2 starts fresh; migration is a follow-up task that reads from `startup_output_test/startups_db_deduped.json` and inserts.
- Cross-IP rotation, paid proxies, or paid CAPTCHA solvers. The three-rung search ladder removes the need.

## Core principles (carried forward + new)

From the hardening pass:

- **Extraction, not discovery.** Gemini operates on text Selenium fetched. No recall, no inference, no estimation. Evidence-span check enforces procedurally.
- **Schema-first.** Pydantic is the contract between every layer.
- **Observable.** Every Gemini call, every Selenium fetch, every DB write has a structured outcome.
- **Degradation, not stop.** The agent stays productive when extraction breaks.

New in v2:

- **HTTP-first.** Browser is the last rung, not the default. Selenium is reserved for sites that genuinely need JS rendering or auth flows that can't be done via HTTP.
- **Single store, multiple views.** DuckDB is the working store. JSON export is the audit view. CSV exports are the analytics view. They derive from the same source.
- **Never lose work.** Failed extractions become candidates. Candidates become targeted searches. Recovery is a first-class flow, not a follow-up task.
- **Doctor before degradation.** Process and session hygiene is checked before parse rate. A leaked chromedriver gets killed before it can affect the parse rate that triggers the ladder.

## Approach overview

Five workstreams. Each is independently shippable but they compose.

| Workstream | What it ships | Depends on |
|---|---|---|
| **R -- Records store** | New `records/` package: DuckDB schema, `RecordStore` interface, JSON export script, monolith migration | (foundation; lands first) |
| **S -- Selenium primitive set** | New `browser/` package: `BrowserSession`, `CookieStore`, `OrphanReaper`, primitive-level integration tests | R (so any Selenium-collected data goes to the new store) |
| **Q -- Query rung ladder** | New `query/` package: `DDGEngine`, `BraveEngine`, `MojeekEngine`, `StartpageEngine`, `SeleniumGoogleEngine`, `QueryLadder` | S (the last-rung Selenium-Google uses the new `BrowserSession`) |
| **F -- Recovery flow** | Candidates table in R, new round mode in main loop, planner prompt variant for recovery rounds | R (candidates store) + Q (recovery search) |
| **D -- Doctor** | `doctor/` package: `Check` interface, scheduled runner, `doctor.jsonl` log, hooks into the degradation ladder | (lands incrementally with R/S/Q/F; each workstream contributes its own checks) |

Landing order: **R first → S → Q → F**. R blocks everything else so that no work ever runs against the monolithic JSON DB after v2 begins. **D lands incrementally** alongside every other workstream -- each delivers its own initial check set.

## Workstream S -- Selenium primitive set

### S0a. Headed-minimized is the default

Verified by `probe_headed_minimized.py` on 2026-06-07: undetected-chromedriver running in HEADED mode with `driver.minimize_window()` and `--window-position=-10000,-10000` survives Google search (4 of 5 queries clean; the one failure was a `site:linkedin.com/in/` abuse-pattern query that ALSO fails in headless), and survives LinkedIn `/in/` profile fetches without the "lite variant throttle" we attributed yesterday to a per-session rate limit (that throttle was actually the headless penalty).

This reframes the rest of the spec. `BrowserSession` defaults to `headless=False`. Headless becomes an explicit opt-in flag for targets that have been verified not to fingerprint it. The wiki entry [[~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md]] is now the binding contract.

Implications:

- Process hygiene matters more, not less -- more visible windows means more chrome.exe processes to track. The minimize-window call has to be on the success path of every BrowserSession startup, not optional.
- The Q workstream's "Selenium-Google as last resort" framing is wrong; see Q updates below.
- The LinkedIn `/in/` retry-with-fresh-session pattern from yesterday's wiki lesson is no longer required. Just use headed-minimized and the throttle doesn't fire.

### S0b. Research mini-spike (1 day, blocking)

Before code, evaluate the base library choice. Today: `undetected-chromedriver`. Alternatives to assess:

- **Playwright + Patchright.** Modern, async, multi-browser. Patchright adds stealth on top.
- **nodriver.** The de facto successor to undetected-chromedriver from the same author. Async, no chromedriver dep at all (talks DevTools directly).
- **curl_cffi.** Pure HTTP, mimics real Chrome TLS fingerprint. No browser at all. Right answer for anything that doesn't need JS rendering.

Evaluation criteria for the browser case: stealth on Google search (still gets CAPTCHA?), stealth on LinkedIn `/in/` profiles (still gets the lite-variant throttle?), cookie store ergonomics, async story, ease of cleaning up child processes. The winner becomes the new base library; the others stay documented as fallbacks.

Output: a one-page comparison written to `docs/superpowers/specs/2026-06-07-browser-library-decision.md` and committed to `web-agent-skills/wiki/escalation-ladders/js-render.md` as a lesson. Recommendation in the spec; the actual pick may shift after the spike.

For the rest of this spec, references to `BrowserSession` are agnostic of base library; the interface is the contract.

### S1. `BrowserSession` interface

```python
class BrowserSession:
    """Wraps a single browser. Owns one process. Handles cleanup."""

    def __init__(self,
                 headless: bool = False,            # default per S0a
                 minimize: bool = True,             # only effective when headless=False
                 cookie_store: Optional["CookieStore"] = None,
                 page_load_timeout_s: int = 60,
                 script_timeout_s: int = 15):
        ...

    def __enter__(self) -> "BrowserSession":
        """Idempotent start. Registers signal handlers. Spawns reaper."""
        ...

    def __exit__(self, *exc) -> None:
        """Always quits the driver. Always reaps the profile dir. Always
        unregisters signal handlers. Safe under crash."""
        ...

    def get(self, url: str, *, wait_for_dom: bool = True,
            wait_for_stable: bool = False, stable_window_s: float = 1.0) -> None:
        """Navigate. Optionally poll for DOM-ready or text-stable."""
        ...

    def html(self) -> str: ...
    def current_url(self) -> str: ...
    def execute_script(self, js: str, *args) -> Any: ...
    def restart(self) -> None:
        """Quit + spawn new driver. Cookies survive via the cookie_store."""
        ...
```

Invariants:

- One `BrowserSession` owns one driver process and one profile dir.
- `__exit__` is bulletproof. Tests verify cleanup under SIGINT, SIGTERM, hung script, OOM kill, exception inside `with` block.
- `restart()` survives at most 3 consecutive failures before raising `BrowserUnavailable`.

### S2. `CookieStore` interface

Replaces the silently-filtered `save_cookies`/`load_cookies` from `gemini_tool.py`.

```python
class CookieStore:
    """Per-domain cookie persistence."""

    def __init__(self, path: Path, mode: int = 0o600): ...

    def save_for(self, driver, domain: str) -> int:
        """Save cookies whose domain ends with `domain`. Returns count saved."""
        ...

    def load_for(self, driver, domain: str) -> int:
        """Driver must already be on `domain` (browser requirement).
        Returns count successfully added."""
        ...

    def clear(self, domain: Optional[str] = None) -> None: ...

    def age_for(self, domain: str) -> Optional[timedelta]:
        """Used by the Doctor to flag stale sessions."""
        ...
```

Stored as JSON, one file per domain (`~/.web_agent_cookies/<domain>.json`, mode `0o600`).

### S3. Orphan reaper

`OrphanReaper` is a process-lifetime singleton that:

- Tracks every `BrowserSession`'s driver PID and profile dir.
- On exit (atexit + signal handlers for SIGINT/SIGTERM), force-kills any still-alive driver process owned by any active session, and removes its profile dir.
- Provides `kill_stale()` -- finds chromedriver / chrome processes spawned by this Python interpreter that no longer have a live `BrowserSession` and kills them. Called by the Doctor as a health-check auto-heal.

### S4. Page-stabilise helpers

Extracted from the existing `_wait_for_body_to_stabilise` in `startup_researcher.py` but generalized:

- `wait_dom_ready(driver, timeout_s)` -- waits for `readyState === complete`.
- `wait_text_stable(driver, window_s, max_wait_s)` -- waits until `document.body.innerText.length` doesn't change for `window_s` seconds.
- `wait_selector(driver, selector, timeout_s)` -- waits for a CSS selector to exist.

All three are timeout-bounded and never block indefinitely.

### S5. Migration of existing browser code

`gemini_tool.py` (~2,700 lines) and `linkedin_login.py` migrate to use `BrowserSession` + `CookieStore`. The Gemini-specific input strategies (clipboard → execCommand → send_keys) stay; they're Gemini-specific and belong in a `gemini_session.py` shim that wraps `BrowserSession`. Same for the LinkedIn-specific cookie-domain filter.

After migration, `gemini_tool.py` shrinks substantially because the cookie/driver lifecycle code moves to the primitive layer.

### S6. Wiki contribution

The `BrowserSession` + `CookieStore` + `OrphanReaper` primitives, with their tests, get published to `web-agent-skills` as:

- `wiki/primitives/browser-session.md` -- the contract and invariants.
- `wiki/primitives/cookie-store.md` -- per-domain pattern, replaces the existing `cookie-persistence.md` entry (which gets updated to point here).
- `wiki/primitives/orphan-reaper.md` -- process-hygiene pattern.
- `skills/web-agent/scripts/browser_session.py` -- reference implementation.

This is the wiki contribution the user asked for in the brainstorm.

## Workstream R -- Records store

### R1. DuckDB schema

One database file: `startup_output/records.duckdb`. DuckDB was chosen over SQLite specifically because it supports modern `ALTER TABLE` operations natively -- column renames, type changes, drops are all single statements. That matters during the build, when the schema will iterate. Native JSON column type. Single-file embedded like SQLite. Single-writer concurrency model (the `RecordStore` instance owns the write connection; reads are unrestricted).

Tables:

```sql
CREATE SEQUENCE seq_cornellians START 1;
CREATE SEQUENCE seq_query_log START 1;
CREATE SEQUENCE seq_promotion_log START 1;

CREATE TABLE companies (
    slug              VARCHAR PRIMARY KEY,        -- canonical name from _normalise_name
    company_name      VARCHAR NOT NULL,
    proof_url         VARCHAR NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'unknown',  -- enum at the Pydantic layer
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
    tags              JSON,                    -- native JSON, queryable via json_extract
    non_cornell_cofounder_schools JSON,
    validation_tier   VARCHAR NOT NULL,         -- enum at the Pydantic layer
    validation_issues JSON,
    first_seen_at     TIMESTAMP NOT NULL,
    last_verified_at  TIMESTAMP NOT NULL
);

CREATE TABLE cornellians (
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

CREATE TABLE candidates (
    slug              VARCHAR PRIMARY KEY,
    company_name      VARCHAR NOT NULL,
    last_attempted_proof_url VARCHAR,
    last_attempt_outcome VARCHAR,               -- "unmatched" | "fetch_failed" | "schema_failed"
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    first_attempt_at  TIMESTAMP NOT NULL,
    last_attempt_at   TIMESTAMP NOT NULL,
    candidate_payload JSON NOT NULL              -- what we know so far (name, partial fields)
);

CREATE TABLE query_log (
    id                BIGINT PRIMARY KEY DEFAULT nextval('seq_query_log'),
    timestamp         TIMESTAMP NOT NULL,
    engine            VARCHAR NOT NULL,
    query             VARCHAR NOT NULL,
    result_count      INTEGER NOT NULL,
    outcome           VARCHAR NOT NULL,         -- "ok" | "empty" | "rate_limited" | "captcha" | "error"
    latency_ms        INTEGER NOT NULL
);

CREATE TABLE promotion_log (
    id                BIGINT PRIMARY KEY DEFAULT nextval('seq_promotion_log'),
    timestamp         TIMESTAMP NOT NULL,
    slug              VARCHAR NOT NULL,
    direction         VARCHAR NOT NULL,         -- "promoted" | "demoted"
    reason            VARCHAR NOT NULL,
    found_via_query   VARCHAR
);

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);
INSERT INTO schema_version VALUES (1, CURRENT_TIMESTAMP);

CREATE INDEX idx_companies_tier ON companies(validation_tier);
CREATE INDEX idx_companies_founded ON companies(founded_year);
CREATE INDEX idx_companies_status ON companies(status);
CREATE INDEX idx_cornellians_company ON cornellians(company_slug);
CREATE INDEX idx_cornellians_name ON cornellians(name);
CREATE INDEX idx_candidates_last_attempt ON candidates(last_attempt_at);
CREATE INDEX idx_query_log_engine ON query_log(engine);
```

DuckDB notes vs the SQLite version this replaced:

- Native `JSON` type for `tags`, `validation_issues`, `non_cornell_cofounder_schools`, `candidate_payload`. Queryable via `json_extract(tags, '$[0]')`, `json_array_length(validation_issues)`, etc. No need to `loads()`/`dumps()` at the Python layer for these columns.
- Native `BOOLEAN` for `is_public` (vs SQLite's int-as-bool).
- Native `TIMESTAMP` for the time columns. No more text-encoded ISO strings.
- `BIGINT GENERATED ALWAYS AS IDENTITY` via sequences (DuckDB doesn't have `AUTOINCREMENT`).
- No `PRAGMA journal_mode = WAL` -- DuckDB's concurrency model is different. The `RecordStore` instance owns a single write connection; reads can be parallel via separate read-only connections.
- Foreign keys: DuckDB supports them at table-creation time; `ON DELETE CASCADE` is supported. Enforcement is enabled by default.

### R2. `RecordStore` interface

```python
class RecordStore:
    """High-level interface over records.duckdb. All methods are transactional."""

    def __init__(self, db_path: Path): ...

    def upsert_record(self, record: StartupRecord) -> str:
        """Returns 'new' | 'merged'. Inserts company + cornellians atomically."""
        ...

    def add_candidate(self, partial: dict, last_url: str,
                       outcome: str) -> str:
        """Insert or update candidates row. Returns 'new' | 'incremented'."""
        ...

    def promote_candidate(self, slug: str, record: StartupRecord) -> None:
        """Single transaction: delete from candidates, upsert to companies+cornellians."""
        ...

    def demote_record(self, slug: str, reason: str) -> None:
        """Single transaction: move company+cornellians to candidates."""
        ...

    def list_records(self, tier: Optional[str] = None,
                      founded_after: Optional[int] = None,
                      **filters) -> Iterator[StartupRecord]:
        """SQL-backed queries. Yields Pydantic models."""
        ...

    def list_candidates(self, max_attempts: int = 3,
                         exclude_recently_attempted_s: int = 3600) -> Iterator[dict]:
        """Candidates ready for a recovery round. Skips ones we just tried."""
        ...

    def log_query(self, engine: str, query: str, result_count: int,
                   outcome: str, latency_ms: int) -> None: ...

    def stats(self) -> dict:
        """Counts by tier, candidates total, query log size, etc.
        Cheap; for the Doctor."""
        ...
```

### R3. JSON export mirror

`scripts/export_records_to_json.py` reads from DuckDB and writes one JSON file per record to `startup_output/records/<slug>.json`. Plus `startup_output/candidates/<slug>.json` for candidates. Run on a schedule (cron-style, via the Doctor) or on-demand.

The JSON shape matches `StartupRecord.model_dump(mode="json")`. Git-trackable. Human-readable. Diff-friendly. The DuckDB is the source of truth; the JSON is the audit log.

### R4. Migration from monolith

One-shot script `scripts/migrate_monolith_to_duckdb.py`:

1. Reads `startup_output_test/startups_db_deduped.json` (the 1,389-record deduped DB from yesterday's overnight work).
2. Iterates each record, validates against `StartupRecord` (re-running the schema check).
3. `RecordStore.upsert_record(rec)` for each. Skips records that fail Pydantic validation; logs to `migrate_skipped.jsonl`.
4. Prints summary: inserted, skipped, by-reason.

Idempotent. Run once at the start of v2 to seed the new DuckDB from yesterday's work.

### R5. Analytics integration

The existing `analyze_ecosystem.py`, `export_csv.py`, `export_network.py` migrate to use `RecordStore` instead of loading JSON files. Most queries become one-liners. `analyze_ecosystem.py` shrinks from ~250 lines to ~80. Network export shrinks similarly.

The migrated scripts ship in this workstream so analytics never breaks during the DuckDB cutover.

## Workstream Q -- Query rung ladder

### Q1. `QueryEngine` interface

```python
class QueryEngine:
    name: str

    def search(self, query: str, *, n_results: int = 10) -> list[SearchResult]:
        """Returns list of SearchResult (url, title, snippet).
        Raises EngineRateLimited / EngineCaptcha / EngineEmpty as appropriate."""
        ...

    def health(self) -> EngineHealth:
        """Recent success rate, recent latency p50/p95, last_error.
        For the Doctor."""
        ...
```

`SearchResult`: `url: str`, `title: str`, `snippet: str`, `engine: str`.

### Q2. HTTP engines (rung 1 and 2)

- **`DDGEngine`** -- `urllib` POST to `https://html.duckduckgo.com/html/` with form-encoded query. Parse the result page for `<a class="result__a" href="...">`. Lite fallback: `https://lite.duckduckgo.com/lite/`. No JS, no auth, no cookies.
- **`BraveEngine`** -- HTTP GET to `https://search.brave.com/search?q=<query>`. Parse result HTML.
- **`MojeekEngine`** -- HTTP GET to `https://www.mojeek.com/search?q=<query>`. Parse result HTML.
- **`StartpageEngine`** -- HTTP GET. Slightly trickier because Startpage requires a session cookie, but it's just one GET to get the cookie then one GET to search.

All four use the `curl_cffi` library if S0 picks it, otherwise `urllib` with a realistic Chrome User-Agent header. No browser.

Per-engine state: cooldown timer if the engine rate-limits or 429s, exponential backoff with jitter via `retry_policy.py` (from the hardening pass).

### Q3. Selenium-Google engine (primary for quality queries, not last-resort)

`SeleniumGoogleEngine` wraps Google search via `BrowserSession` (headed-minimized per S0a). Verified to work on routine queries -- the only failure mode in the 2026-06-07 probe was a `site:linkedin.com/in/` abuse-pattern query that fails in both headed and headless. CAPTCHA handling on the rare query that does trip detection: log the query as `outcome=captcha` and skip; if 3 consecutive queries hit CAPTCHA, the engine cools down for an hour and the ladder routes around it.

Per-query overhead: ~3 seconds with Chrome startup amortized across a session. Acceptable for the per-round planner volume (10-30 queries) but slow for high-volume parallel lookups.

### Q4. `QueryLadder` orchestrator -- engine selection by query intent, not fallback

```python
class QueryLadder:
    def __init__(self, engines: dict[str, QueryEngine], record_store: RecordStore): ...

    def search(self, query: str, *, intent: str = "quality",
                n_results: int = 10) -> list[SearchResult]:
        """Routes to the right engine for the intent. Engines are not a strict
        fallback chain anymore -- they have different strengths.

        intent='quality': Selenium-Google headed. Best results for niche queries.
                          Default for planner-generated research queries.
        intent='speed':   DDGEngine (HTTP). Fast, parallelizable. Default for
                          high-volume slug-discovery / verification lookups.
        intent='diverse': Try DDG + Brave + Mojeek + Startpage in parallel, merge
                          + dedupe results. For broad discovery.
        """
        ...
```

The 2026-06-07 probe inverted the old "fallback chain" framing. Selenium-Google headed is reliable for quality queries; the HTTP engines are reliable for high-volume cheap lookups. They serve different intents.

When an engine fails for its intent, the ladder degrades to its next-best alternative for the same intent (Selenium-Google → DDG for quality; DDG → Brave → Mojeek for speed).

### Q5. Wiring into the planner

The existing `plan_research()` flow generates queries via the planner Gemini call. Today those queries hit Selenium-Google. v2: they hit `QueryLadder.search(query)`. Drop-in replacement at one call site.

The planner prompt stays mostly the same; we add a hint that DDG-style queries (no `site:` operator dependency, smaller quote groups) tend to work better than Google-style. Per-engine query-mangling is the ladder's job, not the planner's.

## Workstream F -- Recovery flow

### F1. Candidates write path

`extract_from_page()` (from the hardening pass) returns either a `StartupRecord` that survives evidence-span, or nothing. v2 change: when it returns nothing AND we have a partial extraction (Gemini did extract a record but no cornellian's evidence_span verified), the partial gets written to `candidates` table via `RecordStore.add_candidate()`.

The candidate payload includes: company_name (the Gemini-extracted name), the attempted proof_url, the list of cornellians Gemini proposed (with their unverifiable spans), the page text size, and the attempt_count from any prior candidate row.

A page processed by the round loop now has three possible outcomes per company:
- Verified record (evidence-span passed) -- to `companies` + `cornellians` tables.
- New candidate (evidence-span failed) -- to `candidates` table with `attempt_count=1`.
- Existing candidate, another failure -- `attempt_count += 1`, `last_attempt_at = now()`.

When `attempt_count` exceeds a threshold (default 5), the candidate is marked `parked` and the recovery loop skips it. Manual intervention required to unpark.

### F2. Recovery round mode

A new round flavor in the main loop. Every Nth round (default 3), the round is a "recovery round":

1. `RecordStore.list_candidates(max_attempts=5, exclude_recently_attempted_s=3600)` -- pull eligible candidates.
2. Group candidates by likely search strategy (e.g., "founder by name" vs "company by domain").
3. Build a planner prompt that includes the candidates list and asks for one targeted search query per candidate -- format: `<company name> founder cornell`, `site:wikipedia.org "<company name>"`, `<founder name> cornell linkedin`, etc.
4. Run those queries through `QueryLadder`.
5. For each result URL, scrape with the existing scraping path, extract via `extract_from_page()`, attempt promotion via `RecordStore.promote_candidate()` if the canonical name matches a candidate and evidence-span now passes.
6. Increment `attempt_count` on candidates that still don't promote.

Recovery rounds slot into the existing round loop; the degradation ladder applies normally.

### F3. Promotion logic

`RecordStore.promote_candidate(slug, record)` runs as a single DuckDB transaction:

1. `DELETE FROM candidates WHERE slug = ?`
2. `INSERT INTO companies (...) VALUES (...)`
3. `INSERT INTO cornellians (...) VALUES (...)` for each affiliation.
4. Append a row to `promotion_log` for audit.

Atomic. Either the candidate moves fully or nothing changes.

### F4. Surface in metrics

`round_metrics.jsonl` gets two new fields per round: `candidates_promoted`, `candidates_added`, `candidates_total`. The end-of-round summary prints: `Round 14: ... candidates: 5 promoted, 12 added, 387 total.`

## Workstream D -- Doctor

### D1. `Check` interface

```python
@dataclass
class CheckResult:
    name: str
    severity: Literal["ok", "warn", "error"]
    message: str
    auto_healed: bool = False
    details: dict = field(default_factory=dict)


class Check:
    name: str
    severity_on_fail: Literal["warn", "error"]
    auto_heal_safe: bool   # True if auto_heal() is side-effect-bounded

    def verify(self, ctx: "DoctorContext") -> CheckResult: ...
    def auto_heal(self, ctx: "DoctorContext") -> None: ...  # optional override
```

### D2. Initial check set

Each workstream contributes its checks:

From **S (Selenium primitives):**
- `OrphanChromedriverCheck` -- count of unowned chromedriver processes > N → kill them (auto-heal).
- `ChromeMemoryCheck` -- total Chrome RSS above threshold → warn.
- `StaleProfileDirCheck` -- ephemeral profile dirs older than 1h with no live session → delete.

From **R (records store):**
- `SchemaVersionCheck` -- `schema_version` table matches expected version → if not, refuse to continue (error).
- `RecordRoundTripCheck` -- sample one record, validate against Pydantic → on failure, quarantine the row.
- `DiskFreeCheck` -- free space on the records.duckdb drive > threshold → warn.

From **Q (query ladder):**
- `EngineHealthCheck` -- per engine, success rate over last 50 calls > threshold → warn if any engine is degraded.
- `AllEnginesColdCheck` -- if every engine is cooled-down or broken → error.

From **F (recovery flow):**
- `CandidatePoolGrowthCheck` -- candidates added per round trending up while promotions trending down → warn (signal: source quality dropping).
- `LoopDetectorCheck` -- same record upserted N times in a single round → quarantine and warn.

Process-wide:
- `GeminiCookieAgeCheck` -- `CookieStore.age_for("google.com")` > 7d → warn.
- `LinkedInCookieAgeCheck` -- > 14d or missing `li_at` → error (requires re-login).
- `LastGeminiCallCheck` -- last successful call in `gemini_calls.jsonl` > 30 min ago AND main loop active → warn.

### D3. Scheduler

`Doctor` runs in two modes:

- **Synchronous** -- called once at start of each round, before any work. Blocking. If any `error`-severity check fails, refuses to enter the round (raises `DoctorBlocked`). The main loop catches and decides whether to retry, escalate, or stop.
- **Background** -- a daemon thread that runs all checks every 60s, logs to `doctor.jsonl`, and updates a shared `doctor_status` dict the main loop and the ladder can read.

### D4. Integration with the degradation ladder

The ladder from the hardening pass already demotes based on Gemini parse rate and Selenium failure rate. v2 adds Doctor signals:

- 3 consecutive `warn` checks on flow-health → demote one level.
- 1 `error` check → demote two levels.
- All checks `ok` for 5 consecutive minutes → eligible for promotion.

The ladder remains the authority; the Doctor is a sensor.

### D5. `doctor.jsonl` log

One JSON line per check execution:

```json
{"ts": "2026-06-08T03:14:22Z", "check": "OrphanChromedriverCheck",
 "severity": "warn", "message": "3 orphan chromedrivers",
 "auto_healed": true, "details": {"killed_pids": [1234, 5678, 9012]}}
```

Rotated by line count (default 50k). The Doctor itself rotates its own log via a meta-check.

### D6. CLI surface

```bash
# Run checks once, print results, exit
python -m doctor check

# Run a specific check
python -m doctor check OrphanChromedriverCheck

# Print current status (last run of each check)
python -m doctor status
```

Useful for human inspection without booting the whole agent.

## Data flow (end-to-end)

Round start:
```
Doctor.run_synchronous()
  ↓  (raises DoctorBlocked on red)
ladder.tick()
  ↓
if backlog/scrape-only/hard-stop: handle and continue/break
  ↓
if recovery round:
    candidates = record_store.list_candidates(...)
    planner_prompt(recovery_mode=True, candidates=candidates)
else:
    gap_report = record_store.stats() + analysis
    planner_prompt(recovery_mode=False, gaps=gap_report)
  ↓
search_strategies = call_gemini(planner_prompt) → SearchStrategy
  ↓
for query in strategies.queries:
    results = query_ladder.search(query)   # DDG → Brave → ... → Selenium-Google
    record_store.log_query(...)
    ↓
    for r in results:
        text = scrape_page(r.url)          # BrowserSession + cache + retry
        records = extract_from_page(text, r.url)
        for rec in records:
            if rec.cornellians:            # evidence-span passed
                record_store.upsert_record(rec)
            else:
                record_store.add_candidate(rec, r.url, "unmatched")
  ↓
round_metrics.append(summary)
checkpoint.save()
```

Background thread (always running):
```
every 60s:
    Doctor.run_background()
        for check in checks:
            result = check.verify(ctx)
            if result.severity != ok and check.auto_heal_safe:
                check.auto_heal(ctx)
            doctor_jsonl.append(result)
            doctor_status[result.name] = result
```

## Error handling

Tier table updates from the hardening pass to include v2-specific errors:

| Tier | Examples | Action |
|---|---|---|
| Retryable (per-engine) | DDG returns empty, Brave returns 503 | `QueryLadder` advances to next engine; engine cools down briefly |
| Retryable (per-fetch) | Selenium timeout on a single page, single 5xx from a page | `retry_policy.retry()` with backoff |
| Skippable | Schema-invalid extraction, prompt-echo, marker missing, fail evidence-span | Log + add to candidates pool. Continue. |
| Fatal (browser) | `BrowserUnavailable` after restart attempts | Trip ladder to BACKLOG; Doctor surfaces. |
| Fatal (session) | LinkedIn `li_at` expired, Gemini login required | Doctor escalates to user via clear error; main loop pauses. |
| Fatal (DB) | DuckDB schema-version mismatch, corrupted DB | Doctor blocks the round; user runs migration script. |
| Fatal (process) | Every search engine cooled-down, no path forward | Ladder to HARD_STOP. |

## Testing

Existing test suite carries forward. v2 adds:

### Unit (no network, no browser)
- `RecordStore.upsert_record` -- insert / merge / cornellian union / conflict log
- `RecordStore.promote_candidate` -- atomic move; verify both tables post-transaction
- `RecordStore.list_records` -- filter combinations
- `RecordStore.add_candidate` -- new / increment / parked threshold
- Each `Check`'s `verify()` and `auto_heal()` with mocked context
- `QueryLadder.search` -- engine ordering, cooldown, fallback (with mocked engines)
- `DDGEngine` -- HTML parsing against canned response fixtures
- `BrowserSession.__exit__` -- cleanup under exception, SIGTERM (using `signal.raise_signal` in a child process)
- `OrphanReaper.kill_stale` -- finds and kills only orphan processes

### Integration (network OK, no browser)
- `DDGEngine.search` against live `html.duckduckgo.com` for a known query.
- `MojeekEngine.search` live.
- `BraveEngine.search` live.

### Live (browser, manual)
- `BrowserSession` against live Selenium-Google search
- LinkedIn enrichment via auth-mode `BrowserSession`
- Migration script against the actual deduped DB

### Doctor self-test
- Spawn 3 orphan chromedrivers (subprocess + immediate abandon), run `OrphanChromedriverCheck.auto_heal`, verify they're gone.
- Same for stale profile dirs.

## Migration from v1

One-shot at v2 start. Two scripts:

1. `scripts/init_v2_layout.py` -- creates `startup_output/records.duckdb` from schema.sql, creates `records/` and `candidates/` dirs.
2. `scripts/migrate_monolith_to_duckdb.py` -- reads `startup_output_test/startups_db_deduped.json`, inserts 1,389 records.

After both run, v2 is fully seeded. v1 artifacts (the monolith DB, the old logs) stay on disk untouched as backup.

## What we are NOT changing

- The Pydantic models (`StartupRecord`, `CornellianAffiliation`, etc.) -- they carry forward unchanged.
- The two-pass extraction prompts.
- The evidence-span check.
- The degradation ladder's state machine.
- `_normalise_name` and the canonical-name dedup rule.
- The CSV / markdown report output shapes.
- The Cornell-specific blocklist and source tiers.
- The perpetual run model with checkpoint-driven resume.

## Wiki references

Cited rather than re-derived:

- [[~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md]] -- the four LinkedIn lessons. Workstream S's `BrowserSession` adopts the auth-mode JSON parser pattern; `CookieStore` adopts the per-domain pattern.
- [[~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md]] -- the 50KB cliff and anonymous-mode lessons. Workstream S's `BrowserSession` enforces page-load timeouts at 120s for heavy pages; `gemini_session.py` shim preserves the existing input strategies.
- [[~/.claude/web-agent-skills/wiki/primitives/cookie-persistence.md]] -- replaced by the new `CookieStore` primitive doc in S6.
- [[~/.claude/web-agent-skills/wiki/primitives/resume-checkpoint.md]] -- "per-unit JSON files" -- we adopt the spirit (atomic per-record writes) via DuckDB transactions plus a JSON export mirror.
- [[~/.claude/web-agent-skills/wiki/primitives/shared-browser-session.md]] -- the existing `BrowserSession` primitive is updated to match the new contract.
- [[~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md]] -- the cookie-filter bug from yesterday is the canonical example; `CookieStore`'s explicit return-count signature prevents the same pattern.
- [[~/.claude/web-agent-skills/wiki/anti-patterns/infinite-retry.md]] -- `retry_policy.py` (from hardening pass) is used by `QueryLadder` for per-engine retries.
- [[~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md]] -- `SeleniumGoogleEngine` defaults to visible+minimized when interactive, headless only when forced (env var or CLI flag).
- [[~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md]] -- `SeleniumGoogleEngine` adopts the pattern when interactive.
- [[~/.claude/web-agent-skills/wiki/primitives/polite-jitter.md]] -- `QueryLadder` per-engine cooldown uses jittered intervals.

## Open questions

1. **Browser library final choice.** Decided after S0 spike. Spec is library-agnostic at the interface level.
2. **DDG result-count target.** Default n_results = 10 per query. May need tuning per-engine after Q lands.
3. **Recovery round cadence.** Default every 3rd round. May want to make it adaptive (more recovery rounds when the candidates pool is growing faster than promotions).
4. **Candidate parking threshold.** Default 5 failed attempts. Easy to tune in code; not load-bearing.
5. **Doctor check frequency.** 60s background interval. Trade-off between detection speed and overhead. May tune after live data.

## Success criteria

A v2 run on the seed URLs (the existing eship + bigredai default) should:

- Complete 10 rounds without any human intervention (no CAPTCHA, no re-login).
- Produce at least 50 records that pass evidence-span (vs. 0 in v1 with the same seeds).
- Generate at least 200 candidates in the candidates pool.
- Run at least 2 recovery rounds, each promoting ≥1 candidate.
- Emit `doctor.jsonl` entries for every check; zero `error`-severity entries.
- Use `SeleniumGoogleEngine` at most twice across the 10 rounds (the other 98%+ of queries served by HTTP engines).
- All existing tests still pass; new test count > 50.

Spec end.
