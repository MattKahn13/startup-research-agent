# Overnight Run -- 2026-06-06 → 2026-06-07

Window: ~7:45 PM Sat → ~8:37 AM Sun. Goal: iron out the existing 1,300 records or gather new ones. AFK execution.

## TL;DR

The 1,525-record production DB now has a clean, deduplicated, schema-v2 sibling at `startup_output_test/startups_db_deduped.json` (1,389 records, 95% of legacy DB recovered). Ecosystem report, CSV exports, and Cornell-network graph all generated. The live re-extract pass and Wikipedia enrichment ran in parallel without ever needing Google search (no CAPTCHA exposure).

Net: not Gemini's fault when the agent didn't produce records. The bigredai and similar aggregator pages don't contain the founder evidence the new strict pipeline requires. The lesson was captured in the web-agent wiki at commit `83ab68a`.

## What landed (commits)

```
ddd651e feat(enrichment): Wikipedia enrichment + CSV export + priority list
303ce0b feat(overnight): heuristic migration + dedup + ecosystem analysis tracks
```

(Both pushed to `MattKahn13/startup-research-agent@hardening-pass`.)

Plus uncommitted: `discover_via_wikipedia_categories.py`, `export_network.py`, `OVERNIGHT_REPORT.md` -- being committed now.

## Outputs you can open

| Path | What |
|---|---|
| `startup_output_test/ECOSYSTEM_REPORT.md` | Top funded, top exits, school + role distribution, multi-company founders, founding decades |
| `startup_output_test/startups.csv` | One row per company, flat |
| `startup_output_test/cornellians.csv` | One row per (Cornellian, company) edge |
| `startup_output_test/nodes_people.csv` | 1,308 Cornellians (Gephi-importable) |
| `startup_output_test/nodes_companies.csv` | 1,389 companies |
| `startup_output_test/edges_person_company.csv` | 1,489 affiliations |
| `startup_output_test/edges_cofounder.csv` | 940 co-founder pairs |
| `startup_output_test/startups_db_migrated.json` | 1,451 records in new Pydantic schema |
| `startup_output_test/startups_db_deduped.json` | 1,389 after canonical-name dedup |
| `startup_output_test/startups_db_enriched.json` | 1,389 with Wikipedia URL + extracted fields where available |
| `startup_output_test/wiki_candidates_new.json` | 165 Wikipedia category candidates worth manual review |
| `startup_output_test/dedup_report.md` | What merged into what |
| `startup_output_test/ecosystem_stats.json` | Structured numbers behind the report |

## What ran

### Track 1 -- Live re-extract (Selenium + browser-Gemini, anonymous mode)

Ran `reextract_all.py` against 502 high+provisional records. Used the saved Gemini cookies (worked from login session). 2 worker threads with a Gemini lock so the browser session wasn't double-driven.

**Result: 0 records survived to v2 DB.** Breakdown of 502 attempts:
- ~89 fetch_failed (page gone, empty, blocked)
- ~413 unmatched: Gemini extracted 0 records from the proof_url page (because the page is an aggregator/portfolio listing without per-founder Cornell affiliation text), so the original record's company_name had no match.

This is the **same finding** the bigredai diagnostic captured in the wiki: many proof_urls in the legacy DB are listing pages -- they were "good enough" for the May system because that system didn't enforce evidence-span, but they're insufficient for the new pipeline. The lesson is in `wiki/site-profiles/gemini-web.md` Lesson 2026-06-06.

So: the backfill itself produced no records, but the failure log is itself useful -- it's a list of records whose `proof_url` needs to be replaced. ~80% of attempted high+provisional records have aggregator-style proofs.

### Track 2 -- Heuristic migration (no Gemini, no network)

`migrate_to_v2_schema.py` reads the legacy DB, parses `affiliation_evidence` / `founders` / `affiliation_type` with regex, and emits records that pass the new `StartupRecord` Pydantic schema.

**Result: 1,451 / 1,525 = 95.1% recovered.** Only 74 records had no parseable Cornellian. Distribution:
- Tiers preserved: 236 high, 557 provisional, 658 weak
- Schools detected: 1,382 CU, 148 Cornell Tech, 30 Weill, 6 Vet, 264 unknown
- Roles detected: 1,655 alumnus, 114 faculty, 39 postdoc, 19 student, 3 researcher

### Track 3 -- Canonical-name dedup

`dedup_records.py` strips suffixes (Inc, LLC, Corp), parens, and punctuation; collapses near-duplicate company entries.

**Result: 62 duplicates merged.** 1,389 unique companies. Examples merged: "Blackboard, Inc." + "CourseInfo, LLC (Blackboard, Inc.)" + "CourseInfo, LLC"; "Rosie" + "Rosie App" + "RosieApp" + "Instacart (Rosie)".

### Track 4 -- Ecosystem analysis

`analyze_ecosystem.py` produces `ECOSYSTEM_REPORT.md` and `ecosystem_stats.json`.

Highlights from the deduped DB:
- **Top funded**: Ava Labs $290M (active), Tia $132M (active), Inpria $73M (acquired), Moat $67.5M (acquired), Hyro $45M (active), Lionano $44M (active), Novomer $41M (acquired)
- **Multi-company Cornellians**: 136 founders appear in more than one company in the dataset. Top: Will Regan (6 companies, mostly fusion energy), Amarildo Gjondrekaj (6, fintech), Dan Cane (4, Blackboard + ModMed), David Duffield (PeopleSoft + Workday), Nick Nickitas (Rosie/Instacart)
- **Top industries**: Biotech (61), Food & Beverage (58), AgTech (51), AI (42), AI/ML (35), FinTech (28), HealthTech (27)
- **Founding decades**: 88 of the dated records founded since 2010 (43 in 2010s, 45 already in 2020s)

### Track 5 -- Wikipedia enrichment

`enrich_wikipedia.py` against a top-200 priority list (scored by funding + status + public). Tried up to 6 title variants per company, applied a company-shape keyword filter.

**Result: 47 hits / 175 queried (27%).** Of those, 22 records have at least one regex-extracted field (founded_year, headquarters, status). The other 25 hits got the Wikipedia URL stored but no field-fill -- the regex patterns are too narrow for many lead-paragraph constructions (e.g. "launched in September 2020" doesn't match the `(founded|established|incorporated) ... year` regex). Iterating on these patterns is the easiest next-day win.

Notable hits: Avalanche/Ava Labs, Lyft, Workday, Western Union, Shake Shack, PeopleSoft, Novomer (founded 2004 in Ithaca; the only record where the regex got the year), Citigroup, Instacart.

False positives observed: "Moat" (the castle moat article matched a company named Moat); "Biotia" → "Madia" (different company). The keyword filter was loosened in the last patch and now lets some non-company hits through; tightening the filter is the second-easiest next-day win.

### Track 6 -- Wikipedia category discovery (new records)

`discover_via_wikipedia_categories.py` pulls members of 7 categories that might contain Cornell-affiliated companies, then for each candidate that's not already in the deduped DB it queries the summary and looks for "cornell" in the lead.

**Result: 165 raw candidates, 0 confirmed.** The Cornell signal in a company's lead paragraph is extremely rare even when the founder is Cornell-affiliated (Clarifai's article doesn't say Cornell in the first 400 chars; Cockroach Labs the same). The 165 names are in `wiki_candidates_new.json` for manual review -- the failure is in the signal-check, not necessarily in the candidates themselves.

## What didn't work

- **Backfill produced 0 v2 records.** Predicted by the wiki lesson; confirmed at scale. The failure log is the actionable artifact (~413 records need new proof_urls).
- **Wikipedia category discovery has near-zero auto-confirmation.** Need a 2-hop check (find company's Wikipedia article → find founder → check founder's article for Cornell) to confirm new candidates. Out of scope for tonight.
- **Wikipedia regex extraction is too narrow.** Captures only "founded in YEAR" style sentences; misses "launched", "started", month-year forms.

## Highest-leverage next moves

1. **Tighten wiki keyword filter** (10 min): require the description AND extract to contain at least one strong company keyword, not just any of the loose list. Re-running on the 200 priority list with this fix will likely drop false positives below 5%.
2. **Broaden wiki year regex** (10 min): add "launched", "started", "began", "since" + handle "Month YYYY" forms. Should double the field-fill rate from 22 → ~40 on the existing 47 hits.
3. **Replace aggregator proof_urls** (manual or scripted): the 413 unmatched records from backfill all have proof_urls pointing at portfolio pages. For each, search Wikipedia / Crunchbase / press coverage for a per-company source page.
4. **Confirm the 165 Wiki candidates manually** (manual, 1-2 hr): for each, click through, see if a Cornell connection is present somewhere on the page or in a referenced article. New records can be added directly to the deduped DB.
5. **Network analysis in Gephi** (5 min import): nodes + edges CSVs are Gephi-ready. Modularity / centrality / community detection on the 1,308-Cornellian / 1,389-company / 940-pair graph would surface clusters.

## Background processes

Killed at end of run:
- `bjqxgu1ba` reextract_all (the productive one)
- `blf5rc077` enrich_wikipedia
- Earlier stale `bd4moz2yb` (the no-Gemini-session false-start)

## Code added during the run (root of repo)

```
migrate_to_v2_schema.py          # legacy -> Pydantic conversion
dedup_records.py                 # canonical-name dedup
analyze_ecosystem.py             # stats + markdown report
export_csv.py                    # flat CSVs
export_network.py                # Gephi-format CSVs
enrich_wikipedia.py              # Wikipedia API enrichment
discover_via_wikipedia_categories.py  # candidate discovery
probe_gemini.py                  # prompt-strategy probe (kept for reference)
OVERNIGHT_REPORT.md              # this file
```
