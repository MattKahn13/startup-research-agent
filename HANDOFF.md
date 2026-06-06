# Startup Researcher — Conversation Handoff

**Date:** 2026-05-08 (debug session 2 — RESOLVED)
**Working dir:** `g:/My Drive/Cornell/Spring 2026/Agents/startup_research_agent/`
**Main script:** `startup_researcher.py`

## 🎉 SESSION RESULT — chunk extraction now works

A 2-seed-URL run (eship.cornell.edu + bigredai.org/startups) extracted
**441 records** in 10m52s, with `cornellian_founder`, `description`, and
`industry` populated on all 441 records (440 complete by validator).
Previous runs in this session were stuck at 0 records.

The unblocking change was switching the JS response extractor in
`gemini_tool.py` to query `<message-content>` directly (the inner-most
model-reply element in current Gemini's DOM), rather than guessing via
size/position/class heuristics. Confirmed by an interactive DOM-probe
session (`junk/inspect_gemini_response.py` → `junk/html.txt`) where the
user opened DevTools and saved the rendered DOM.

### Production run summary (May 8 evening)

`cornell-startups-tasks.md` deliverables:

| Item | Status |
|---|---|
| eship.cornell.edu/cornell-startups/high-profile-startups/ | ✅ scraped — 85 records sourced from this domain after merge |
| bigredai.org/startups | ✅ scraped — 348 new records added |
| Flag bigredai entries for review | ✅ all 348 bigredai records carry `source_credibility=2` and validation_tier="provisional" with a `single-source from low-credibility origin` issue note |
| Gemini quality verify | ✅ removed 1 (Activate — fellowship program, not a startup) |

**Final production DB state:**
- 1,357 total records (up from 1,010 → net +347)
- 74 high / 304 provisional / 979 weak
- 460 records flagged-for-review (low-credibility, unverified)
- Backups in `startup_output/startups_db.bak.*.json`

### Five additional fixes landed in this session

1. **`<message-content>` → strict JS extractor** (the breakthrough). With a
   `length >= 10` floor so we don't return a typing-cursor placeholder.
2. **Planner prompt**: now uses `\`\`\`json` fence + the marker + asks for
   single quotes around phrase-match Google search queries. Previously
   the planner emitted `"site:ycombinator.com "Cornell" founders"` (broken
   JSON); now it returns valid JSON.
3. **`plan_research()`** normalises both shapes — Gemini sometimes emits
   the strategies LIST directly instead of the wrapping `{strategies: …}`
   object. Either is accepted now.
4. **`SOURCE_TIERS`**: added `"bigredai.org": 2` and changed
   `score_source()` to return the LONGEST matching pattern (was
   returning the first match in dict-insertion order, so `.org` would
   shadow `bigredai.org`).
5. **`validate_record()`**: when a record is single-sourced from a known
   low-credibility origin (cred ≤ 2), it adds an explicit
   `single-source from low-credibility origin` validation issue and
   marks the record `provisional`.

## What Changed This Session (debug pass)

Five fixes addressing the "Outstanding Issues" below. **Three** were what
the previous handoff identified; **two more** were uncovered by running the
live smoke test, which revealed that the JS response extractor in
`gemini_tool.py` was capturing the user prompt instead of Gemini's reply.

### 1. Gemini extraction prompt now demands a ```json fence
- Format rules moved to TOP of `_extract_startups_chunk` prompt (was buried at bottom).
- Output must be wrapped in a single ```json … ``` fenced code block.
- Explicit anti-table instruction ("DO NOT render the data as a Markdown table").
- `_clean_json` now extracts the largest ```json fence's contents (handles
  prose-then-fence, fence-then-prose, multiple fences). Falls back to old
  behaviour when no fence is present.
- `_parse_json` failure log now includes `raw_len`, `cleaned_len`, `has_fence`,
  `has_bracket` for faster diagnosis. Saves first 8KB instead of 5KB.
- **Why this fixes it:** the failure-log evidence showed Gemini was emitting a
  rendered Markdown table (cell text concatenated without separators when read
  via `innerText`). Forcing a code fence makes Gemini emit `<pre><code>`
  which preserves whitespace through `innerText`.
- Unit tests in `junk/test_clean_json.py` (8/8 pass) and
  `junk/test_parse_json_e2e.py` (6/6 pass).

### 2. CAPTCHA in headless no longer crashes the worker
- `google_search` now checks `sys.stdin.isatty()` before calling `input()`.
- TTY context: same behaviour as before, plus `try/except EOFError` guard.
- Non-TTY (headless / piped / CI): logs warning, sleeps 30s, retries; gives
  up after `MAX_RETRIES` instead of throwing.

### 3. eship.cornell.edu and other SPAs now wait for hydration
- `eship.cornell.edu` added to `_JS_HEAVY_DOMAINS` (skip HTTP, go direct to Selenium).
- New `_wait_for_body_to_stabilise()` helper polls `body.innerText.length`
  until unchanged for 1s (8s cap). Replaces the old fixed `time.sleep(1.5)`
  in `scrape_page`.
- If first soup-extract returns `empty`, retry once with a longer settle
  window (10s cap, 2s stable) before declaring the page dead.

### Verified statically (this session)
- `python -m ast.parse` — file parses ✓
- `import startup_researcher` — module loads ✓
- 8/8 `_clean_json` unit tests pass ✓
- 6/6 `_parse_json` end-to-end tests pass (including a regression test that
  the OLD broken markdown-table response still falls back to `[]` instead
  of crashing) ✓
- Prompt template assembles without f-string errors ✓

### 4. JS response extractor in `gemini_tool.py` rebuilt
- Live smoke test showed the parser was being given the **user prompt** plus
  Gemini's left-nav chrome ("Sign in / Gemini / About Gemini"), not Gemini's
  reply. Root cause: the existing strategies (especially Strategy 7) picked
  the **largest** text block under 50K chars. With a 30K-char user prompt
  embedding the page content, the user message *is* the largest text block
  on the page, so the extractor returned it instead of the model reply.
- Added a high-priority **Strategy 0** that finds the LAST element matching
  response-class selectors (`model-response`, `message-content`,
  `[class*="model-response"]`, `[data-message-author-role="model"]`, etc.)
  in DOM order and returns its text. In a chat UI the model reply is always
  the most recently added DOM node — iterating in reverse picks it correctly.
- Added an `isUserScoped()` helper that walks ancestors looking for
  user-message indicators (`<user-query>`, `[class*="user-query"]`,
  `data-message-author-role="user"`, etc.) and skips matches inside a
  user-message container.
- Modified Strategy 7 + 8 to iterate in **reverse DOM order** as well, so
  fallback paths also prefer the latest element over the largest.
- Strategy 9 (formerly the largest-block rule) is now a true last resort.

### 5. Marker-slice safety net in `_clean_json` (defense-in-depth)
- Even if the JS extractor still returns the user prompt by accident, the
  Python parser slices everything before the LAST occurrence of
  `<<<__GEMINI_RESPONSE_BELOW__>>>`. The marker is appended as the very last
  line of every extract prompt, so anything after it in the captured text
  is candidate model output.
- Two extra unit tests in `junk/test_parse_json_e2e.py` cover this path
  (8/8 pass, including the realistic prompt-echo failure mode).
- Reordered the prompt so the marker is truly the final token (a previous
  draft put a "BEGIN — emit the ```json fence now:" line *after* the marker,
  which leaked literal triple-backticks into the post-marker region and
  confused the fence regex).

### Verified statically (this session)
- `python -m ast.parse` on both `startup_researcher.py` and `gemini_tool.py`
  — both files parse ✓
- 8/8 `_clean_json` unit tests pass ✓
- 8/8 `_parse_json` end-to-end tests pass (now including the realistic
  prompt-echo + marker case and the no-fence graceful-degrade case) ✓

### Live smoke test results (run 1, before fixes 4 & 5)
- ✅ CAPTCHA-in-non-TTY no longer crashed: workers logged
  `CAPTCHA detected in non-TTY context — backing off` and continued.
- ❌ Gemini extraction still produced parse failures, but the failure log
  now showed *what was actually captured* — and it turned out to be the
  user prompt + UI chrome, not Gemini's reply. That uncovered fixes 4 & 5.

### Live smoke test results (runs 2-12, after all five fixes)
- ✅ CAPTCHA-in-non-TTY no longer crashed across every run.
- ✅ Strategy planner (1.5KB user prompt → 5K-char JSON reply) was
  captured correctly by the JS extractor in early runs.
- ❌ Chunk extraction (large prompts) NEVER produced parseable JSON over
  the course of a dozen iterations on the JS extractor. The model reply
  is consistently absent from whatever the extractor returns:
  - Run 2-3: extractor returned the user prompt + UI chrome (37K chars).
  - Runs 4-7: extractor returned the chat composer toolbar (60-138 chars
    of "Use microphone / Edit prompt / Stop response").
  - Runs 8-9: extractor returned individual nav-link panels (138 chars).
  - Run 10: switched the extractor to `document.body.innerText`. Captured
    1520 chars total — but the section between "Gemini said" and the
    footer was empty. Either the response was rendered in a shadow root
    that body.innerText doesn't traverse, OR the script captured *before*
    Gemini began streaming the response.
  - Run 11-12: tried recursive shadow-DOM traversal in JS — caused the
    extractor to return empty, leading to "Empty response after restart".
    Reverted to body.innerText.

### 6. JS response extractor now uses `document.body.innerText`
- File: `gemini_tool.py`, function `_JS_EXTRACT_RESPONSE`.
- Removed the legacy Strategies 1-6 (they queried `<message-content>` /
  `<model-response>` / etc. without size or user-scope filters and ended
  up returning the user prompt for long prompts).
- Restructured remaining strategies into Pass A (strong selectors with
  ≥50-char floor and 30K cap), Pass B (weak class selectors with ≥150
  floor), Strategy 7 (paragraph blocks with reverse-order iteration and
  `isUserScoped` / `isInteractiveChrome` filters), Strategy 8 (broader
  div fallback), Strategy 9 (the `body.innerText` last resort).
- **However**: even `body.innerText` doesn't reliably contain the model
  reply for long extract prompts in the current Gemini DOM. **The root
  cause is in Gemini's UI**, not in our JS — the response renders into a
  DOM region that a generic `body.innerText` walk doesn't see.

## STATUS — what works

### ✅ End-to-end verified live
- **Chunk extraction returns parseable JSON.** Confirmed with the seed-URL
  smoke test: 441 records / 9 chunks of bigredai with zero parse failures.
- CAPTCHA-in-headless does not crash workers.
- Selenium SPA polling (`_wait_for_body_to_stabilise`).
- Parser fence extraction + marker-slice (8/8 unit tests).
- The new strict JS extractor: queries `<message-content>`, then
  `<response-container>`, then `<model-response>` (in order). Returns
  empty string if none exist — that's deliberate, so the wait loop sees
  text length grow as Gemini streams instead of seeing a stable user
  prompt and falsely declaring "done."

### ⚠️ Known but separate from the main fix
- **Strategy planner JSON has unescaped double quotes** inside Google
  search query strings (e.g. `"site:ycombinator.com "Cornell" founders"`).
  Parses fail; the script falls back to a hard-coded one-strategy plan.
  Two ways to fix:
  1. Ask the planner for a fenced-JSON output (same trick we used for
     extraction). The marker-slice + fence regex would clean it up.
  2. Change the prompt to require single quotes inside Google queries,
     or to escape embedded double quotes with `\\"`.
- **Round 1 Google searches** still hit CAPTCHA in headless. The new
  CAPTCHA-non-TTY path no longer crashes; queries are skipped after 3
  back-off attempts. Round 1 ended with 0 new records this run, but the
  seed URLs alone produced 441.

### Why earlier hypotheses were wrong (post-mortem)
1. **"Shadow DOM"** — wrong. The DOM probe showed the response IS in
   regular DOM. The reason `body.innerText` looked empty is that the
   wait loop captured *before* Gemini had streamed any tokens (its body
   text was just the user prompt, which is stable from t=0). When we
   captured `<message-content>` directly, returning empty for the first
   ~30s, the wait loop saw text grow from 0 → final and stayed alive
   long enough for Gemini to finish.
2. **"Cap by size"** — wrong. Trying caps of 12K / 25K / 30K kept
   admitting the user prompt's `<p>` element while excluding the model
   reply. The fix was to be a SELECTOR-based filter, not a size filter.
3. **"`<model-response>` is gone"** — half-right. It exists, but only
   AFTER Gemini begins streaming. The probe ran during the moment it
   didn't yet exist; the user's manual DOM dump (taken later, with the
   reply visible) had it. Conclusion: query the elements AT THE RIGHT
   TIME, not "find a generic largest text block."

### How the live DOM probe unblocked us
`junk/inspect_gemini_response.py` runs Gemini in visible Chrome with a
realistic 10K-char prompt, prints the response, then runs a JS query to
list which selectors actually match the DOM (count of `<model-response>`,
`<message-content>`, `[class*="response"]`, etc.) and keeps the browser
open for 15 minutes for manual inspection. Re-run this any time Gemini
ships a UI update that breaks the extractor.

### Side issue surfaced — strategy planner JSON has unescaped quotes
The strategy planner prompt asks Gemini for plain JSON, no fence. Gemini
returns:
```
"queries": ["site:ycombinator.com "Cornell University" founders"]
```
The double quotes around `"Cornell University"` aren't escaped, breaking
JSON parsing. Two quick fixes:
1. Apply the fenced-JSON-output style to the planner prompt too (so the
   parser at least has a clean delimiter).
2. Have the prompt explicitly require `\\"` for embedded quotes, or use
   single quotes inside Google search queries.

This is independent of the extractor bug.

---


## Goal
Build a perpetual web-scraper that finds Cornell-affiliated companies (any size, any era — startups *and* Fortune 500s) by running Google searches and using a logged-in Gemini browser session as both a search-strategist and a structured-data extractor.

## Affiliation Rule
A company qualifies **iff at least one founder is a Cornellian** (any Cornell school — CU, Cornell Tech, Weill Cornell Medicine, Cornell Vet, faculty, alumni, students, researchers).
- Sandy Weill / Citigroup → qualifies
- Daphne Koller / Coursera → qualifies
- Pre-seed two-person startup → qualifies
- A company that just *employs* Cornellians → does NOT qualify

## What Was Just Built (this session)

### Schema additions
New fields in every record: `cornellian_founder` (REQUIRED), `funding_total_usd`, `funding_stage`, `funding_last_round_year`, `founded_year`, `employee_count`, `is_public`, `headquarters`, `validation_tier`, `validation_issues`. CSV columns updated to match.

### `validate_record()` — vigorous tier-based validation
- **high**: all required fields + canonical proof URL (Wikipedia/Crunchbase/SEC/etc.) OR multi-sourced
- **provisional**: required fields present, single source
- **weak**: missing one or more required fields

Coercers normalise mixed-format funding ("$12M" → 12000000), years, booleans. Human-name regex catches placeholder founders.

### Blocklist tightened
Removed Fortune 500s (Citigroup, Goldman, etc.) from blocklist — they qualify now if a Cornellian was a founder. Blocklist now only catches: VC funds, accelerator programs, university offices, hackathons, hallucinated placeholders.

### Hard insert gate
`StartupDB.upsert()` rejects records with no `cornellian_founder` (unless `affiliation_type = "Licensed Tech"`).

### `--verify-only` mode
New CLI flag: skips discovery, walks every record, revalidates in place, runs `fill_missing_data()` on every weak/provisional record. Three-pass: validate → fill → revalidate. Cap with `--max-records N`.

### `--seed-urls` flag
Comma-separated URLs scraped first (no Google search). Currently testing with:
- `https://eship.cornell.edu/cornell-startups/high-profile-startups/`
- `https://bigredai.org/startups`

### Long-page chunking
Pages >30K chars get split into ≤4 chunks (1KB overlap) for separate Gemini calls. Per-page dedup before return.

### Other fixes this session
- Logging duplication fixed (`logging.getLogger("gemini_tool").propagate = False`)
- Strategy prompt trimmed from ~50KB to ~5KB (clipboard fast path now works)
- HTTP-empty no longer short-circuits — falls through to Selenium
- `gap_report()` returns `complete_count: 0` when DB is empty (was a KeyError on `print` summary)
- Failed Gemini JSON responses get logged to `gemini_parse_failures.log` for debugging

## Outstanding Issues — DEBUG TARGETS

### 1. Gemini's JSON output is not parseable on long extraction prompts
On the 60K-char `bigredai.org/startups` page (split into 3 chunks of ~35K each), all three Gemini extraction calls returned text that `_parse_json` couldn't decode. Root cause unknown — could be:
- Gemini wrapping output in prose despite the explicit "JSON ONLY" rule
- Gemini truncating output mid-JSON because the input was too large
- Gemini returning markdown ```json fences in some odd format

**Next step:** read `gemini_parse_failures.log` after a run to see what Gemini actually returned. The prompt was just rewritten to drop `<placeholder>` syntax in favor of plain field-type listings. Re-run and inspect.

### 2. `https://eship.cornell.edu/cornell-startups/high-profile-startups/` returns empty even via Selenium
Page seems to be JS-rendered or anti-bot-blocked. Even with the new "fall through to Selenium on empty HTTP" path, the Selenium step also returns empty. Possible fix: add `eship.cornell.edu` to `_JS_HEAVY_DOMAINS` (skip HTTP entirely), or wait longer for JS to render, or scrape via the Wayback Machine.

### 3. CAPTCHA blocks Google searches in headless mode
Round 1 worker crashes on `[CAPTCHA detected — Press Enter after solving]` because no TTY. For visible-mode runs you can solve it manually; for headless we need either residential proxies, slower request pacing, or a CAPTCHA solver.

## Test Setup (last attempted)

```bash
cd "g:/My Drive/Cornell/Spring 2026/Agents/startup_research_agent"
PYTHONUTF8=1 python startup_researcher.py \
  --headless --max-rounds 1 --output-dir startup_output_test \
  --seed-urls "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups" \
  "Find every company where AT LEAST ONE founder is a Cornellian. Include any size — startups AND Fortune 500s. Capture: company_name, cornellian_founder, founders, description, proof_url, founded_year, funding_total_usd, funding_stage, employee_count, headquarters, affiliation_evidence."
```

## Key Files
- `startup_researcher.py` — main script (~2,950 lines)
- `startup_output/` — production DB (1,010 records, schema is the OLD one — re-running will trigger re-validation)
- `startup_output_test/` — clean test runs
- `gemini_parse_failures.log` — populated when Gemini returns unparseable JSON
- `../pipelines/parcelle_pipeline/gemini_tool.py` — canonical GeminiSession (imported via sys.path)
- `browser_cookies.json` — saved LinkedIn/Crunchbase cookies (4 entries)

## Architecture Notes
- 2 parallel Selenium workers + 1 Gemini browser instance = ~3 Chromium processes
- HTTP-first scraping with Selenium fallback for JS-heavy domains
- File-backed `PageCache` (`startup_output_test/cache/<sha>.txt`)
- Checkpoint-driven: `startup_checkpoint.json` lets `--resume` continue
- Inline Gemini verify every 7 rounds (samples 20 unverified records)
- Targeted `fill_missing_data()` every 3 rounds (specific company → specific search)
