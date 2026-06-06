# Startup Research Agent -- Hardening Pass

**Date:** 2026-06-05
**Author:** Matt + Claude (brainstorming session)
**Scope:** reliability, observability, and output-quality improvements to the existing browser-Gemini pipeline. Does NOT migrate to the Anthropic/Gemini API, does NOT generalize beyond Cornell, does NOT add paid data sources.

## Goal

The agent works -- 1,357 records produced -- but it fails silently in ways that burn quota, hide DOM regressions until they have caused three days of broken runs, and accept half-correct extractions into the database. This pass closes those gaps without changing the core architecture (Selenium + browser-Gemini + JSON DB).

After this work, three things should be true:

1. Every Gemini call has a recorded outcome (parsed / fence-extracted / marker-sliced / prompt-echoed / empty / timeout / crash) and we can compute parse-rate, latency, and prompt-echo-rate per round.
2. Every extracted record is validated against an explicit schema at extraction time, not silently coerced at validate time. Records that fail the schema get re-extracted or rejected -- never half-merged.
3. The agent stops itself when extraction is clearly broken (parse rate below 50% over the last 20 calls) instead of grinding through 6 hours of empty rounds.

## Core principle: extraction, not discovery

Gemini is treated as a structured-output parser, not a researcher. Every Gemini call must operate on text that Selenium has already fetched. Prompts never ask Gemini to "find" or "recall" anything from its training data -- only to read the supplied text and emit fields it sees there.

Why: Gemini's discovery mode hallucinates plausible-looking founders, funding amounts, and exit dates that don't exist in the source. The agent has shipped records of this kind into the production DB, and the validation tier can't distinguish them from real ones. The fix is procedural, not prompt-engineering: never give Gemini the opportunity.

Operationally this means:

- Every extraction prompt opens with: "From the text below, extract X. If a field is not stated in the text, return null. Do not infer, recall, or estimate." That instruction is the contract.
- Field values that arrive without a corresponding source span in the text become validation errors, not warnings. The model returns the source span alongside each non-null value (`founders_evidence_span: "...Sandy Weill (Cornell '55), founder of..."`) and the parser checks the span is actually a substring of the input.
- Discovery work -- "find more pages about Citigroup's founders" -- is Selenium's job. Gemini sees the resulting page text and extracts; it doesn't propose new URLs.
- The one explicit exception is the planner Gemini call, which is asked to propose Google queries given the gap report. Even there, it operates on the gap report as input text, not on its general knowledge.

This principle reshapes the prompts in workstream A3 and the contingent-question logic in A5.

## Non-goals

- Replacing browser-Gemini with the API. Out of scope by user direction.
- Generalizing to non-Cornell institutions. Premature.
- LinkedIn scraping at scale or paid Crunchbase. Cost.
- Splitting the 3K-line files into modules. Useful but not the bottleneck; can come later.
- Re-extracting the existing 1,357 records as a backfill. Separate task; this pass only fixes forward.

## Approach overview

Four workstreams, landing in this order:

1. **Observability + degradation ladder (B).** Wrap Gemini calls and Selenium fetches in a context manager that records structured outcomes. Add a per-round metrics summary. Replace the circuit-breaker concept with a 5-level degradation ladder that shifts the agent into progressively cheaper work when extraction quality drops, instead of stopping. Extend the checkpoint to include `page_cache` keys and `visited_urls` so resume is real.
2. **Schema-first extraction (A).** Define Pydantic models for the four contract surfaces between the orchestrator and Gemini: `StartupRecord`, `ExtractionResult`, `SearchStrategy`, `GapItem`. Every extraction prompt declares the schema; every parse runs `model_validate_json` and routes failure explicitly. Prompts are assembled contingently -- fields whose preconditions are not met are not asked. The DB upsert merges via model-aware logic, not last-write-wins.
3. **Expanded schema (C).** Nine new columns plus the structured affiliation fields, folded into the Pydantic models above. Covers company status / exit, cornellian-network density, canonical URLs, and tags.
4. **Backfill (D).** One-shot script that re-extracts every existing record against the new schema using its `proof_url`. Runs after A, B, C land. Output goes to a sibling DB file; manual swap when satisfied.

B lands first so we can measure whether A actually helped. A and C land together because A's models are the place where the new columns live. D runs last, against the hardened pipeline.

---

## Workstream B -- Observability and failure policy

### B1. Structured outcome logging

Today: every Gemini call returns a string. Failures are caught broadly, logged as text, and replaced with `""`. We have no way to ask "what fraction of last hour's extraction calls actually parsed?"

After: a single `gemini_call(prompt, label)` context manager that records, for each call:

| Field | Example |
|---|---|
| `timestamp` | `2026-06-05T14:22:11Z` |
| `label` | `extract_chunk`, `plan_strategies`, `verify_records`, `fill_company` |
| `prompt_hash` | first 8 chars of sha256 |
| `prompt_chars` | 34281 |
| `response_chars` | 12044 |
| `latency_ms` | 31200 |
| `outcome` | enum: `parsed | fence_extracted | marker_sliced | prompt_echoed | empty | timeout | crash` |
| `error` | text, only if outcome != parsed |
| `extractor_strategy` | which JS strategy returned the text (0..9) |

Written as one JSON line per call to `startup_output/gemini_calls.jsonl`. Append-only. Survives restarts.

The same wrapper goes around Selenium page fetches with a smaller schema (`url`, `path` (http|selenium), `latency_ms`, `outcome`, `chars`).

### B2. Per-round metrics summary

End of every round, print and persist to `startup_output/round_metrics.jsonl`:

```
Round 14: 38 Gemini calls, 31 parsed (82%), 4 prompt-echoed, 2 empty, 1 crash.
            Avg latency 24s. New records: 47. Merged: 12. Rejected by schema: 8.
            Selenium: 142 fetches, 6 empty (4.2%), avg 3.1s.
```

These two files are enough to graph pass-rate-over-time later if we want.

### B3. Degradation ladder

When extraction quality drops, the agent demotes through progressively cheaper modes of useful work instead of stopping. Promotions back up the ladder happen automatically when the relevant pass rate recovers.

| Level | State | Trigger to enter | Promotion to L-1 |
|---|---|---|---|
| 1 | Normal: plan, search, scrape, extract, validate, merge. | -- | -- |
| 2 | Demoted extraction: chunks shrink from 30K to 15K chars, prompt uses the minimal-schema variant (`company_name`, `cornellian_founder`, `proof_url` only); the targeted-fill pass handles the rest later. | Full-prompt parse rate below 70% over last 20 calls. | 10 consecutive full-prompt parses succeed (sampled once per round). |
| 3 | Extraction paused, scraping continues: Selenium workers keep fetching pages from search results and caching them. Cache pre-warms for when extraction recovers. | Level-2 parse rate below 50% over last 20 calls, OR 5 consecutive `prompt_echoed` outcomes. | Level-2 sample call succeeds twice in a row. |
| 4 | Backlog mode: zero Gemini, zero Selenium. Local-CPU work on the existing DB -- re-run `validate_record()` on every record, recompute gap report, dedupe pass, write `health_report.json` flagging records that need re-extraction. | Selenium fetch failure rate above 50% over last 20 fetches (CAPTCHA wall or WAF block). | Level-3 sample fetch succeeds. |
| 5 | Hard stop: print warning, save state, exit. | Levels 3-4 have run for 60 minutes with no successful promotion. | (Manual only.) |

Each demotion logs why (`Round 14: extraction degraded to level 2, parse rate 64% over 20 calls`). Each promotion does the same. The user can read the metrics file to see the gear-shifting history.

The point is that an extraction outage is no longer a wasted run -- backlog mode produces a cleaner database than we started with, and scraping mode pre-warms the cache for recovery.

### B4. Real resume

Today: `--resume` restores round number, used queries, dry-run count. It does NOT restore the page cache index or the visited-URLs set, so resumed runs re-fetch pages and can re-process URLs already done.

After: checkpoint additionally persists `visited_urls` (set of canonical URLs already extracted from) and a `cache_manifest` (list of cache file hashes present at checkpoint time). On resume, both are reloaded before the loop starts.

### B5. Worker safety

Today: parallel scrape workers can die without putting their sentinel `None` on the queue, hanging the main thread on `get(timeout=120)`.

After: every worker target wrapped in `try/finally` that guarantees the sentinel. Switch to `concurrent.futures.ThreadPoolExecutor` if the wrap proves awkward.

---

## Workstream A -- Schema-first extraction

### A1. Pydantic models

Five models live in a new `schema.py`:

**`CornellianAffiliation`** -- one Cornellian's relationship to one company.

```
name: str
school: Literal["CU", "Cornell Tech", "Weill", "Vet", "unknown"]
role: Literal["alumnus", "faculty", "student", "postdoc", "researcher"]
grad_year: int | None
role_at_company: Literal["founder", "cofounder", "ceo", "cto", "early_employee", "board", "investor", "advisor"]
evidence_span: str   # the substring of source text supporting this record
source_url: str
```

**`StartupRecord`** -- one company.

Required: `company_name`, `cornellians: list[CornellianAffiliation]` (must be non-empty), `proof_url`.

Optional scalar fields (existing):
`description`, `industry`, `funding_total_usd: int | None`, `funding_stage`, `funding_last_round_year: int | None`, `founded_year: int | None`, `employee_count: int | None`, `is_public: bool | None`, `headquarters: str | None`.

Optional new fields (workstream C):
- `status: Literal["active","acquired","shutdown","ipo","unknown"]` -- default `unknown`.
- `exit_year: int | None`
- `acquirer: str | None`
- `acquisition_amount_usd: int | None`
- `website_url: str | None`
- `linkedin_company_url: str | None`
- `crunchbase_url: str | None`
- `tags: list[str]` -- short classifier tags, e.g. `["AI/ML", "B2B SaaS"]`.
- `non_cornell_cofounder_schools: list[str]` -- e.g. `["Stanford", "MIT"]`.

Hygiene fields:
- `first_seen_at: datetime`
- `last_verified_at: datetime`
- `validation_tier: Literal["high","provisional","weak"]`
- `validation_issues: list[str]`

Coercers (the existing `_coerce_funding_amount`, `_coerce_year`, etc.) become `@field_validator` methods on the model. They stop being silently-applied post-extraction transforms and become first-class validation that either succeeds or raises.

The legacy `cornellian_founder: str` and `affiliation_evidence: str` are not on the new model. The backfill (workstream D) writes the new shape; the old DB stays untouched until the swap.

**`ExtractionResult`** -- what one extraction call returns. `records: list[StartupRecord]`, `notes: str` (Gemini can flag uncertainty here). Prompts explicitly request this shape; parsing accepts only this shape.

**`SearchStrategy`** -- what the planner returns. `name: str`, `rationale: str`, `queries: list[str]`. The planner JSON-quote bug documented in HANDOFF.md section 1 gets caught at parse time instead of silently falling back to a hard-coded plan.

**`GapItem`** -- one row of the gap report. `record_id: str`, `missing_fields: list[str]`, `validation_tier: Literal["high","provisional","weak"]`, `suggested_action: str`.

### A2. Schema-aware parsing

`_parse_json(text, model_cls, fallback=None)` replaces the current heuristic-soup. Order of operations:

1. Marker-slice (existing behavior, preserved).
2. Extract fenced ```json block (existing behavior, preserved).
3. `model_cls.model_validate_json(cleaned)` -- this either succeeds with a typed object or raises `ValidationError`.
4. On `ValidationError`, log the structured error (which field, what was wrong) and return `fallback`. The outcome recorded by B1 becomes `schema_invalid` rather than the generic "parsed but useless" we have today.

Critically: `extract_startups_chunk` now returns `ExtractionResult | None`, not `list[dict]`. The caller branches on `None`. There is no silent half-record path anymore.

### A3. Prompt update

Every extraction prompt gets the model's JSON schema appended verbatim (`StartupRecord.model_json_schema()` rendered into the prompt). This is two paragraphs of explicit field-by-field instructions Gemini can follow, replacing the current hand-maintained "FIELDS:" block that drifts from the code.

The schema is the contract. The prompt header is the extraction-not-discovery contract from the Core Principle section:

> Read the text below. For each company mentioned, return the fields the schema requires. For every non-null value you return, include the substring of the input text that supports it in `*_evidence_span`. If a field is not stated in the text, return null. Do not infer, recall, or estimate. Do not use knowledge from outside the supplied text.

### A4. Evidence-span validation

Every non-null field comes back with a corresponding `*_evidence_span` string. The parser checks that span is a substring of the original input text (after light normalization: collapse whitespace, lowercase). Spans that don't match are dropped, the field reverts to null, and the outcome is recorded as `evidence_unverified` in the metrics file.

This is the procedural defense against hallucination. If Gemini invents "Sandy Weill (Cornell '55)" but the source page never mentioned Cornell, the evidence span will not match, the affiliation is rejected, and the record either falls to a lower validation tier or is dropped entirely.

### A5. Contingent prompt assembly

Prompts are templated, not static. Fields whose preconditions can be evaluated without Gemini are omitted from the prompt to reduce token spend and reduce the surface for hallucination:

| Field | Asked only when |
|---|---|
| `exit_year`, `acquirer`, `acquisition_amount_usd` | `status` resolves to `acquired` (from a first lightweight pass, or already-known) |
| `funding_stage`, `funding_last_round_year` | `funding_total_usd` is non-null OR explicitly mentioned in the text |
| `linkedin_company_url` | `website_url` was found (LinkedIn URLs are searched-for during the cross-ref pass, not extracted here) |
| `crunchbase_url` | same -- found during cross-ref, not free-text extraction |
| `non_cornell_cofounder_schools` | the record has more than one founder |

Implementation: extraction runs in two micro-passes per page.

**Pass 1: identify and classify.** Prompt asks only for `company_name`, `cornellians[]`, `proof_url`, `status` (with the closed enum + "if unstated, return `unknown`"), `funding_total_usd` (numeric or null), `founded_year`. ~6 fields. Short prompt, fast call, easy to parse.

**Pass 2: conditional fill.** For each record from pass 1, assemble a follow-up prompt containing only the fields whose preconditions are met. If `status=acquired`, ask exit details. If `funding_total_usd > 0`, ask funding stage. If the founder count > 1, ask other schools. If `status=unknown` and no funding signal, skip pass 2 entirely -- the record is sparse but consistent.

This cuts average tokens-per-page significantly, gives Gemini a smaller blast radius for hallucination per call, and produces visibly cheaper degraded-mode operation in workstream B's level 2.

### A4. DB merge becomes model-aware

`StartupDB.upsert(new: StartupRecord)`:

- List fields (`founders`, `validation_issues`, future `evidence`): union and dedupe.
- Scalar fields: keep existing if present, fill from new if missing. (Same as today, but now type-safe.)
- Conflicting scalars (e.g. two sources disagree on `funding_total_usd`): keep the value from the higher-credibility source. Both values logged to `merge_conflicts.jsonl` for human review.

Re-validates after merge and updates `validation_tier` accordingly. Fixes the "stale tier after fill_missing_data" bug.

---

## Workstream C -- Expanded schema (folded into A's models)

The nine new columns are listed in A1 and consumed by the contingent prompts in A5. This workstream is mostly schema authoring + prompt updates + DB migration glue. It does not introduce new Gemini call shapes beyond what A5 already defines.

### C1. Structured cornellians list

The single `cornellian_founder: str` field is replaced by `cornellians: list[CornellianAffiliation]` (model in A1). A company with three Cornellians as cofounders now has three entries with structured `school`, `role`, `grad_year`, `role_at_company`, and per-entry `evidence_span`.

This is the biggest schema departure. It answers "which Cornell startups have the most Cornell density?" and "how often do CU and Cornell Tech alumni co-found?" -- queries that today require regex on the free-text affiliation string.

### C2. Status and exit fields

`status`, `exit_year`, `acquirer`, `acquisition_amount_usd` -- gated by the contingent prompt logic in A5 so the agent doesn't ask about exits for companies with no funding signal.

### C3. Canonical URLs

`website_url`, `linkedin_company_url`, `crunchbase_url` -- powers the deferred employee-localization follow-up pipeline noted in `cornell-startups-tasks.md`. These are extracted from page text when present; they are not looked-up via separate searches in this pass (that would be discovery -- out of scope).

### C4. Tags and cofounder schools

`tags: list[str]` and `non_cornell_cofounder_schools: list[str]`. Both small, both useful for downstream filtering and ecosystem analysis.

### C5. URL canonicalization helper

Pure-function helper (`canonicalize_url(url) -> str`) strips tracking params and normalizes encoding. Applied to `proof_url`, `website_url`, `linkedin_company_url`, `crunchbase_url` on every write. Lets dedup treat `?utm_source=foo` variants as the same URL.

## Workstream D -- Backfill of existing 1,357 records

After A, B, C land, run a one-shot script that re-extracts every existing record against the new schema using its stored `proof_url`.

### D1. Script: `reextract_all.py`

Reads `startups_db.json`, iterates records, performs:

1. Fetch `proof_url` (cache hit if present from production runs).
2. Run the same Pass-1 + contingent Pass-2 extraction from A5 on the page text.
3. Match the extracted record back to the original by normalized `company_name`.
4. Write the new-schema record to `startups_db_v2.json`.
5. Resume-safe: skip records already present in v2.

### D2. Failure modes and outputs

| Outcome | Action |
|---|---|
| Page fetch fails (404, gone, blocked) | Log to `reextract_failed_fetch.jsonl`. Skip. Original record stays in old DB. |
| Page fetched but company not mentioned anymore | Log to `reextract_unmatched.jsonl`. Skip. Likely a stale source. |
| Page fetched, new record passes schema | Write to v2. |
| Page fetched, new record fails schema | Log full Gemini reply to `reextract_schema_fail.jsonl`. Skip. |

### D3. Expected scale

1,357 records × ~24s/call average = ~9 hours sequential or ~4.5 hours with 2 workers. The page fetch step will hit the cache for most production-DB records, so most of the time is Gemini latency. Expect ~10-20% to fall into one of the failure buckets above. The successful re-extractions will populate the new fields (status, exit, urls, tags) from the same source pages -- whatever wasn't extracted the first time gets a second pass with the better schema.

### D4. Swap

After the backfill finishes and `reextract_failed_fetch.jsonl` and friends have been reviewed, swap `startups_db.json` ← `startups_db_v2.json` manually. Old file moves to `startups_db.bak.pre-backfill.json`. The agent resumes against the new DB on the next run.

---

## Data flow after the changes

```
Round start
  v
Gap analysis (reads DB, emits list[GapItem]; validated by Pydantic)
  v
Planner Gemini call (returns SearchStrategy; gemini_call records outcome)
  v
Google search (Selenium; per-fetch outcome recorded)
  v
For each result URL:
    Scrape page (cache -> HTTP -> Selenium fallback; outcome recorded)
    Extract Pass 1 (Gemini call: company_name, cornellians, status, funding_total_usd, founded_year)
        v
        For each candidate:
            Evidence-span check on every non-null field
            Extract Pass 2 (conditional, only fields whose preconditions are met)
        v
        For each StartupRecord:
            Validate against blocklist + affiliation rule
            db.upsert(record)  <-- model-aware merge
  v
Round metrics flush (writes round_metrics.jsonl)
  v
Circuit breaker check  <-- pauses run if parse rate collapsed
  v
Checkpoint save (now includes visited_urls + cache_manifest)
  v
Loop
```

## Error handling

Three explicit failure tiers replace today's "catch everything and continue":

| Tier | Examples | Action |
|---|---|---|
| Retryable | Selenium timeout, single empty Gemini response, 503 from a domain | Exponential backoff with jitter (`base * 2**n + random.uniform(0,1)`), up to 3 attempts. Each attempt logged. |
| Skippable | Schema-invalid extraction, prompt-echo, marker missing | Log structured error to `gemini_parse_failures.log`. Skip this page. Continue round. Counted in metrics. |
| Fatal | Browser session crash that fails to restart, 5 consecutive prompt-echoes, parse rate below 50% over 20 calls | Trip circuit breaker. Print warning. Stop accepting new work. User intervenes. |

`call_gemini()` no longer returns `""` on persistent failure. It raises `GeminiUnavailable`, which the orchestrator catches at the loop level to trip the circuit breaker.

## Testing

After this pass, these are unit-testable in isolation (matters because the existing codebase has near-zero test coverage):

- All Pydantic models -- validation, coercion, the `@field_validator` paths.
- `_parse_json(text, model_cls)` -- the marker-slice, fence-extract, schema-validate sequence. Test fixtures from `gemini_parse_failures.log`.
- `StartupDB.upsert` -- list-field union, scalar fill, merge-conflict logging, tier re-computation.
- Circuit breaker state machine -- given a sequence of outcomes, does it trip at the right point?
- Coercers -- already pure, just need tests.

Integration tests stay as live smoke tests (one seed URL, one round, assert at least one record extracted and metrics file written).

## What we are NOT changing

- The 2-worker Selenium architecture.
- The browser-Gemini session management in `gemini_tool.py` (the 9-strategy JS extractor stays; B1 just instruments it).
- The CSV output format -- it remains a flat view derived from the JSON DB.
- The Cornell-specific blocklist and source tiers.
- The 30K-char page chunking threshold in normal operation (level-2 of the degradation ladder drops it to 15K, but the default stays 30K).
- The perpetual run model with checkpoint-driven resume.

## Open questions

None blocking. Two we will hit during implementation:

1. **Degradation-ladder thresholds.** 70% for L1→L2, 50% for L2→L3 are starting values; we'll tune after seeing a week of real `round_metrics.jsonl`.
2. **Tag vocabulary.** `tags` is free-text today; over time, drift will produce variants ("AI/ML", "AI", "Machine Learning"). Likely answer: post-backfill, freeze the top 30 observed tags as a canonical set and remap variants at upsert time. Out of scope for this pass.

## Wiki references

This spec is grounded in the web-agent-skills wiki at `~/.claude/web-agent-skills/wiki/`. Entries cited rather than re-derived:

- [`primitives/gap-finding-loop.md`](~/.claude/web-agent-skills/wiki/primitives/gap-finding-loop.md) -- describes exactly this agent's structured-record + gap-report + targeted-query pattern. The "converging on garbage" failure mode (hallucinated records being chased forever) is what Workstream A4's evidence-span check is the defense against.
- [`site-profiles/gemini-web.md`](~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md) -- two production-verified facts shape this spec:
  - **50KB prompt cliff.** Prompts above ~50KB fall back from clipboard paste to chunked `send_keys`, costing ~38s per prompt. The Pass-1/Pass-2 split in A5 keeps each call well under this. Level-2 degradation (15K chunks) provides additional headroom.
  - **Anonymous mode is supported for text-only prompts.** `_looks_like_signed_out_deflection` already exists in `gemini_tool.py`. If the logged-in session rots, the agent can continue without re-auth for the extraction path.
  - **Do not "modernize" the SPACE+BACKSPACE input trick in this codebase.** The wiki explicitly notes the `startup_research_agent` copy is the older variant *without* the last-char-truncation bug present in three other copies on the machine.
- [`anti-patterns/silent-failure.md`](~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md) -- "validate output before declaring success" maps to A2's schema-validate-or-route-failure. "Fail loudly above a threshold" maps to the degradation ladder.
- [`anti-patterns/infinite-retry.md`](~/.claude/web-agent-skills/wiki/anti-patterns/infinite-retry.md) -- the current 3× flat-sleep retry pattern is the named anti-pattern. The error-handling section's "Tier 1: Retryable" rows must use bounded retries with exponential backoff + jitter and classify errors (4xx vs 5xx, 429 special).
- [`anti-patterns/selector-over-data-attribute.md`](~/.claude/web-agent-skills/wiki/anti-patterns/selector-over-data-attribute.md) -- the 9-strategy DOM cascade in `_JS_EXTRACT_RESPONSE` is selector-soup. We are not restructuring it in this pass, but B1's strategy-index logging surfaces drift when Gemini's UI ships changes.
- [`anti-patterns/headless-default.md`](~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md) -- the production run uses headless. Worth flagging as a known risk; not changed in this pass because Selenium fingerprinting issues haven't yet manifested at the volumes used.
- [`primitives/captcha-handoff.md`](~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md) -- HANDOFF.md already documents the CAPTCHA-in-non-TTY fix landed in May. Level-4 of the degradation ladder is the unattended counterpart -- pivot to local work instead of blocking on a human.
- [`primitives/resume-checkpoint.md`](~/.claude/web-agent-skills/wiki/primitives/resume-checkpoint.md) -- the wiki prefers per-unit JSON files; this codebase uses a monolithic `startup_checkpoint.json`. Noted divergence; out of scope for this pass.
- [`escalation-ladders/llm-as-tool.md`](~/.claude/web-agent-skills/wiki/escalation-ladders/llm-as-tool.md) -- we are explicitly staying on rung 2 (browser pseudo-API). The "cost discipline" guidance (profile prompt size, cache repeated context, smallest model that passes) is the framing for A5's contingent prompts.

## Success criteria

- `gemini_calls.jsonl` and `round_metrics.jsonl` exist after a 1-round smoke test, with at least one entry each.
- A deliberately broken extraction prompt (e.g., asking for invalid JSON) trips the degradation ladder from L1 to L2 within 20 calls, and from L2 to L3 within another 20.
- A smoke run produces zero records that pass `upsert` with an empty `cornellians` list or missing `proof_url`.
- Every non-null field on every new record has a matching `*_evidence_span` substring in the source page text. Records with unverifiable spans are demoted or dropped, not silently accepted.
- Resume after Ctrl+C does not re-fetch any URL in `visited_urls` from the previous run.
- The existing live-smoke seed-URL test (eship + bigredai) still produces at least 400 records under the new schema.
- The backfill produces a `startups_db_v2.json` with ≥80% of the original 1,357 records re-extracted successfully.
