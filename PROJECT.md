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
  "Ops -- supervisor watchdog": ["supervisor.py", "launch_supervisor.py"]
  "Data layer -- migrate / dedup / analyze": ["migrate_to_v2_schema.py", "dedup_records.py", "analyze_ecosystem.py", "export_csv.py", "export_network.py", "reextract_all.py", "repair_stranded_founders.py"]
  "Enrichment -- wikipedia + linkedin": ["enrich_wikipedia.py", "discover_via_wikipedia_categories.py", "linkedin_login.py", "parse_linkedin_auth.py"]
  "Probes (empirical findings)": ["probe_headed_minimized.py", "probe_linkedin.py", "probe_linkedin_auth.py", "probe_gemini.py"]
  "Specs (design)": ["docs/superpowers/specs/2026-06-07-research-agent-v2-design.md", "docs/superpowers/specs/2026-06-05-hardening-pass-design.md", "docs/superpowers/specs/2026-06-07-browser-defaults.md"]
  "Plans (implementation)": ["docs/superpowers/plans/2026-06-07-research-agent-v2-implementation.md", "docs/superpowers/plans/2026-06-05-hardening-pass-implementation.md"]
  "Reports & handoffs": ["OVERNIGHT_REPORT.md", "HANDOFF.md", "BLOCKED_NEEDS_HUMAN.md", "cornell-startups-tasks.md"]
  "Tests": ["tests/test_parse_json.py", "tests/test_schema.py", "tests/test_db_upsert.py", "tests/test_gap_fill_field_consistency.py", "tests/test_json_quote_repair.py", "tests/test_parse_json_shape_confusion.py", "tests/test_gap_fill_driver_resilience.py", "tests/test_degradation.py", "tests/test_supervisor.py", "tests/test_hard_quit.py", "tests/test_visited_log.py"]
external:
  - "~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)"
  - "~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07"
  - "~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges"
  - "~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok"
  - "https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass"
-->
_synced: 2026-07-06 19:13 UTC | HEAD: 4711ccc | status-HEAD: 4711ccc

## Status

**2026-07-06 ~00:03 UTC: the run OOM-crashed -- the `driver.quit()` Chrome leak finally exhausted
RAM. Root-caused, fixed at the source, and the watchdog auto-recovered it.** The prior run (PID
36556) and its watchdog (PID 18616) both died ~00:03; ~7.5h were lost before the hourly heartbeat
glance caught it (the machine may also have slept). Cause, from the research log: `Gemini restart
failed: MemoryError()` while the Gemini session was restarting after a hang -- the
[[silent-driver-quit-failure]] leak (quit() silently fails on Windows with WinError 6, leaving Chrome
alive) had piled up ~100+ windows over the hours until spawning one more Chrome threw MemoryError.
This is the leak graduating from cosmetic to fatal; the cross-run orphan sweep couldn't help because
those windows had a live parent. **Fixed at the source:** new `gemini_tool.hard_quit(driver)`
captures the browser + chromedriver PIDs BEFORE `quit()` and force-kills the tree (`taskkill /T /F`)
for any that survive; `quit_driver` routes through it and all four `startup_researcher.py`
worker-driver `driver.quit()` sites now call it. Backstop: the watchdog escalates `chrome-high` if a
live run's Chrome count climbs back toward the OOM zone (>=90) so a repeat is a caught warning, not a
hard crash. TDD -- 5 new tests (`test_hard_quit.py` + `chrome_alarm`), 82/82 green. Recovery was
autonomous and proved the design: relaunching the watchdog (PID 13824) detected the dead research
PID, classified the exit, relaunched research as **PID 7788** with the fixed code, and wrote the
MemoryError tail to `supervisor_escalations.jsonl` -- the exact hands-off recovery the watchdog
exists for. DB safe at 1,413 throughout (per-page save). Open follow-up: the machine sleeping kills
BOTH the run and the watchdog together (wiki lesson #1 in supervising-background-runs); the hourly
human glance is the only backstop for that -- keeping the machine awake is a Matt-side call.
**Fix validated live (2026-07-06 ~09:50):** on the resumed run (PID 7788) Chrome spiked to 109 once
(fired one `chrome-high` escalation, as designed), then teardowns pulled it back to 67 -- net BELOW
the pre-spike 84, i.e. windows are being reclaimed, not leaked. Traced every `driver.quit()` site to
confirm coverage and closed the last bare one (`run_login`, unused by the agent) in `e5068bc`; the
only `driver.quit()` left in the codebase is inside `hard_quit` itself. DB progressing (1,475).
**Metric correction (2026-07-06 ~11:00):** the watchdog's `chrome-high` was crying wolf -- it
alarmed on TOTAL `chrome.exe`, but of 102 windows only 54 belonged to the run; the other 48 were
Matt's own browser. The run's real Chrome load (~39) is healthy for its ~3 concurrent browsers.
Fixed: `supervisor.run_chrome_count(procs, pid)` counts only Chrome DESCENDED from the watched run,
and `chrome-high` alarms on that (heartbeat carries both `run_chrome` and total `chrome_procs`). So
the leak fix is confirmed working AND the alarm no longer false-fires on the user's browsing.
Watchdog relaunched (PID 25248) with the corrected metric; 84 tests green.
**Resume fix (2026-07-06 ~12:00):** the detached launcher never passed `--resume`, so every
crash/sleep relaunch started FRESH -- empty `visited_urls`, re-planned, round 1 -- re-running Gemini
extraction on already-visited pages and re-running the same searches (no duplicate DATA: the DB
dedups records and the page cache blocks re-downloads, but the extraction work was wasted, and a
fresh start's planning-phase checkpoint save even WIPED the prior `visited_urls`). Fixed: `--resume`
added to `RESEARCH_ARGV` (one continuous task -> always resume), and `run()`'s checkpoint load
hardened with `or []`/`or 0` so a partial checkpoint (round=None) resumes cleanly. Watchdog restarted
(PID 22716) so its relaunches now use `--resume`; from here restarts CONTINUE instead of repeating.
The visited history up to the 11:32 sleep was already lost to the wipe (unrecoverable), but the page
cache still prevents re-downloads so the current session only re-extracts; future restarts are
protected.
**Resume fix, part 2 (2026-07-06 ~15:10):** verifying the above on a live relaunch exposed that
`--resume` was passing correctly but STILL loaded `visited_urls: 0` -- because the checkpoint only
saves `visited_urls` at round-end, and rounds (~30-45 min) rarely complete before this machine sleeps
(~hourly), so the checkpoint stayed frozen at its 11:41 planning-phase state and every resume loaded
an empty set. Same coarse-cadence bug as the earlier per-round DB save, now in the checkpoint. Fixed
with `VisitedLog` -- an append-only, flush-per-URL log (`startup_output_overnight/visited_urls.log`),
a drop-in for the set (`in`/add/len/iter/clear; clear truncates so URL-expiry survives resume). Every
visited URL is now durable the instant it's seen, independent of round completion, so a mid-round
sleep-death resumes with the full visited set. TDD (`test_visited_log.py`), 89 green. Deployed
(research PID 18728, watchdog 24756). Root cause of the OFFLINE gaps remains the machine sleeping
(~hourly, 3 sleep-deaths so far) -- disabling sleep while plugged in is the only durable fix for THAT
and is a Matt-side setting; this fix just makes the inevitable restarts finally cheap (no repeat).

**2026-07-05 ~22:41 UTC: replaced LLM-polling supervision with a Python watchdog (`supervisor.py`).**
Babysitting the run by waking Claude every ~30 min to run five process/log commands and print a
table was giving diminishing returns -- high cost per check, low time resolution, and every failure
this project hit (3 crashes, a 45-min stuck ladder, a 65-min Gemini hang) happened BETWEEN polls and
burned dead wall-clock before anyone noticed. The watchdog inverts that: a cheap detached process
ticks every 60s (file stats + one process snapshot, no browser) and handles the mechanical 95%
itself -- clean-stop relaunch (rotates the log first), crash relaunch under a 3-in-15min loop-guard,
cross-run orphan-Chrome sweep (parent-dead roots only -- it does NOT touch the within-run
`driver.quit()` leak, whose windows have a live parent), Gemini-hang and log-freeze and
pending-CAPTCHA detection. It escalates to a human ONLY for novel crashes, crash-loops, and pending
CAPTCHAs, via `supervisor_escalations.jsonl`. Every tick writes `supervisor_status.json`, so a
check-in is now ONE Read of that heartbeat, not five commands. Relaunch is a plain `subprocess.Popen`
from the watchdog, so the kb-gate never enters the loop. Pure decision logic (exit classification,
loop-guard, orphan-set, hang detection) is TDD'd -- 10 new tests, 77/77 green. Launched detached
(PID 18616) attached to the live research run (PID 36556, NOT restarted); first sweep auto-killed 12
cross-run orphaned Chrome windows. Watchdog files live in `startup_output_overnight/`
(`supervisor.log`, `supervisor_status.json`, `supervisor_escalations.jsonl`, `supervisor.pid`).

**2026-07-05 ~08:19 UTC: relaunched as PID 36556 to keep the run going -- bumped
`launch_detached.py`'s `--max-rounds` from 30 to 500.** After the clean 2026-07-04 finish (below),
the process sat idle overnight per its own design (round budget reached, not a crash). Confirmed
zero orphaned chromedriver/python processes from this pipeline before relaunching -- the finished
run cleaned up after itself correctly. Resumed clean from the 1,278-record DB ("DB: 1278 records
loaded", 4,208 cached pages). 30 rounds took 22h50m last time; at 500 rounds the process will keep
researching for a long stretch before it would self-stop again -- relaunch again (or raise the cap
further) if it reaches the new budget.

**2026-07-04 ~18:49 UTC: PID 26892 finished CLEANLY -- hit its `--max-rounds 30` budget and
stopped itself as designed. Not a crash; this is the overnight run completing.** Final tally:
**1,278 records**, 757 verified (single-source or better), 1,625 URLs visited, 30 rounds, elapsed
22h50m from the 2026-07-03 ~19:58 UTC relaunch. Five distinct bugs were found and fixed live over
that span (see below) and none recurred after their fixes landed -- the run's last many hours were
uneventful aside from the expected, self-recovering BACKLOG blips. `launch_detached.py` writes
`startups_clean.json`, `startups.csv`, and `gap_report.json` to `startup_output_overnight/` on a
clean stop; those are ready to inspect. This dataset is still the isolated verification run (see
the three-dataset-generations decision below) -- merging it into the real 1,389-record working
dataset is the next actual step, not another relaunch.

**2026-07-03 ~19:48 UTC: PID 20552 wasn't crashed, but stuck idle for 45+ minutes with NO way to
recover on its own -- a real, distinct finding, root-caused and fixed.** A routine steady-state
check found the process alive but not progressing: DB count flat, log growth nearly stopped, CPU
barely moving. The log showed a repeating 5-minute loop of "Ladder at BACKLOG level; running
local-CPU backlog pass" -- 0 tier changes each time, since BACKLOG means "no gemini, no selenium,"
i.e. no real work is even attempted. Traced the cause: a transient Selenium fail-rate burst near
the end of Round 1 tripped the degradation ladder straight to `Level.BACKLOG`. Grepping the whole
codebase found `observe_l2_sample_success`/`observe_full_prompt_success` -- the methods that would
normally step a degraded level back down -- are **never called anywhere**. The entire ladder was a
one-way ratchet: it can only escalate (NORMAL -> DEMOTED -> SCRAPE_ONLY -> BACKLOG -> HARD_STOP),
with no wired-up recovery until the full 60-minute `HARD_STOP_AFTER_S` gives up and exits. Fixed in
`degradation.py`: `tick()` now auto-resets a degraded ladder back to NORMAL after 15 minutes
(`BACKLOG_RETRY_AFTER_S`) instead of sitting idle for the full hour, with a reset-count cap
(`MAX_RESETS_BEFORE_HARD_STOP`) so a genuinely broken environment still gives up via HARD_STOP
rather than resetting forever, and a sustained-health window (`SUSTAINED_HEALTHY_S`) so an old,
already-resolved blip doesn't count against a later, unrelated problem. TDD, 3 new tests, 67/67
green. DB was safely at 540 (Round 1: 495->540, +45, matched exactly) when the stuck process was
killed and relaunched clean as PID 26892.

**2026-07-02 ~22:33 UTC: PID 26408 died from a THIRD, genuinely different failure category --
an unhandled browser crash, not a logic bug -- root-caused, fixed, relaunched.** Three full rounds
had already completed cleanly (Round 1: 273->359, Round 2: 359->451, Round 3: 451->495), so nothing
was lost. The crash: the search browser's chromedriver process died mid-gap-fill
(`urllib3.exceptions.MaxRetryError` / `ConnectionRefusedError`, "target machine actively refused
it") while `fill_missing_data`'s loop called `google_search(driver, query)` -- a call with NO
exception handling, unlike the Gemini extraction call a few lines below it in the SAME function
(which already survives failures via its own `except Exception`). The uncaught exception propagated
through `run()` to `<module>` and killed the whole detached process. Confirmed via the full
traceback (not assumed) that this is unrelated to any of tonight's three earlier logic bugs -- it's
an external infrastructure failure (the browser itself died), the first of that category tonight.
Fixed by wrapping both `google_search` and `scrape_page` in that loop with the same
`except Exception` pattern already used a few lines below for the Gemini call, matching this
project's own locked "Degradation, not stop" principle. TDD, 2 new tests, 64/64 green.

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
- [x] **Root-cause + fix the browser-crash that killed PID 26408.** DONE 2026-07-02 ~22:40 UTC --
  see Status. `google_search`/`scrape_page` in `fill_missing_data` now survive a dead chromedriver
  the same way the Gemini call next to them already does.
- [x] **Give the degradation ladder a real recovery path.** DONE 2026-07-03 ~19:55 UTC -- see
  Status. Was a one-way ratchet to HARD_STOP (60 min); now auto-resets to NORMAL after 15 min with
  a reset-count cap. Relaunched clean as PID 26892.
- [ ] **Wire up `observe_l2_sample_success`/`observe_full_prompt_success`, or remove them.** The
  15-minute auto-reset (above) is a safe, coarse band-aid for "the ladder can't recover at all." The
  deeper fix is either to actually call these methods from the SCRAPE_ONLY/DEMOTED code paths so the
  ladder steps down gradually based on real observed recovery (matching the original design intent),
  or to delete the dead methods if graduated step-down isn't worth the complexity. Low urgency now
  that the ladder can no longer get permanently stuck, but worth deciding deliberately rather than
  leaving unreachable code in place.
- [ ] **Extend incremental per-page `db.save()` to `execute_searches_parallel`.** Found during the
  2026-07-02 ~18:12 UTC steady-state check: the main parallel round loop only calls `db.save()`
  ONCE, after the entire round finishes (in `run()`, right after `execute_searches_parallel`
  returns) -- unlike the seed-URL flow (line ~2866) and the serial flow (line ~2932), which both
  save after every page. In-memory `new_count` was climbing mid-round (confirmed: "+6 new total")
  while the on-disk file stayed flat at 273 for 30+ min, because nothing had flushed yet. Not a bug
  right now (nothing crashed), but it reopens exactly the mid-round-loss risk the "incremental save"
  fix was supposed to close everywhere -- a crash mid-round in THIS path would still lose everything
  found since the last round boundary. Fix: move `db.save()` inside the drain loop (e.g. every page,
  or every K new records) in `execute_searches_parallel`, same pattern as the other two flows.
  Deliberately NOT applied live tonight -- doing so requires killing the currently-healthy PID 26408
  mid-round, which would itself lose this round's not-yet-saved progress (the exact thing being
  fixed). Apply next time the process needs restarting anyway, or on explicit go-ahead.
- [x] **Let the overnight run complete (round 1).** DONE 2026-07-04 ~18:49 UTC -- PID 26892 hit its
  `--max-rounds 30` budget and stopped cleanly (not a crash). Final: 1,278 records, 757 verified,
  1,625 URLs visited, 30 rounds, 22h50m elapsed. See Status.
- [ ] **Run is going again** -- PID 36556, relaunched 2026-07-05 ~08:19 UTC with `--max-rounds`
  raised to 500 so it doesn't self-stop again soon. Monitor `startup_output_overnight/
  startups_db.json` count + `run_detached.log`; relaunch again if it reaches the new budget.
- [ ] **Run the data layer over the DB** (currently 1,278+ records and growing):
  `analyze_ecosystem.py`, `export_csv.py`, `export_network.py` against
  `startup_output_overnight/startups_db.json`. Report findings. Can run anytime -- doesn't require
  stopping the live process.
- [ ] **Merge tonight's fresh run into the real 1,389-record dataset.** Tonight's run
  (`startup_output_overnight/startups_db.json`, now 1,278 records) started from an EMPTY db on
  purpose (isolate the parser-fix verification from the real dataset). It is NOT additive to
  `startup_output_test/startups_db_deduped.json` (the 1,389, June 6-7) or `startup_output/
  startups_db.json` (the original 1,525, May). Dedupe-merge its records into the 1,389 the same way
  `dedup_records.py` merged the original 1,525 -- by canonical company name.
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

- **[2026-07-05] Long-run supervision is a Python watchdog (`supervisor.py`), not an LLM poll loop.**
  The default going forward: launch the research agent, then launch the watchdog
  (`python launch_supervisor.py`) which attaches to `run_detached.pid` and handles clean-stop/crash
  relaunch, orphan sweeps, and hang/CAPTCHA detection on a 60s tick. A human/LLM reads ONE file --
  `startup_output_overnight/supervisor_status.json` -- to check in, and acts only on
  `supervisor_escalations.jsonl` entries (novel crash, crash-loop, pending CAPTCHA). Do NOT go back
  to waking Claude every 30 min to run process/log commands -- that was measured to give diminishing
  returns (see Status). The watchdog relaunches via `subprocess.Popen`, so the kb-gate is not in the
  relaunch loop; `launch_detached.spawn_detached()` is the single source of the launch argv (reused
  by both the manual launcher and the watchdog -- no drift). The orphan sweep only kills parent-dead
  Chrome (cross-run leftovers); the within-run `driver.quit()` leak ([[silent-driver-quit-failure]]
  in the web-agent wiki) is a separate, still-open code fix.
- **[2026-07-03] A degraded ladder level MUST have a bounded, wired-up recovery path -- verify the
  promotion methods are actually called, don't assume they are because they exist.** Found live:
  `observe_l2_sample_success`/`observe_full_prompt_success` were defined and unit-tested in
  isolation, but never invoked by any real caller in `startup_researcher.py`. Every level above
  NORMAL was reachable but functionally a dead end short of the full 60-minute `HARD_STOP_AFTER_S`.
  `degradation.py`'s `tick()` now auto-resets to NORMAL after `BACKLOG_RETRY_AFTER_S` (15 min),
  capped by `MAX_RESETS_BEFORE_HARD_STOP` so a genuinely broken environment still gives up, with
  `SUSTAINED_HEALTHY_S` so an old resolved blip doesn't count against a later unrelated one. This is
  a deliberately coarse full-reset, not the originally-intended graduated step-down -- see the
  tracked Next-steps item to decide whether to wire up the finer-grained promotions or delete them.
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

**Ops -- supervisor watchdog**
- `supervisor.py` -- Watchdog supervisor for the detached research agent
- `launch_supervisor.py` -- Spawn supervisor

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
- `tests/test_gap_fill_driver_resilience.py` -- Regression test for an unhandled Selenium/chromedriver crash inside
- `tests/test_degradation.py`
- `tests/test_supervisor.py` -- Tests for the supervisor watchdog's pure decision logic
- `tests/test_hard_quit.py` -- Tests for the browser-process force-kill on driver teardown
- `tests/test_visited_log.py` -- Tests for VisitedLog -- crash-safe visited-URL persistence

- (external) ~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md | Gemini-web scraping profile -- 50KB prompt cliff, anonymous mode, the JSON-label-prefix lesson (2026-06-11)
- (external) ~/.claude/web-agent-skills/wiki/site-profiles/linkedin.md | LinkedIn profile -- urllib vs Selenium rungs, auth-mode voyager JSON parser, the headed-fixes-the-throttle correction
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/headless-default.md | why headed-minimized is the binding default; empirical probe 2026-06-07
- (external) ~/.claude/web-agent-skills/wiki/primitives/captcha-handoff.md | un-minimize-then-handoff pattern for interactive challenges
- (external) ~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md | the cookie-filter + no-op-login footguns; valid-data-discarded-while-pipeline-reports-ok
- (external) https://github.com/MattKahn13/startup-research-agent | remote; active work is on branch hardening-pass
<!-- /AUTO -->

## Recent log

<!-- AUTO:log -->
- 4711ccc fix(resume): crash-safe visited-URL log so --resume actually carries forward
- b255ccd docs(manifest): record the --resume fix; sync
- 40286b9 fix(resume): relaunch with --resume so restarts continue instead of repeating
- 249bc0e docs(manifest): record run-scoped chrome metric correction; sync
- 5a717ad fix(supervisor): alarm on run-scoped Chrome, not total -- total was polluted by the user's own 48 browser windows, crying wolf. run_chrome_count attributes chrome.exe to the watched run's subtree; heartbeat carries both. TDD +2.
- a730f92 docs(manifest): fix validated live (chrome reclaiming, last bare quit closed); sync
- e5068bc fix(driver): route the last bare driver.quit() (run_login) through hard_quit -- closes the leak pattern completely
- 33a39c7 docs(manifest): sync + confirm-status
- 316f1ae fix(driver): force-kill Chrome at teardown -- the quit() leak OOM-crashed the run
- bab8a11 docs(manifest): sync + confirm-status
- 5124f7d feat(ops): Python watchdog supervisor -- replaces LLM-polling babysitting
- 4983880 chore(ops): raise overnight run's round cap 30 -> 500; relaunch PID 36556 from 1278-record DB
<!-- /AUTO -->
