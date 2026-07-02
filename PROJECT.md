# PROJECT: Startup Research Agent

> Living state-of-the-world for this repo. **Read this first** -- it tells you the current
> status, the next steps, and where every artifact lives, so you never have to Glob-hunt or
> ask for a path. The index and log below are regenerated automatically by
> `~/.claude/hooks/project_manifest.py` (run at session Stop); the **Status**, **Next steps**,
> and **Decisions & constraints** are human/agent-authored -- keep them current and bump
> `status-HEAD` when you do.

<!-- manifest-sync-config
scan:
  "Core pipeline (the agent)": ["startup_researcher.py", "gemini_tool.py", "schema.py", "evidence.py", "metrics.py", "degradation.py", "retry_policy.py", "url_canonical.py"]
  "Ops -- detached overnight launch": ["launch_detached.py", "run_detached.ps1"]
  "Data layer -- migrate / dedup / analyze": ["migrate_to_v2_schema.py", "dedup_records.py", "analyze_ecosystem.py", "export_csv.py", "export_network.py", "reextract_all.py"]
  "Enrichment -- wikipedia + linkedin": ["enrich_wikipedia.py", "discover_via_wikipedia_categories.py", "linkedin_login.py", "parse_linkedin_auth.py"]
  "Probes (empirical findings)": ["probe_headed_minimized.py", "probe_linkedin.py", "probe_linkedin_auth.py", "probe_gemini.py"]
  "Specs (design)": ["docs/superpowers/specs/2026-06-07-research-agent-v2-design.md", "docs/superpowers/specs/2026-06-05-hardening-pass-design.md", "docs/superpowers/specs/2026-06-07-browser-defaults.md"]
  "Plans (implementation)": ["docs/superpowers/plans/2026-06-07-research-agent-v2-implementation.md", "docs/superpowers/plans/2026-06-05-hardening-pass-implementation.md"]
  "Reports & handoffs": ["OVERNIGHT_REPORT.md", "HANDOFF.md", "BLOCKED_NEEDS_HUMAN.md", "cornell-startups-tasks.md"]
  "Tests": ["tests/test_parse_json.py", "tests/test_schema.py", "tests/test_db_upsert.py"]
external:
  - "~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)"
  - "~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07"
  - "~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok"
  - "https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass"
-->
_synced: 2026-07-01 20:43 UTC | HEAD: 86ed419 | status-HEAD: 86ed419

## Status

**The agent runs headed-minimized without CAPTCHA and now actually lands records.** The
long-standing "0 records despite hundreds of clean Gemini calls" symptom was a **parser bug**,
not the source-quality problem it looked like: Gemini renders extraction output as a ```json
code block, and when `gemini_tool` scrapes the response via innerText the backticks vanish but
the language label survives as a bare `JSON` prefix (`JSON{...}`). The fence regex needs literal
backticks so it missed, and the typed extraction parser then choked on the prefix at column 1.
390 real extractions -- each with 5-20 valid Cornell-founder records whose evidence spans ARE
present in the source page -- were silently thrown to `gemini_parse_failures.log`. Fixed with a
balanced-bracket payload extractor (`_extract_json_payload`) wired into `_parse_json_typed` +
`_slice_and_unfence`. Verified live: real parse failures dropped **390 -> 1**.

Fixing that exposed a CHAIN of downstream gates that each silently ate records, all now fixed:
(1) pass-2 enrichment fired a ~3-min Gemini call PER record (bigredai's ~300 companies -> ~15h
round 1), so **pass-2 is deferred by default** (`EXTRACT_PASS2=0`; pass-1 already yields complete
evidence-verified records). (2) the DB was saved only at end-of-round, so a mid-round interruption
lost everything -- now it **saves incrementally after every page**. (3) shell-launched background
runs die on Claude-session teardown (killed 3 overnight attempts mid-round) -- **`launch_detached.py`
+ `UNATTENDED=1`** spawns the agent fully detached (Windows `DETACHED_PROCESS`) so it survives.
(4) **the LAST gate**: `db.upsert`'s legacy dict branch required the OLD `cornellian_founder`
string, but `extract_startups` dumps new-schema dicts carrying a `cornellians` LIST -- so all 78
evidence-verified records from the first successful run were rejected as "no Cornellian founder
identified". Fixed: upsert backfills the legacy fields from `cornellians[0]` and accepts a
non-empty list. The full chain (parser -> evidence-span -> pass-1 -> upsert -> save) is now proven
end to end (51 tests green; a detached run is verifying live that records land).

The **v2 architecture is spec'd + planned but NOT built**: DuckDB store (lands first), intent-routed
query ladder (Selenium-Google-headed for quality + DDG/Brave/Mojeek/Startpage HTTP for speed),
candidates-pool + recovery-round flow, and a script-based Doctor watchdog. Spec:
`docs/superpowers/specs/2026-06-07-research-agent-v2-design.md`; plan:
`docs/superpowers/plans/2026-06-07-research-agent-v2-implementation.md`. The current agent is the
**hardening-pass** codebase (Pydantic schema, evidence-span gate, two-pass extraction, degradation
ladder, metrics) -- all landed and tested (50 tests green).

There is a separate, already-produced **data layer** from the overnight-of-2026-06-06 work: 1,389
deduped Cornell-startup records (heuristic-migrated from the legacy 1,525-record monolith),
ecosystem report, CSVs, and a Gephi-ready network graph. See `OVERNIGHT_REPORT.md`.

## Next steps

- [ ] **Confirm the current detached run landed records.** `startup_output_overnight/startups_db.json`
  count should climb past 0 (bigredai alone ~250). Ground-truth signal is that file's mtime + count.
  If it stalls, read `startup_output_overnight/run_detached.log` (note: startup can take ~12 min when
  undetected-chromedriver patches a new Chrome major).
- [ ] **Let a full overnight run complete**, then run the data layer over the fresh DB:
  `analyze_ecosystem.py`, `export_csv.py`, `export_network.py`. Report findings.
- [ ] **Enrichment pass** (free, no CAPTCHA): `enrich_wikipedia.py` for founded_year/HQ/status on
  high-profile companies; `parse_linkedin_auth.py` (auth-mode, headed) for employee_count / HQ /
  founder headlines. Both are built and probe-verified; not yet wired into the main loop.
- [ ] **Decide: build v2, or keep hardening the current agent?** v2 is a large build (DuckDB +
  query ladder + recovery + Doctor). The current agent now works; v2's biggest wins are the recovery
  loop (candidates that fail evidence-span get a second targeted search) and DuckDB queryability.
  PARKED until the current agent has produced a full dataset and we know what it's missing.
- [ ] **PARKED -- pass-2 enrichment inline.** Deferred by default. If wanted, `EXTRACT_PASS2=1`
  (gated to pages yielding <= 5 records so a dedicated company page enriches but an aggregator
  doesn't stall). The Wikipedia + LinkedIn scripts largely supply the same fields.

## Decisions & constraints

The center of truth for **locked decisions and standing constraints**. Check here BEFORE re-opening
a settled question: if a tension is recorded resolved, reference it, don't re-litigate. Authored and
preserved by the sync (never auto-rewritten).

- **[2026-06-07] Headed-minimized is the binding default browser mode, not headless.** Empirically
  verified (`probe_headed_minimized.py`): undetected-chromedriver headed + `minimize_window()` +
  `--window-position=-10000,-10000` passed 4/5 Google queries (the 1 failure was a
  `site:linkedin.com/in/` abuse-pattern query that ALSO fails headless) and returned full LinkedIn
  `/in/` JSON. Friday's 7-hour CAPTCHA loop and the "LinkedIn /in/ throttle" were both the headless
  penalty. Headless is an explicit opt-in for targets verified not to fingerprint it. Wiki:
  `anti-patterns/headless-default.md`.
- **[2026-06-11] The evidence-span gate is a procedural anti-hallucination defense -- never weaken
  it.** Every non-null field Gemini returns must be a substring of the source page or it is dropped.
  When the agent produced 0 records, the fix was NEVER to loosen this gate; it was to fix the parser
  (below) and the source strategy. The gate is why the dataset can be trusted.
- **[2026-06-11] Gemini's rendered code-block leaks a bare `JSON` language-label prefix into scraped
  innerText.** Parse defensively: after the fence-regex attempt, use a balanced-bracket scan
  (`_extract_json_payload`) that tolerates a label prefix and surrounding prose. Put it on BOTH parse
  paths (the untyped planner parser had a bracket fallback; the typed extraction parser -- the one
  that mattered -- did not, which hid the bug for a whole build cycle). Wiki: `site-profiles/gemini-web.md`
  lesson 2026-06-11.
- **[2026-06-11] Pass-2 enrichment is deferred by default (`EXTRACT_PASS2=0`).** It fires a full
  ~3-min Gemini call per record; on aggregator pages that makes a round take many hours. Pass-1
  already yields a complete evidence-verified record; description/tags/URLs come from the Wikipedia +
  LinkedIn enrichment scripts. Re-enable only for small dedicated-company pages.
- **[2026-06-11] Long runs must be detached + unattended.** Shell-launched background runs die on
  Claude-session teardown. Use `launch_detached.py` (Windows `DETACHED_PROCESS`, argv as a list -- no
  shell quoting) with `UNATTENDED=1` (skips the Enter prompt; cookies must be preloaded) and rely on
  the per-page incremental save so an interruption never loses landed records.
- **[2026-06-07] v2 store = DuckDB, not SQLite or per-record JSON.** Chosen for native `ALTER TABLE`
  (column rename/type-change/drop are single statements) during an iterating schema, native JSON
  columns, single-file embedded. R (records store) lands first so nothing runs against the old
  monolithic JSON during the v2 build.
- **[2026-06-05] The aggregator "0 records" was a parser/tooling problem, not a data problem.**
  bigredai.org/startups DOES state founders + Cornell affiliation inline ("Raj Mehra, MBA '09").
  Confirmed the evidence spans Gemini returns are `span_present` in the cached page. Don't conclude
  "the source lacks evidence" without checking the parse path first.
- **[2026-07-02] The hardening pass left a schema seam: `extract_startups` dumps StartupRecord to
  DICTS, which hit `db.upsert`'s LEGACY dict branch, not the model-aware Pydantic branch.** That
  legacy branch (blocklist gate, `cornellian_founder` hard rule, `RECORD_FIELDS` merge) expects the
  OLD flat schema. A new-schema dict must be made legacy-compatible at the branch boundary (backfill
  `cornellian_founder` / `affiliation_*` from `cornellians[0]`). Tests that pass Pydantic OBJECTS
  hit the other branch and hide this whole class of bug -- test the DICT path explicitly. Deeper fix
  (deferred): retire the dict shim and pass models end to end, or fully port the legacy branch onto
  the new schema.
- **[2026-06-05] Reusable scraping lessons live in the web-agent-skills wiki, not here.** Capture via
  `/lesson`. This repo's PROJECT.md points at them; it does not duplicate them.

## Artifact index

<!-- AUTO:index -->
**Core pipeline (the agent)**
- `startup_researcher.py` -- startup_researcher
- `gemini_tool.py` -- gemini_tool
- `schema.py`
- `evidence.py`
- `metrics.py`
- `degradation.py`
- `retry_policy.py` -- Transient failure -- safe to retry
- `url_canonical.py`

**Ops -- detached overnight launch**
- `launch_detached.py` -- Spawn the research agent as a fully DETACHED background process on Windows
- `run_detached.ps1`

**Data layer -- migrate / dedup / analyze**
- `migrate_to_v2_schema.py` -- Heuristic conversion of the prod startups_db
- `dedup_records.py` -- Aggressive deduplication of the migrated DB based on canonical company name
- `analyze_ecosystem.py` -- Ecosystem analysis from the migrated Cornell-startup DB
- `export_csv.py` -- Export the deduped (or enriched) DB to a flat CSV for spreadsheet review
- `export_network.py` -- Export the Cornell startup network as a graph:
- `reextract_all.py` -- One-shot re-extraction of every record in startups_db

**Enrichment -- wikipedia + linkedin**
- `enrich_wikipedia.py` -- Enrich the deduped Cornell-startup DB with Wikipedia data: headquarters,
- `discover_via_wikipedia_categories.py` -- Discover candidate Cornell-affiliated companies via Wikipedia category traversal
- `linkedin_login.py` -- Interactive LinkedIn login: opens a visible Chrome window, navigates to the
- `parse_linkedin_auth.py` -- Authenticated LinkedIn probe + JSON-LD/voyager parser

**Probes (empirical findings)**
- `probe_headed_minimized.py` -- Empirical test of the wiki claim:
- `probe_linkedin.py` -- Empirical probe: what does LinkedIn return to a logged-out client?
- `probe_linkedin_auth.py` -- Same target set as probe_linkedin
- `probe_gemini.py` -- Probe harness: ask Gemini to extract records from a known cached page,

**Specs (design)**
- `docs/superpowers/specs/2026-06-07-research-agent-v2-design.md` -- Research Agent v2 -- Design
- `docs/superpowers/specs/2026-06-05-hardening-pass-design.md` -- Startup Research Agent -- Hardening Pass

**Plans (implementation)**
- `docs/superpowers/plans/2026-06-07-research-agent-v2-implementation.md` -- Research Agent v2 -- Implementation Plan
- `docs/superpowers/plans/2026-06-05-hardening-pass-implementation.md` -- Startup Research Agent -- Hardening Pass Implementation Plan

**Reports & handoffs**
- `OVERNIGHT_REPORT.md` -- Overnight Run -- 2026-06-06 → 2026-06-07
- `HANDOFF.md` -- Startup Researcher — Conversation Handoff
- `BLOCKED_NEEDS_HUMAN.md` -- Tasks Requiring Local Browser Execution
- `cornell-startups-tasks.md` -- Cornell Alumni + Startups Research Tasks

**Tests**
- `tests/test_parse_json.py` -- Gemini's rendered code-block language label ('JSON') leaks into the
- `tests/test_schema.py`
- `tests/test_db_upsert.py`

- (external) ~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)
- (external) ~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07
- (external) ~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok
- (external) https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass
<!-- /AUTO -->

## Recent log

<!-- AUTO:log -->
- 86ed419 feat(ops): detached-launch scripts for session-teardown-proof overnight runs
- bdf0dd0 feat(researcher): UNATTENDED=1 skips interactive prompts for detached runs
- 4e6858b perf(researcher): defer pass-2 by default + incremental DB save per page
- 9e336f7 fix(researcher): recover records from Gemini's 'JSON{...}' label-prefix responses
- 6ea92fd fix(researcher): refuse to overwrite cookie file when auth marker would be lost
- d2e9a38 spec(v2): BrowserSession.handoff_for_captcha contract
- ad89fa1 plan(v2): implementation plan (R, S, Q, F, D workstreams)
- 51900ad spec(v2): empirical headed-minimized probe + Q workstream rework
- 5d0d2ed spec: switch v2 store to DuckDB; R-first landing order
- 200a60a spec: research-agent v2 (Selenium audit, query ladder, SQLite store, recovery flow, Doctor)
- 583ff75 feat(linkedin): auth-mode probe + JSON parser, working company extractor
- 88163cb docs(overnight): summary report + network/discovery scripts
<!-- /AUTO -->
