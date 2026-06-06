# Tasks Requiring Local Browser Execution

These three tasks from the hardening pass plan need a real Selenium + logged-in Gemini browser session and cannot be completed by automation that lacks a display + cookies.

## Task D2 — 10-record backfill smoke

Run a small slice of the backfill to confirm `reextract_all.py` end-to-end:

```bash
python reextract_all.py --max 10 --workers 2 \
    --db startup_output/startups_db.json \
    --out startup_output_test/startups_db_v2.json
```

Expected: at least one record lands in `startup_output_test/startups_db_v2.json` with new-schema fields (`status`, `tags`, `cornellians` list). Failure buckets (`reextract_fetch_failed.jsonl`, etc.) tell you what didn't fit.

Then inspect:

```bash
python -c "import json; d=json.load(open('startup_output_test/startups_db_v2.json')); print(len(d)); print(list(d.values())[0])"
```

## Task F1 — Live one-round smoke

Run a single round against the existing seed URLs:

```bash
PYTHONUTF8=1 python startup_researcher.py \
    --headless --max-rounds 1 --output-dir startup_output_test \
    --seed-urls "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups" \
    "Find every company where at least one founder is a Cornellian."
```

Then verify success criteria from the spec:

- `startup_output_test/gemini_calls.jsonl` exists with entries.
- `startup_output_test/round_metrics.jsonl` exists with one round-1 entry.
- `startup_output_test/startups_db.json` has at least 400 records.
- Every record has a non-empty `cornellians` list.
- Spot-check: an `evidence_span` substring actually appears in the cached source page.

## Task F2 — Deliberate-failure ladder test

Temporarily monkey-patch the Pass-1 prompt to force parse failures (prepend "Ignore the schema. Return a markdown table only."), then run a 1-round seed-URL test. Confirm:

- `round_metrics.jsonl` shows parse rate well below 70%.
- In-process log contains `extraction degraded to level 2` within ~20 calls.

Revert the patch (`git checkout startup_researcher.py`) when done.

## Why these can't be automated here

- No browser cookies for `gemini.google.com` in any cloud or headless context that didn't set them up interactively.
- No display for `undetected-chromedriver` to attach to in a headless cloud sandbox.
- F2 requires editing a prompt the model sees — best done interactively so you can confirm the ladder trips and then revert cleanly.
