# Founder Graph: candidate -> verify -> publish, with an expansion crawl

**Status:** design (brainstormed 2026-07-09, approved in shape by Matt).
**Trigger:** Prof. Matt Marx (Cornell VP for Entrepreneurship) reviewed the 1,761-record
dataset and flagged systemic false positives -- employees, executives, donors, Cornell's own
units, and VCs all recorded as "founders." Precision is ~47% (825 real founders of 1,761).

## The problem, precisely

Three systemic issues, one root:

1. **The pipeline asserts "founder" without verifying founding.** The schema has no founding
   concept -- `affiliation_type` records only the *Cornell* tie (alum/student/faculty), so any
   Cornellian mentioned near a company becomes its "founder." The evidence-span gate proves the
   text isn't hallucinated; it says nothing about the *relationship*. "verified/high-tier" only
   counts sources.
2. **No real-company check.** Nothing confirms the entity is a real external registered company.
   Cornell centers, journals (Physical Review), and programs sailed through.
3. **The engine is brittle.** The perpetual browser-Gemini scraper gave a week of crashes, a
   Chrome-leak OOM, a stuck degradation ladder, four sleep-deaths, and a resume that carried
   nothing forward. Patched, but inherently fragile.

The deeper root: **the system maximizes recall (cast the widest net, extract loosely) but the goal
is a precision-critical list defensible to a Cornell Vice Provost.** The architecture optimizes for
the opposite of what is needed.

## Core principle

**Nothing is a "founder" until verified.** The scraper emits *candidates* -- `(company, person,
cornell-tie, source, evidence)` leads. A verification gate is the ONLY door into the published
dataset. And -- the key move -- **verification also expands the discovery frontier**: authoritative
lookups surface adjacent entities (co-founders, other ventures, DBAs) that become new candidates.

## Architecture: a budgeted, deduplicated entity-graph crawler on one durable job queue

Model the world as a graph. **Nodes:** companies, people. **Edges:** founded-by, co-founder,
dba-of / alias-of, works-at, cornell-tie. Candidates are unverified nodes/edges; the published
dataset is the subgraph of *verified* founded-by edges anchored to a Cornellian.

Everything runs off ONE durable job queue. Discovery and verification are not separate stages --
they are **job types that mutually enqueue each other**, which is what produces the expansion tree.

```
                       ┌──────────────── DURABLE JOB QUEUE (DuckDB) ────────────────┐
 seeds ──► SEARCH ─────┤  {type, payload, state, priority, attempts, result}         │
                       │  workers drain it; each job may enqueue CHILD jobs           │
 (Cornellians,         │  survives crash/sleep; parallel consumers by job type        │
  directories)         └───────────────────────────────────────────────────────────┘
                              │            │             │              │
                     VERIFY_COMPANY  VERIFY_FOUNDING  CHECK_CORNELL   EXPAND_PERSON /
                       (API)           (LLM)          _TIE (LLM)      EXPAND_COMPANY
                              │            │             │              │
                              └──────► CANDIDATE STORE (state machine) ◄┘
                                             │
                                    published view = verified founders  ──►  Excel / dataset
```

### Job types (the spine)

Each is a small, independently-testable unit that reads its payload, does one thing, writes a
result, and may enqueue children.

- **SEARCH** (discovery) -- seed/query -> candidate `(company, person)` pairs. Backed by the
  existing scraper (DDG/Selenium/Gemini). Its ONLY job is finding leads; it never claims founder.
- **WIKIDATA_SEED** (discovery, API) -- SPARQL for companies whose founder studied at Cornell ->
  structured candidates with founder + founded-year. Also serves as an authoritative validator.
- **EDGAR_SEARCH** (discovery, API) -- Form D + full-text search for filings mentioning "Cornell" ->
  company + related persons -> candidates.
- **GRANT_PATENT_SEARCH** (discovery, API) -- SBIR.gov / NIH RePORTER / NSF / USPTO PatentsView for
  Cornell-affiliated PIs/inventors -> the companies they founded (the deep-tech channel).
- **VERIFY_COMPANY** (API) -- OpenCorporates / SEC EDGAR / state SoS: is it a real registered
  external entity? Pull officers/founders, incorporation date, DBAs/aliases, jurisdiction. Enqueues
  EXPAND_COMPANY and (per founder) CHECK_CORNELL_TIE.
- **VERIFY_FOUNDING** (LLM, source-aware) -- did person P found company C? Reads the cached SOURCE
  PAGE, not the thin snippet (fixes the thin-evidence + founder-name/evidence-mismatch bugs).
- **CHECK_CORNELL_TIE** (LLM/search) -- is P actually Cornell-affiliated? The anchor filter.
- **EXPAND_PERSON** (search+API) -- a confirmed **Cornellian** founder -> their OTHER ventures ->
  new company candidates. (Non-Cornellian co-founders are recorded but not expanded, to stay
  anchored.)
- **EXPAND_COMPANY** (search+API) -- company -> DBAs, subsidiaries, aliases -> dedup + enrichment,
  and corroborating "founded by X" sources.

### The expansion tree (Matt's snowball)

> NY SoS returns "ABC Inc." with two registered founders. If a founder is a Cornellian ->
> EXPAND_PERSON (DDG their name -> their other companies -> new candidates). ABC -> EXPAND_COMPANY
> (DBAs / related entities -> dedup + more corroboration). Each spawns more verification, which
> spawns more expansion.

Anchored to the Cornell tie, **bounded** by (a) a per-run job/token budget and (b) a visited-set
keyed on canonical entity IDs (see Entity resolution) so it terminates and never re-processes a
node. Authoritative registry data makes each hop *higher* precision than a scrape.

### Candidate store + state machine

Every candidate node/edge carries a state:

```
new ──► queued ──► verifying ──► verified
                              ├─► rejected(reason)    # employee/exec/donor/investor/non-company/not-real/not-cornell
                              └─► needs_human         # genuinely ambiguous -> small review queue
```

Published dataset = the view where `founding=confirmed AND company_real AND cornell_tie AND
entity_type=company`. Auditable (every rejection carries reason + evidence + source) and resumable
(state + queue are durable).

### Entity resolution / dedup

Canonical company key (normalized name + registration ID + DBA cluster) and person key (normalized
name + affiliations). This is not optional polish -- it fixes two observed bugs (one press-release
bio smeared across Goldman/Warburg/Student Agencies; the Ava Labs founder-name/evidence mismatch)
AND it is what makes the expansion crawl terminate (the visited-set).

## Verification checks (the precision gate)

- **Real-company** (API) -- OpenCorporates (registration, officers, free tier), SEC EDGAR
  (public/funded, free), state SoS (NY etc.). Kills Cornell units, journals, non-companies.
- **Founding-relationship** (LLM, source-aware) -- reads the cached page; source-tier aware
  (a curated Cornell-startup directory listing = founder; a news/LinkedIn mention needs the full
  check). This is the adjudicator already built and smoke-validated.
- **Cornell-tie** -- the anchor; only Cornell-founded companies are in scope.
- **Entity-type** -- drop VCs, foundations, accelerators, Cornell-internal units.
- **Source-tier routing** -- directory-sourced candidates get a light check; mention-sourced get
  the full battery. Saves most of the verification cost.
- **Structured-agreement shortcut** -- if two authoritative sources agree on the founding edge
  (Wikidata + OpenCorporates/EDGAR), mark it verified with NO LLM call. Only genuinely ambiguous
  edges reach the (fragile) browser-Gemini adjudicator.

## Reliability, addressed by the shape itself

- Discovery no longer calls Gemini inline -> a Gemini hang cannot crash discovery; it just leaves a
  job on the queue for a worker to retry.
- The queue + candidate state are on disk (DuckDB) -> a crash/sleep loses nothing; workers resume.
- The real-company check runs on a real API, not a browser -> shrinks the failure surface where all
  our incidents lived.
- The perpetual scraper stays (it is genuinely good at lead discovery) but becomes one SEARCH
  worker among several queue consumers. The watchdog from this week still guards the workers.

## Store

DuckDB (already the v2 plan): tables for `jobs`, `entities` (companies + people, canonicalized),
`edges` (founded-by / cofounder / dba / cornell-tie with state + evidence + source), and
`verification_results`. Queryable; ALTER-friendly during schema iteration; single embedded file.
The published dataset and the Excel are views/exports.

## Data sources (all free / free-tier -- matches Matt's zero-spend constraint)

Grouped by the role they play. Each is a worker behind the queue.

**Structured seed + validator (highest leverage):**
- **Wikidata SPARQL** -- encodes the exact edges natively: `educated at (P69) = Cornell University`
  + `founded by (P112)`. One free query returns companies whose founder studied at Cornell, with
  founder + founded-year, structured. Used BOTH as a high-precision seed AND as a validator (if
  Wikidata already asserts "X founded Y", that's authoritative corroboration). No key.

**Real-company / entity-type (verification):**
- **OpenCorporates** -- registration + officers + DBAs (free tier; NY/DE/etc.); the SoS unifier.
- **SEC EDGAR** -- free full API. **Form D** private-placement filings list the company + its
  "related persons" (founders/execs) with addresses; EDGAR full-text search finds filings mentioning
  "Cornell." Authoritative for funded startups -- a government source Marx cannot dispute.
- **GLEIF (Legal Entity Identifier)** -- free global registry; confirms a real registered entity.
- **ProPublica Nonprofit Explorer** -- free; used in REVERSE to *exclude* foundations/nonprofits.

**Deep-tech discovery channel (a whole tier the news/directory scrape misses):**
- **SBIR.gov API** + **NIH RePORTER** + **NSF Award Search** -- free; SBIR/STTR grants and research
  awards name the company + the PI. Cornell-affiliated PIs who founded a grant-funded startup.
- **USPTO PatentsView API** -- free; inventors + assignees. Cornell inventors -> the companies they
  founded to commercialize. Also Cornell-as-assignee -> licensed-tech spinouts.
- Decision (per Matt): make this a **first-class discovery source**, not a later add.

**Cornell-tie anchor + person expansion:**
- **ORCID** -- free; researcher education + employment + works (faculty/researcher founders).
- **GitHub API** -- free; tech founders with "Cornell" in bio + their company.
- **LinkedIn** (existing auth scrape) -- founder titles + "Cornell" education; high-signal, anti-bot.

**LLM:** Gemini-web via the queue now (short compact prompts, already reliable). An LLM API for
adjudication is a drop-in later -- but note the structured-agreement shortcut below removes the LLM
from the path entirely for a large fraction of records.

## Graph-native features (what the architecture uniquely enables)

- **Confidence score + provenance chain, not binary keep/drop.** Every founded-by edge carries a
  score aggregating source-tier, independent-corroboration count, API confirmation, and Cornell-tie
  strength -- plus its full evidence chain (which sources, which API hits, which adjudication). The
  published record is defensible on its face: it says *why* it's there and how sure we are.
- **Structured-agreement shortcut (reliability + cost win).** When two authoritative sources agree
  (e.g. Wikidata asserts "X founded Y" AND OpenCorporates lists X as an officer of Y), mark the edge
  verified WITHOUT an LLM call. This removes the fragile browser-Gemini step for a large fraction of
  records -- solving reliability by *not needing* the flaky path, not just hardening it.
- **Contradiction detection.** When sources disagree on the founder (SoS says Jane, scrape says
  John), flag a contradiction -> needs_human, instead of silently picking wrong. This is exactly the
  Ava-Labs bug, turned into a caught signal.
- **Queryable rejects = an instant defense.** The rejects log answers Marx's "what about Cisco?"
  with "Lew Tucker, CTO, EXECUTIVE -- excluded, here's the source." Every future challenge is a
  lookup, not a re-investigation.
- **Repeat-founder mining.** The expansion crawl surfaces Cornellians who founded 3+ companies;
  those nodes are high-yield -- prioritize expanding them. Also reveals founder teams/clusters,
  genuinely useful data for Marx's actual role.

## Migration of the existing 1,761

All existing records become candidates. Seed with the founding-adjudication already run (825
FOUNDER). Enqueue VERIFY_COMPANY (real-company) + CHECK_CORNELL_TIE + the UNCLEAR source-recovery
pass. **The first published output of the gate is the Marx deliverable** -- so the near-term
cleanup and the long-term architecture are the same work, run once vs. run continuously.

## Sequencing

1. **Marx deliverable (near-term):** finish verifying the existing candidates -- UNCLEAR
   source-recovery + real-company API check -> the clean Excel. This is the gate, run in batch.
2. **Queue + store:** stand up the DuckDB job queue, candidate state machine, entity resolution.
3. **Rewire discovery:** the scraper enqueues SEARCH candidates instead of asserting founders; wire
   the API verify workers.
4. **Turn on expansion:** EXPAND_PERSON / EXPAND_COMPANY, budgeted.
5. **Relaunch perpetual discovery** in candidate-only mode behind the gate.

## Non-goals (YAGNI)

- No paid data (OpenCorporates free tier, EDGAR, DDG, Gemini-web) -- matches the zero-spend pattern.
- No UI beyond a human-review queue (a sheet/CSV of `needs_human` candidates Matt clears in batches).
- Not rebuilding the scraper -- it becomes a SEARCH worker.

## Testing

Pure logic is unit-tested: the candidate state machine, canonical entity keys / dedup, job routing,
the budget/visited-set termination, and the `verdict -> publish` rule. The founding + real-company
checks are validated against the known cases (Marx's exact list: keep OpenEvidence/Hermeus/Sage/
Varda/Burger King; drop Amazon/Citigroup/Amex/BCG/Google/Cisco).
