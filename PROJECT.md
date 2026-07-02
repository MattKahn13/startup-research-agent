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
  "Data layer -- migrate / dedup / analyze": ["migrate_to_v2_schema.py", "dedup_records.py", "analyze_ecosystem.py", "export_csv.py", "export_network.py", "reextract_all.py", "repair_stranded_founders.py"]
  "Enrichment -- wikipedia + linkedin": ["enrich_wikipedia.py", "discover_via_wikipedia_categories.py", "linkedin_login.py", "parse_linkedin_auth.py"]
  "Probes (empirical findings)": ["probe_headed_minimized.py", "probe_linkedin.py", "probe_linkedin_auth.py", "probe_gemini.py"]
  "Specs (design)": ["docs/superpowers/specs/2026-06-07-research-agent-v2-design.md", "docs/superpowers/specs/2026-06-05-hardening-pass-design.md", "docs/superpowers/specs/2026-06-07-browser-defaults.md"]
  "Plans (implementation)": ["docs/superpowers/plans/2026-06-07-research-agent-v2-implementation.md", "docs/superpowers/plans/2026-06-05-hardening-pass-implementation.md"]
  "Reports & handoffs": ["OVERNIGHT_REPORT.md", "HANDOFF.md", "BLOCKED_NEEDS_HUMAN.md", "cornell-startups-tasks.md"]
  "Tests": ["tests/test_parse_json.py", "tests/test_schema.py", "tests/test_db_upsert.py", "tests/test_gap_fill_field_consistency.py", "tests/test_json_quote_repair.py", "tests/test_parse_json_shape_confusion.py"]
external:
  - "~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)"
  - "~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07"
  - "~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok"
  - "https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass"
-->
_synced: 2026-07-02 21:39 UTC | HEAD: 7d1d5cf | status-HEAD: 7d1d5cf

## Status

**2026-07-02 ~17:21 UTC: the relaunched run crashed for real (not a regression of the two bugs
fixed a few hours earlier) -- root-caused, fixed, relaunched clean as PID 26408.** Monitoring
after the evening audit found PID 27244 dead with an uncaught exception:
`AttributeError: 'list' object has no attribute 'get'` in `run()` at `strategy.get("thinking", "")`.
Traced to `_parse_json`'s bracket-boundary fallback, which tries array boundaries (`[`...`]`)
before object boundaries (`{`...`}`) -- correct for the extraction caller ("try array first since
we expect arrays from extract", per its own comment) but wrong for `generate_gap_filling_strategy`,
which always expects a dict wrapping a nested `actions` array. Whenever Gemini's raw text has any
prose before the real JSON object (e.g. "Here is my analysis.\n\n{...}"), the direct whole-text
parse fails on the prose, and the array-first fallback finds the *inner* `actions` array's own
brackets before the outer object's -- silently returning just that list. Isolated with a clean,
one-variable repro containing zero quote-escaping issues, proving this is unrelated to tonight's
earlier quote-repair fix; it's a pre-existing latent ordering flaw that just hadn't been hit before.
Grep'd all four `_parse_json` call sites and found the SAME vulnerability latent in two more places
(`fill_missing_data`'s per-record fill result, the verify-batch `{"decisions": [...]}` call) that
hadn't crashed yet only by luck. Fixed once, centrally: `_parse_json` now takes an optional
`expect_type` hint; a successful parse of the wrong type is treated as a failed attempt and the
search keeps going (through the other bracket order, then quote-repair, then the caller's
`fallback`) instead of returning the mismatched value. All three dict-expecting call sites now pass
`expect_type=dict`; the one array-expecting caller (extraction) is untouched. TDD, 4 new tests,
62/62 green. No data lost -- the crash happened after Round 1's `db.save()` (273 records on disk,
confirmed before relaunch); the crash log was copied to
`run_detached.log.crash-20260702-1721` before the relaunch overwrote it.

**2026-07-02 evening audit found and fixed a second silent data-loss bug in gap-fill, now confirmed
live.** Requested audit ("see what issues exist, make repairs") turned up two real bugs, neither
of which was the thing being watched for:

1. **`fill_missing_data`/`gap_report` read and wrote the legacy `founders` field, not
   `cornellian_founder`** -- the field `validate_record`/`upsert`/CSV export actually treat as
   authoritative. A record could have a garbage `cornellian_founder` (self-flagged by its own
   `validation_issues`) and gap-fill would never target it, because `gap_report` only checked
   whether `founders` was empty. When gap-fill DID run and Gemini found the real name, it wrote
   that name to `founders` -- a field nothing downstream reads -- so the console reported success
   while `cornellian_founder` stayed wrong forever. Caught by inspecting a real record (Conceive:
   `cornellian_founder="health providers"`, `founders=""`, both present with divergent values) and
   confirmed with a deterministic mocked regression test showing the console "success" message
   never actually persisting. Fixed all three call sites (`gap_report`, `fill_missing_data`,
   the ambiguous-record completeness check) to read/write `cornellian_founder`.
2. **Gemini's JSON responses periodically embed literal unescaped quotes inside a string value**
   (a Google phrase-match operator like `"Cornell University"`, or a nickname like `Maofan "Ted"
   Yin`), breaking `json.loads` at the string-literal level even though bracket boundaries are
   correct -- a different failure mode than the label-prefix bug fixed 2026-06-11. Confirmed via
   the actual captured raw response text (not assumed to be the same bug) before writing a fix.
   Added `_repair_unescaped_json_quotes` as a third parse attempt in `_parse_json` (defense in
   depth alongside the existing prompt instruction, which Gemini doesn't reliably follow).

Both fixed under TDD (58/58 tests green, up from 51). **6 records that had already been correctly
gap-filled under the old buggy code** (right answer sitting in the dead `founders` field) were
recovered via a one-off script (`repair_stranded_founders.py`) rather than re-spending Gemini/
Selenium budget re-discovering them -- e.g. Conceive -> Lauren Berson Sugarman, Hyro -> Israel
Krush, xPub -> Pallavi Bansal. The live detached run was killed (PID 26172), the DB repaired
in place, and relaunched (PID 27244) via the same `launch_detached.py` + kb-gate contract
pattern; boot log confirmed a clean resume from the repaired 206-record file ("DB: 206 records
loaded"). Post-restart the log shows zero recurrences of either bug's failure signature and
normal extraction activity continuing. `.gitignore` also gained the two runtime-output dirs
(`startup_output_overnight/`, `startup_output_test_headed/`) that were created mid-arc and
untracked-but-not-ignored.

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
non-empty list.

**CONFIRMED LIVE (2026-07-02 ~13:25 UTC):** the full chain (parser -> evidence-span -> pass-1 ->
upsert -> save) works end to end. Detached run PID 26172 landed **60 real records** in the first
~15 min (Sage/Raj Mehra, OpenEvidence/Zachary Ziegler, Hermeus/Michael Smayda, ... sourced from
bigredai + eship.cornell.edu + tech.cornell.edu + elabstartup.com), each with a matched
`evidence_span` and `proof_url`. 51 tests green. Known cosmetic bug (non-blocking): apostrophes in
evidence spans render as UTF-8-as-Latin-1 mojibake (`MBA` + garbled apostrophe + `09` instead of
`MBA '09`) -- doesn't break evidence-span matching, tracked as a follow-up.

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

- [x] **Confirm the current detached run lands records.** DONE 2026-07-02 -- PID 26172 landed 60
  real evidence-verified records in ~15 min from bigredai/eship/tech.cornell.edu/elabstartup.com.
  The full chain is proven end to end.
- [x] **Audit + repair the gap-fill field mismatch and the JSON quote-escaping failure.** DONE
  2026-07-02 evening -- see Status. Both fixed under TDD, 6 records recovered, process restarted
  clean as PID 27244.
- [x] **Root-cause + fix the shape-confusion crash that killed PID 27244.** DONE 2026-07-02 ~17:40
  UTC -- see Status. `_parse_json` gained an `expect_type` guard; relaunched clean as PID 26408.
- [ ] **Let the overnight run complete** (now PID 26408, resumed from the 273-record DB -- no data
  lost across either restart), then run the data layer over the fresh DB: `analyze_ecosystem.py`,
  `export_csv.py`, `export_network.py`. Report findings. Monitor
  `startup_output_overnight/startups_db.json` count + `run_detached.log` (see
  `startup_output_overnight/run_detached.pid`).
- [ ] **Merge tonight's fresh run into the real 1,389-record dataset.** Tonight's run
  (`startup_output_overnight/startups_db.json`) started from an EMPTY db on purpose (isolate the
  parser-fix verification from the real dataset). It is NOT additive to `startup_output_test/
  startups_db_deduped.json` (the 1,389, June 6-7) or `startup_output/startups_db.json` (the
  original 1,525, May). Once the overnight run is done, dedupe-merge its new records into the 1,389
  the same way `dedup_records.py` merged the original 1,525 -- by canonical company name.
- [ ] **Fix the evidence_span mojibake** (non-blocking, cosmetic). Apostrophes render as
  UTF-8-as-Latin-1 garble (`MBA` + garbled char + `09`). Likely the page-scrape decode step; check
  `scrape_page`'s encoding detection. Doesn't break matching today but will look bad in any
  client-facing export.
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

- **[2026-07-02] Any shared JSON-repair helper needs to know what SHAPE the caller expects, not
  just how to recover valid JSON.** `_parse_json` is called by four different places: one always
  wants a bare list (extraction), three always want a dict wrapping a nested array
  (`generate_gap_filling_strategy`, `fill_missing_data`'s fill result, the verify-batch decisions).
  Its bracket-boundary fallback tries array boundaries before object boundaries -- right for the
  first caller, wrong for the other three, and the failure is silent: it returns a real, validly
  parsed value of the WRONG type (the nested `actions` list instead of the wrapping dict) rather
  than raising or falling back, so it crashes downstream instead of at the parse site. Fixed with
  an `expect_type` parameter -- a parse of the wrong type is treated as no-match and the search
  continues. Any NEW caller of `_parse_json` that expects a dict must pass `expect_type=dict`;
  don't assume the default bracket order is safe just because parsing "succeeds."
- **[2026-07-02] `cornellian_founder` is the single authoritative founder field everywhere --
  `founders` is a legacy/secondary field that nothing downstream should read.** `validate_record`
  requires `cornellian_founder`; `upsert`, tier scoring, and CSV export all read it too. Any new
  code that discovers or repairs a founder name (gap-fill, enrichment, one-off scripts) must write
  `cornellian_founder` (and may mirror into `founders` for legacy display, as `fill_missing_data`
  now does) -- writing only `founders` is a silent no-op bug that looks like success in the console.
  Reuse `_looks_like_human_name()` to judge whether a candidate value is real; it's already shared
  by `validate_record`, `gap_report`, `fill_missing_data`, and the ambiguous-record check.
- **[2026-07-02] JSON quote-repair is a THIRD, independent parse fallback -- do not conflate it with
  the 2026-06-11 label-prefix fix.** `_parse_json` now tries a direct `json.loads` first, then a
  balanced-bracket boundary extraction, then `_repair_unescaped_json_quotes` on the bracket-matched
  candidate as a last resort. That third failure mode is Gemini embedding a literal unescaped `"`
  inside a string value (search phrase-match operators, nicknames) -- bracket boundaries are fine,
  the string literal itself is broken. Verify which failure mode you're looking at from the raw
  captured text before assuming it's the same bug as before; they need different fixes. Wiki:
  `anti-patterns/llm-json-unescaped-quotes.md`.
- **[2026-07-02] There are THREE separate dataset generations in THREE separate files -- they do
  NOT merge automatically.** (1) `startup_output/startups_db.json` -- 1,525 records, the ORIGINAL
  May production DB, old flat schema (`cornellian_founder` string, no evidence-span). (2)
  `startup_output_test/startups_db_deduped.json` -- 1,389 records, the June 6-7 migrated + deduped
  dataset (the REAL working dataset; ecosystem report / CSVs / network graph were built from this).
  (3) `startup_output_overnight/startups_db.json` -- started from ZERO on 2026-07-02 on purpose, to
  isolate verification of the parser-chain fix from the real dataset. It is NOT additive to (1) or
  (2) until explicitly merged (see Next steps). Before answering "how many startups do we have,"
  check WHICH file is being asked about -- "the tally" almost always means (2), not whatever the
  most recent run produced.
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
- `repair_stranded_founders.py` -- One-off data repair: promote a record's legacy 'founders' value into

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
- `tests/test_db_upsert.py` -- The live flow passes DICTS (StartupRecord
- `tests/test_gap_fill_field_consistency.py` -- Regression tests for the founders / cornellian_founder field-mismatch bug
- `tests/test_json_quote_repair.py` -- Regression tests for the unescaped-inner-quotes JSON repair
- `tests/test_parse_json_shape_confusion.py` -- Regression tests for a shape-confusion crash in `_parse_json`

- (external) ~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)
- (external) ~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07
- (external) ~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok
- (external) https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass
<!-- /AUTO -->

## Recent log

<!-- AUTO:log -->
- 7d1d5cf docs(manifest): record tonight's audit -- gap-fill field mismatch + JSON quote repair, 6 records recovered, PID 26172->27244
- 8843778 chore: gitignore newer runtime output dirs; add repair_stranded_founders.py
- db6a82d fix(planner): repair unescaped inner quotes in Gemini JSON before giving up
- b4007ee fix(gap-fill): read/write cornellian_founder, not the dead legacy 'founders' field
- ae9b4ce docs(manifest): clarify the three separate dataset generations -- nothing was lost
- ea6269b docs(manifest): sync + confirm-status
- 44e0dbb docs(manifest): CONFIRMED -- 60 records landed live; mark next-step done, add mojibake follow-up
- f7d418f docs(manifest): sync + confirm-status
- 85b5f4d docs(manifest): record the upsert schema-seam fix in Status + Decisions
- cd07c3a fix(db): accept new-schema dicts in upsert -- the LAST gate that dropped records
- 91c3f6f docs(manifest): add living PROJECT.md -- state-of-truth for compaction survival
- 86ed419 feat(ops): detached-launch scripts for session-teardown-proof overnight runs
<!-- /AUTO -->
