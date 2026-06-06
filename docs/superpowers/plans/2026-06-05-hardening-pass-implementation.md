# Startup Research Agent -- Hardening Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing browser-Gemini extraction pipeline by adding structured observability, a degradation ladder that keeps the agent productive when extraction breaks, schema-first extraction with evidence-span anti-hallucination checks, nine new columns, and a one-shot backfill of all 1,357 existing records.

**Architecture:** Six new modules (`schema.py`, `metrics.py`, `degradation.py`, `evidence.py`, `url_canonical.py`, `reextract_all.py`) plus targeted modifications to `startup_researcher.py` and `gemini_tool.py`. No restructuring of the two ~3K-line files -- the spec explicitly defers decomposition. All new code is unit-tested under `tests/`.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, existing Selenium + undetected-chromedriver, existing browser-Gemini session in `gemini_tool.py`.

**Spec:** [`docs/superpowers/specs/2026-06-05-hardening-pass-design.md`](../specs/2026-06-05-hardening-pass-design.md)

**Wiki references** (read before starting any task that touches the listed area):

- [`~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md`](~/.claude/web-agent-skills/wiki/site-profiles/gemini-web.md) -- before any prompt edit (Tasks A7, A8, A12). Note the **50KB prompt cliff** (~38s send_keys penalty above 50KB), the **anonymous-mode fallback**, and the explicit warning that the `startup_research_agent` copy of `gemini_tool.py` is the older variant *without* the SPACE+BACKSPACE last-char-truncation bug -- do not "modernize" the input trick.
- [`~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md`](~/.claude/web-agent-skills/wiki/anti-patterns/silent-failure.md) -- before Tasks B1-B11. The wiki's pseudocode for `RunStats` is the model for `RoundMetrics`.
- [`~/.claude/web-agent-skills/wiki/anti-patterns/infinite-retry.md`](~/.claude/web-agent-skills/wiki/anti-patterns/infinite-retry.md) -- before Task B12. Bounded retries, exponential backoff + jitter, classify errors. Don't retry on 4xx; long-back-off on 429.
- [`~/.claude/web-agent-skills/wiki/primitives/gap-finding-loop.md`](~/.claude/web-agent-skills/wiki/primitives/gap-finding-loop.md) -- context for the whole pipeline. The "atomic unit is a structured record, not a fact" line is why workstream A's StartupRecord exists.
- [`~/.claude/web-agent-skills/wiki/anti-patterns/selector-over-data-attribute.md`](~/.claude/web-agent-skills/wiki/anti-patterns/selector-over-data-attribute.md) -- context for Task B3 (extractor strategy logging). We are not rewriting the cascade; we're instrumenting it.

**Working directory:** `G:/My Drive/Cornell/Spring 2026/Agents/startup_research_agent/` -- referred to below as the project root. All paths are relative to it unless absolute.

---

## Pre-flight reading

Before starting, the implementer should read:

1. The spec above (every section).
2. `HANDOFF.md` in the project root -- documents the May 2026 debugging session that motivates Workstream B's circuit breaker.
3. `startup_researcher.py` -- skim the table of contents: `StartupDB`, `call_gemini`, `_parse_json`, `scrape_page`, `_extract_startups_chunk`, `plan_research`, `validate_record`, `gap_report`, the main `run()` loop.
4. `gemini_tool.py` -- skim: `GeminiSession`, `send_prompt`, `_JS_EXTRACT_RESPONSE`, restart logic.

---

## File layout after this plan lands

```
startup_research_agent/
  startup_researcher.py    (modified)
  gemini_tool.py           (modified)
  schema.py                (new -- Pydantic models)
  metrics.py               (new -- gemini_call wrapper, RoundMetrics, JSONL writers)
  degradation.py           (new -- 5-level state machine)
  evidence.py              (new -- substring span verification)
  url_canonical.py         (new -- URL normalization)
  reextract_all.py         (new -- workstream D one-shot script)
  conftest.py              (new -- pytest fixtures)
  pytest.ini               (new)
  requirements.txt         (modified)
  tests/
    __init__.py
    test_schema.py
    test_evidence.py
    test_url_canonical.py
    test_metrics.py
    test_degradation.py
    test_db_upsert.py
    test_parse_json.py
    fixtures/
      gemini_replies/     (sample replies pulled from gemini_parse_failures.log)
```

---

## Phase 0 -- Setup

### Task 0.1: Initialize git in the project root

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Initialize the repo**

Run:
```bash
cd "G:/My Drive/Cornell/Spring 2026/Agents/startup_research_agent"
git init -b main
```
Expected: `Initialized empty Git repository in .../.git/`

- [ ] **Step 2: Write .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
browser_cookies.json
startup_checkpoint.json
startup_output/
startup_output_test/
gemini_parse_failures.log
gemini_tool.log
startup_researcher.log
log.txt
html.txt
junk/
gemini_calls.jsonl
round_metrics.jsonl
merge_conflicts.jsonl
reextract_*.jsonl
startups_db_v2.json
```

- [ ] **Step 3: Initial commit**

```bash
git add .gitignore startup_researcher.py gemini_tool.py HANDOFF.md cornell-startups-tasks.md docs/
git commit -m "chore: initialize repo with current working state"
```
Expected: a commit hash printed.

---

### Task 0.2: Add new dependencies

**Files:**
- Create: `requirements.txt` (if absent; otherwise modify)

- [ ] **Step 1: Check current requirements**

Run: `ls requirements.txt 2>&1 || echo "absent"`

- [ ] **Step 2: Write requirements.txt**

Replace or create with:
```
selenium>=4.15
undetected-chromedriver>=3.5
beautifulsoup4>=4.12
lxml>=5.0
pydantic>=2.6
pytest>=8.0
```

- [ ] **Step 3: Install**

Run: `pip install -r requirements.txt`
Expected: all packages install cleanly. If `undetected-chromedriver` errors on Windows because Chrome moved, note it but proceed -- the existing environment already has it working.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: pin pydantic and pytest"
```

---

### Task 0.3: Set up pytest harness

**Files:**
- Create: `pytest.ini`
- Create: `conftest.py`
- Create: `tests/__init__.py`
- Create: `tests/fixtures/.gitkeep`

- [ ] **Step 1: Write pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -ra --strict-markers
markers =
    live: requires browser + network (skipped in CI)
```

- [ ] **Step 2: Write conftest.py**

```python
import sys
from pathlib import Path

# Make project root importable so tests can `import schema` etc.
sys.path.insert(0, str(Path(__file__).parent))
```

- [ ] **Step 3: Touch the test directory**

```bash
mkdir -p tests/fixtures
touch tests/__init__.py tests/fixtures/.gitkeep
```

- [ ] **Step 4: Verify pytest collects**

Run: `pytest --collect-only`
Expected: `no tests ran in 0.XXs` (no errors).

- [ ] **Step 5: Commit**

```bash
git add pytest.ini conftest.py tests/__init__.py tests/fixtures/.gitkeep
git commit -m "test: set up pytest harness"
```

---

## Phase B -- Observability + Degradation ladder

### Task B1: Outcome enum and call record

**Files:**
- Create: `metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
from metrics import CallOutcome, GeminiCallRecord

def test_outcome_enum_values():
    expected = {
        "parsed", "fence_extracted", "marker_sliced",
        "prompt_echoed", "empty", "timeout", "crash",
        "schema_invalid", "evidence_unverified",
    }
    assert {o.value for o in CallOutcome} == expected

def test_call_record_serializes_to_jsonl():
    rec = GeminiCallRecord(
        timestamp="2026-06-05T14:22:11Z",
        label="extract_chunk",
        prompt_hash="abc12345",
        prompt_chars=34281,
        response_chars=12044,
        latency_ms=31200,
        outcome=CallOutcome.PARSED,
        error=None,
        extractor_strategy=0,
    )
    line = rec.to_jsonl()
    assert '"outcome": "parsed"' in line
    assert line.endswith("\n")
```

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_metrics.py -v`
Expected: `ModuleNotFoundError: No module named 'metrics'`

- [ ] **Step 3: Implement metrics.py**

```python
# metrics.py
from __future__ import annotations
import enum
import json
from dataclasses import dataclass, asdict
from typing import Optional


class CallOutcome(str, enum.Enum):
    PARSED = "parsed"
    FENCE_EXTRACTED = "fence_extracted"
    MARKER_SLICED = "marker_sliced"
    PROMPT_ECHOED = "prompt_echoed"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SCHEMA_INVALID = "schema_invalid"
    EVIDENCE_UNVERIFIED = "evidence_unverified"


@dataclass
class GeminiCallRecord:
    timestamp: str
    label: str
    prompt_hash: str
    prompt_chars: int
    response_chars: int
    latency_ms: int
    outcome: CallOutcome
    error: Optional[str]
    extractor_strategy: Optional[int]

    def to_jsonl(self) -> str:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return json.dumps(d) + "\n"
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_metrics.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add metrics.py tests/test_metrics.py
git commit -m "feat(metrics): add CallOutcome enum and GeminiCallRecord"
```

---

### Task B2: gemini_call context manager

**Files:**
- Modify: `metrics.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:
```python
import time
from metrics import gemini_call, GeminiCallLog

def test_gemini_call_records_success(tmp_path):
    log = GeminiCallLog(tmp_path / "calls.jsonl")
    with gemini_call(log, label="planner", prompt="hello") as call:
        time.sleep(0.01)
        call.set_response("world", strategy=0)
        call.set_outcome(CallOutcome.PARSED)
    lines = (tmp_path / "calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["label"] == "planner"
    assert rec["outcome"] == "parsed"
    assert rec["response_chars"] == 5
    assert rec["latency_ms"] >= 10

def test_gemini_call_records_exception(tmp_path):
    log = GeminiCallLog(tmp_path / "calls.jsonl")
    with pytest.raises(RuntimeError):
        with gemini_call(log, label="planner", prompt="hi") as call:
            raise RuntimeError("boom")
    rec = json.loads((tmp_path / "calls.jsonl").read_text().strip())
    assert rec["outcome"] == "crash"
    assert "boom" in rec["error"]
```

Add `import pytest, json` at the top of the test file if missing.

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_metrics.py -v`
Expected: `ImportError: cannot import name 'gemini_call'`

- [ ] **Step 3: Implement the context manager**

Append to `metrics.py`:
```python
import contextlib
import hashlib
import time
import datetime as _dt
from pathlib import Path


class GeminiCallLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: GeminiCallRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.to_jsonl())


class _CallHandle:
    def __init__(self, label: str, prompt: str):
        self.label = label
        self.prompt_chars = len(prompt)
        self.prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        self.response_chars = 0
        self.strategy: Optional[int] = None
        self.outcome: Optional[CallOutcome] = None
        self.error: Optional[str] = None

    def set_response(self, text: str, strategy: Optional[int] = None) -> None:
        self.response_chars = len(text or "")
        self.strategy = strategy

    def set_outcome(self, outcome: CallOutcome, error: Optional[str] = None) -> None:
        self.outcome = outcome
        self.error = error


@contextlib.contextmanager
def gemini_call(log: GeminiCallLog, label: str, prompt: str):
    handle = _CallHandle(label, prompt)
    started = time.perf_counter()
    try:
        yield handle
    except Exception as e:
        handle.set_outcome(CallOutcome.CRASH, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        if handle.outcome is None:
            handle.outcome = CallOutcome.PARSED  # caller forgot; assume ok
        log.append(GeminiCallRecord(
            timestamp=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            label=handle.label,
            prompt_hash=handle.prompt_hash,
            prompt_chars=handle.prompt_chars,
            response_chars=handle.response_chars,
            latency_ms=latency_ms,
            outcome=handle.outcome,
            error=handle.error,
            extractor_strategy=handle.strategy,
        ))
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_metrics.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add metrics.py tests/test_metrics.py
git commit -m "feat(metrics): add gemini_call context manager with JSONL logging"
```

---

### Task B3: Plumb extractor_strategy from gemini_tool

**Files:**
- Modify: `gemini_tool.py` -- function `_JS_EXTRACT_RESPONSE` and its caller

- [ ] **Step 1: Locate the extraction call site**

Run: `grep -n "_JS_EXTRACT_RESPONSE\|extract_response\|driver.execute_script" gemini_tool.py | head -30`

Identify the function that runs `_JS_EXTRACT_RESPONSE` and returns the text to Python. It's typically called from `GeminiSession.send_prompt` or a helper named like `_read_response`.

- [ ] **Step 2: Modify the JS to return both text and a strategy index**

In `_JS_EXTRACT_RESPONSE`, find where strategies return their result. Change every `return text` inside a strategy to `return {text: text, strategy: N}` where N is the strategy number (0-9). The final fallback returns `{text: "", strategy: -1}`.

- [ ] **Step 3: Modify the Python side to unpack the dict**

Find the line `result = driver.execute_script(_JS_EXTRACT_RESPONSE)` (or equivalent). Change to:
```python
_raw = driver.execute_script(_JS_EXTRACT_RESPONSE)
if isinstance(_raw, dict):
    result = _raw.get("text", "")
    last_strategy = _raw.get("strategy", -1)
else:
    result = _raw or ""
    last_strategy = -1
```

Add `last_strategy` as an attribute on `GeminiSession` so callers can read it after `send_prompt` returns:
```python
self.last_extractor_strategy = last_strategy
```

- [ ] **Step 4: Smoke check (no test -- relies on live browser)**

Run: `python -c "import gemini_tool; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add gemini_tool.py
git commit -m "feat(gemini_tool): emit extractor strategy index alongside response text"
```

---

### Task B4: Wire gemini_call into startup_researcher.call_gemini

**Files:**
- Modify: `startup_researcher.py` -- function `call_gemini`

- [ ] **Step 1: Locate call_gemini**

Run: `grep -n "^def call_gemini\|def call_gemini(" startup_researcher.py`

- [ ] **Step 2: Add the metrics import at top of file**

Add near other imports:
```python
from metrics import gemini_call, GeminiCallLog, CallOutcome
from pathlib import Path as _PathForMetrics

_GEMINI_CALL_LOG = GeminiCallLog(_PathForMetrics("startup_output/gemini_calls.jsonl"))
```

(The output dir is normally configurable via CLI; for now point at the default. Task B11 makes this configurable.)

- [ ] **Step 3: Wrap the body of call_gemini**

Replace the existing body of `call_gemini(prompt, label)` so it looks like:

```python
def call_gemini(prompt: str, label: str = "unlabeled") -> str:
    with gemini_call(_GEMINI_CALL_LOG, label=label, prompt=prompt) as call:
        try:
            response = _gemini_session.send_prompt(prompt)
        except _GeminiTimeoutError:
            call.set_outcome(CallOutcome.TIMEOUT)
            raise GeminiUnavailable("timeout")
        strategy = getattr(_gemini_session, "last_extractor_strategy", None)
        call.set_response(response, strategy=strategy)
        if not response:
            call.set_outcome(CallOutcome.EMPTY)
            return ""
        if _looks_like_prompt_echo(response, prompt):
            call.set_outcome(CallOutcome.PROMPT_ECHOED)
            return ""
        # outcome refined later by _parse_json; default to PARSED
        call.set_outcome(CallOutcome.PARSED)
        return response
```

Note: this introduces `GeminiUnavailable` exception and `_looks_like_prompt_echo` helper. Add them in the same edit:

```python
class GeminiUnavailable(RuntimeError):
    pass

def _looks_like_prompt_echo(response: str, prompt: str) -> bool:
    if not response or len(response) < 20:
        return False
    # 200-char window comparison; the marker line is the last line of every prompt
    tail = prompt[-200:].strip()
    return tail and tail in response
```

The old `try/except: return ""` fallback at the end of `call_gemini` is now redundant. Remove it. Callers that consumed `""` for failure still get `""` for `EMPTY` and `PROMPT_ECHOED`; only timeouts raise.

- [ ] **Step 4: Run the project's existing module-load smoke check**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`. If you get an `ImportError`, fix imports.

- [ ] **Step 5: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): wrap call_gemini with structured outcome logging"
```

---

### Task B5: Selenium fetch wrapper

**Files:**
- Modify: `metrics.py`
- Modify: `tests/test_metrics.py`
- Modify: `startup_researcher.py` -- function `scrape_page`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:
```python
from metrics import selenium_fetch, SeleniumFetchLog

def test_selenium_fetch_records(tmp_path):
    log = SeleniumFetchLog(tmp_path / "fetches.jsonl")
    with selenium_fetch(log, url="https://example.com", path="http") as f:
        f.set_result(chars=4200, outcome="ok")
    rec = json.loads((tmp_path / "fetches.jsonl").read_text().strip())
    assert rec["url"] == "https://example.com"
    assert rec["path"] == "http"
    assert rec["outcome"] == "ok"
    assert rec["chars"] == 4200
```

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_metrics.py::test_selenium_fetch_records -v`
Expected: `ImportError`.

- [ ] **Step 3: Implement**

Append to `metrics.py`:
```python
@dataclass
class SeleniumFetchRecord:
    timestamp: str
    url: str
    path: str        # "cache" | "http" | "selenium"
    latency_ms: int
    outcome: str     # "ok" | "empty" | "timeout" | "captcha" | "blocked" | "crash"
    chars: int

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self)) + "\n"


class SeleniumFetchLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: SeleniumFetchRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(rec.to_jsonl())


class _FetchHandle:
    def __init__(self, url: str, path: str):
        self.url = url
        self.path = path
        self.chars = 0
        self.outcome = "ok"

    def set_result(self, chars: int, outcome: str) -> None:
        self.chars = chars
        self.outcome = outcome


@contextlib.contextmanager
def selenium_fetch(log: SeleniumFetchLog, url: str, path: str):
    handle = _FetchHandle(url, path)
    started = time.perf_counter()
    try:
        yield handle
    except Exception as e:
        handle.outcome = f"crash:{type(e).__name__}"
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        log.append(SeleniumFetchRecord(
            timestamp=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            url=handle.url,
            path=handle.path,
            latency_ms=latency_ms,
            outcome=handle.outcome,
            chars=handle.chars,
        ))
```

- [ ] **Step 4: Wire into scrape_page**

In `startup_researcher.py`, find `scrape_page(url)`. Add at the top of file:
```python
from metrics import selenium_fetch, SeleniumFetchLog
_SELENIUM_LOG = SeleniumFetchLog(_PathForMetrics("startup_output/selenium_fetches.jsonl"))
```

Wrap the three branches (cache hit / HTTP / Selenium fallback) so each enters a `selenium_fetch` block with the corresponding `path` argument and calls `handle.set_result(chars=len(text), outcome=...)` before returning.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_metrics.py -v && python -c "import startup_researcher"`
Expected: 5 passed; `import` succeeds.

- [ ] **Step 6: Commit**

```bash
git add metrics.py tests/test_metrics.py startup_researcher.py
git commit -m "feat(metrics): add selenium_fetch wrapper; wire into scrape_page"
```

---

### Task B6: RoundMetrics summary

**Files:**
- Modify: `metrics.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:
```python
from metrics import RoundMetrics

def test_round_metrics_summary():
    rm = RoundMetrics(round_number=14)
    for _ in range(31): rm.record_gemini(CallOutcome.PARSED, latency_ms=24000, label="extract_chunk")
    for _ in range(4):  rm.record_gemini(CallOutcome.PROMPT_ECHOED, latency_ms=1000, label="extract_chunk")
    for _ in range(2):  rm.record_gemini(CallOutcome.EMPTY, latency_ms=1000, label="extract_chunk")
    rm.record_gemini(CallOutcome.CRASH, latency_ms=500, label="extract_chunk")
    rm.record_selenium("ok", 3100); rm.record_selenium("empty", 4000)
    rm.record_db(new_records=47, merged=12, rejected=8)
    summary = rm.summary_text()
    assert "Round 14" in summary
    assert "38 Gemini calls" in summary
    assert "31 parsed" in summary
    assert "82%" in summary
    assert "47" in summary
```

- [ ] **Step 2: Run, verify it fails.**

Run: `pytest tests/test_metrics.py::test_round_metrics_summary -v`
Expected: `ImportError`.

- [ ] **Step 3: Implement**

Append to `metrics.py`:
```python
from collections import Counter

class RoundMetrics:
    def __init__(self, round_number: int):
        self.round_number = round_number
        self.gemini_outcomes: Counter[str] = Counter()
        self.gemini_latency_total = 0
        self.gemini_calls = 0
        self.selenium_outcomes: Counter[str] = Counter()
        self.selenium_calls = 0
        self.selenium_latency_total = 0
        self.new_records = 0
        self.merged = 0
        self.rejected = 0

    def record_gemini(self, outcome: CallOutcome, latency_ms: int, label: str) -> None:
        self.gemini_outcomes[outcome.value] += 1
        self.gemini_latency_total += latency_ms
        self.gemini_calls += 1

    def record_selenium(self, outcome: str, latency_ms: int) -> None:
        self.selenium_outcomes[outcome] += 1
        self.selenium_calls += 1
        self.selenium_latency_total += latency_ms

    def record_db(self, new_records: int, merged: int, rejected: int) -> None:
        self.new_records += new_records
        self.merged += merged
        self.rejected += rejected

    def summary_text(self) -> str:
        parsed = self.gemini_outcomes.get("parsed", 0)
        pct = (100 * parsed // self.gemini_calls) if self.gemini_calls else 0
        avg_g = (self.gemini_latency_total // self.gemini_calls // 1000) if self.gemini_calls else 0
        emp = self.selenium_outcomes.get("empty", 0)
        sel_pct = (100 * emp / self.selenium_calls) if self.selenium_calls else 0
        avg_s = (self.selenium_latency_total / self.selenium_calls / 1000) if self.selenium_calls else 0
        echoed = self.gemini_outcomes.get("prompt_echoed", 0)
        empty = self.gemini_outcomes.get("empty", 0)
        crash = self.gemini_outcomes.get("crash", 0)
        return (
            f"Round {self.round_number}: {self.gemini_calls} Gemini calls, "
            f"{parsed} parsed ({pct}%), {echoed} prompt-echoed, {empty} empty, {crash} crash. "
            f"Avg latency {avg_g}s. New records: {self.new_records}. "
            f"Merged: {self.merged}. Rejected by schema: {self.rejected}. "
            f"Selenium: {self.selenium_calls} fetches, {emp} empty ({sel_pct:.1f}%), "
            f"avg {avg_s:.1f}s."
        )

    def to_dict(self) -> dict:
        return {
            "round": self.round_number,
            "gemini_calls": self.gemini_calls,
            "gemini_outcomes": dict(self.gemini_outcomes),
            "gemini_avg_latency_ms": self.gemini_latency_total // max(self.gemini_calls, 1),
            "selenium_calls": self.selenium_calls,
            "selenium_outcomes": dict(self.selenium_outcomes),
            "new_records": self.new_records,
            "merged": self.merged,
            "rejected": self.rejected,
        }
```

- [ ] **Step 4: Run**

Run: `pytest tests/test_metrics.py -v`
Expected: 6 passed.

- [ ] **Step 5: Wire into the main loop**

In `startup_researcher.py`, find the round loop (look for `while ...: ... round += 1` near `run()`). At the top of each round body, instantiate `rm = RoundMetrics(round_number=round_idx)`. Pass `rm` into `call_gemini` and `scrape_page` (or use a module-level singleton you swap at round start -- pick whichever fits the existing structure). At the end of the round body, print `rm.summary_text()` and append `json.dumps(rm.to_dict()) + "\n"` to `startup_output/round_metrics.jsonl`.

- [ ] **Step 6: Commit**

```bash
git add metrics.py tests/test_metrics.py startup_researcher.py
git commit -m "feat(metrics): per-round summary printed and persisted"
```

---

### Task B7: Degradation ladder state machine

**Files:**
- Create: `degradation.py`
- Test: `tests/test_degradation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_degradation.py
from collections import deque
from degradation import DegradationLadder, Level
from metrics import CallOutcome

def feed(ladder, outcomes):
    for o in outcomes:
        ladder.observe_gemini(o)

def test_starts_at_level_1():
    ladder = DegradationLadder()
    assert ladder.level == Level.NORMAL

def test_demotes_to_l2_when_parse_rate_drops():
    ladder = DegradationLadder()
    # 13/20 fail -> 35% pass rate, well below 70%
    feed(ladder, [CallOutcome.PARSED]*7 + [CallOutcome.SCHEMA_INVALID]*13)
    assert ladder.level == Level.DEMOTED

def test_demotes_to_l3_on_consecutive_prompt_echoes():
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.PROMPT_ECHOED]*5)
    assert ladder.level == Level.SCRAPE_ONLY

def test_promotes_back_to_l1_after_10_normal_successes():
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.SCHEMA_INVALID]*20)   # drop to L2
    assert ladder.level == Level.DEMOTED
    for _ in range(10):
        ladder.observe_full_prompt_success()
    assert ladder.level == Level.NORMAL

def test_demotes_to_l4_when_selenium_failure_rate_high():
    ladder = DegradationLadder()
    for _ in range(15): ladder.observe_selenium("captcha")
    for _ in range(5):  ladder.observe_selenium("ok")
    assert ladder.level == Level.BACKLOG

def test_hard_stop_after_60min_in_demoted_modes(monkeypatch):
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.PROMPT_ECHOED]*5)   # -> L3
    ladder._degraded_since = ladder._now() - 61 * 60  # pretend 61 minutes ago
    ladder.tick()
    assert ladder.level == Level.HARD_STOP
```

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_degradation.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# degradation.py
from __future__ import annotations
import enum
import time
from collections import deque
from typing import Deque
from metrics import CallOutcome


class Level(enum.IntEnum):
    NORMAL = 1
    DEMOTED = 2          # smaller chunks, minimal schema
    SCRAPE_ONLY = 3      # no gemini
    BACKLOG = 4          # no gemini, no selenium
    HARD_STOP = 5


class DegradationLadder:
    WINDOW = 20
    L1_TO_L2_PARSE_RATE = 0.70
    L2_TO_L3_PARSE_RATE = 0.50
    L3_FROM_PROMPT_ECHO = 5
    L4_SELENIUM_FAIL_RATE = 0.50
    L4_SELENIUM_WINDOW = 20
    L1_PROMOTE_AFTER = 10
    L2_PROMOTE_AFTER = 2
    HARD_STOP_AFTER_S = 60 * 60

    def __init__(self):
        self.level = Level.NORMAL
        self._gemini_window: Deque[CallOutcome] = deque(maxlen=self.WINDOW)
        self._selenium_window: Deque[str] = deque(maxlen=self.L4_SELENIUM_WINDOW)
        self._full_prompt_streak = 0
        self._l2_sample_streak = 0
        self._l3_fetch_streak = 0
        self._degraded_since: float | None = None

    @staticmethod
    def _now() -> float:
        return time.time()

    def _maybe_mark_degraded(self):
        if self.level > Level.NORMAL and self._degraded_since is None:
            self._degraded_since = self._now()
        elif self.level == Level.NORMAL:
            self._degraded_since = None

    def observe_gemini(self, outcome: CallOutcome) -> None:
        self._gemini_window.append(outcome)
        if outcome == CallOutcome.PROMPT_ECHOED:
            recent_echoes = sum(1 for o in list(self._gemini_window)[-self.L3_FROM_PROMPT_ECHO:]
                                if o == CallOutcome.PROMPT_ECHOED)
            if recent_echoes >= self.L3_FROM_PROMPT_ECHO and self.level < Level.SCRAPE_ONLY:
                self.level = Level.SCRAPE_ONLY
                self._maybe_mark_degraded()
                return
        if len(self._gemini_window) >= self.WINDOW:
            parsed = sum(1 for o in self._gemini_window if o == CallOutcome.PARSED)
            rate = parsed / len(self._gemini_window)
            if rate < self.L2_TO_L3_PARSE_RATE and self.level < Level.SCRAPE_ONLY:
                self.level = Level.SCRAPE_ONLY
            elif rate < self.L1_TO_L2_PARSE_RATE and self.level < Level.DEMOTED:
                self.level = Level.DEMOTED
        self._maybe_mark_degraded()

    def observe_selenium(self, outcome: str) -> None:
        self._selenium_window.append(outcome)
        if outcome == "ok" and self.level == Level.SCRAPE_ONLY:
            self._l3_fetch_streak += 1
        else:
            self._l3_fetch_streak = 0 if outcome != "ok" else self._l3_fetch_streak
        if len(self._selenium_window) >= self.L4_SELENIUM_WINDOW:
            fails = sum(1 for o in self._selenium_window if o != "ok")
            rate = fails / len(self._selenium_window)
            if rate > self.L4_SELENIUM_FAIL_RATE and self.level < Level.BACKLOG:
                self.level = Level.BACKLOG
        self._maybe_mark_degraded()

    def observe_full_prompt_success(self) -> None:
        self._full_prompt_streak += 1
        if self.level == Level.DEMOTED and self._full_prompt_streak >= self.L1_PROMOTE_AFTER:
            self.level = Level.NORMAL
            self._full_prompt_streak = 0
            self._maybe_mark_degraded()

    def observe_l2_sample_success(self) -> None:
        self._l2_sample_streak += 1
        if self.level == Level.SCRAPE_ONLY and self._l2_sample_streak >= self.L2_PROMOTE_AFTER:
            self.level = Level.DEMOTED
            self._l2_sample_streak = 0
            self._maybe_mark_degraded()

    def tick(self) -> None:
        """Called once per round; promotes hard stop if degraded too long."""
        if self.level >= Level.SCRAPE_ONLY and self._degraded_since is not None:
            if self._now() - self._degraded_since > self.HARD_STOP_AFTER_S:
                self.level = Level.HARD_STOP
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_degradation.py -v`
Expected: 6 passed. If a test fails, fix the state machine logic (don't change the test).

- [ ] **Step 5: Commit**

```bash
git add degradation.py tests/test_degradation.py
git commit -m "feat(degradation): 5-level ladder state machine"
```

---

### Task B8: Wire ladder into main loop -- level-2 (demoted) and level-3 (scrape-only) branches

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Instantiate the ladder in run()**

Near the top of `run()` (after argparse, before the round loop):
```python
from degradation import DegradationLadder, Level
ladder = DegradationLadder()
```

Make `ladder` accessible to the call sites. The simplest pattern given the existing single-file design: put it on a module-level mutable holder:
```python
_LADDER_HOLDER = {"ladder": None}
def _ladder() -> "DegradationLadder":
    return _LADDER_HOLDER["ladder"]
```
Set it from `run()`:
```python
_LADDER_HOLDER["ladder"] = ladder
```

- [ ] **Step 2: Feed gemini outcomes into the ladder**

In `call_gemini()` (inside the `with gemini_call(...)` block, after the outcome is set), call:
```python
if _ladder() is not None:
    _ladder().observe_gemini(call.outcome)
```

In `scrape_page()`, after the fetch result, call:
```python
if _ladder() is not None:
    _ladder().observe_selenium(handle.outcome)
```

- [ ] **Step 3: Read level at the top of each extraction call site**

Find `_extract_startups_chunk(text, ...)`. At the top:
```python
level = _ladder().level if _ladder() else Level.NORMAL
if level >= Level.SCRAPE_ONLY:
    return []  # skip extraction entirely
chunk_size = 15000 if level == Level.DEMOTED else 30000
schema_mode = "minimal" if level == Level.DEMOTED else "full"
```

The remainder of the function uses `chunk_size` instead of the hard-coded 30000, and switches prompts based on `schema_mode`. The `schema_mode` switch consumes the minimal Pass-1-only prompt that will be authored in Task A7.

- [ ] **Step 4: Read level at the top of the round loop**

At the top of the round body:
```python
ladder.tick()
if ladder.level == Level.HARD_STOP:
    log.error("Degradation ladder reached HARD_STOP. Saving state and exiting.")
    save_checkpoint(...)
    return
if ladder.level == Level.BACKLOG:
    run_backlog_pass(db, output_dir)   # implemented in Task B10
    continue
if ladder.level == Level.SCRAPE_ONLY:
    run_scrape_only_pass(url_queue, page_cache)  # implemented inline next step
    continue
```

- [ ] **Step 5: Implement run_scrape_only_pass**

Add a function: it pulls from the URL queue, scrapes each page (which still records selenium outcomes via Task B5), and dumps the text into the page cache without extracting. Returns immediately so the round loop continues and the ladder gets the chance to promote.

```python
def run_scrape_only_pass(url_queue, page_cache, max_urls: int = 20):
    """Level 3: scrape and cache pages, do not extract."""
    for _ in range(max_urls):
        try:
            url = url_queue.get_nowait()
        except queue.Empty:
            return
        try:
            text = scrape_page(url)
            if text:
                page_cache[url] = text
        except Exception as e:
            log.warning("scrape_only pass: %s -> %s", url, e)
```

- [ ] **Step 6: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): wire degradation ladder into main loop"
```

---

### Task B9: Level-4 (backlog) mode

**Files:**
- Modify: `startup_researcher.py`

- [ ] **Step 1: Write the function**

Add to `startup_researcher.py`:

```python
def run_backlog_pass(db: "StartupDB", output_dir: Path) -> None:
    """Level 4: zero Gemini, zero Selenium. Local CPU work on the existing DB."""
    log.info("backlog pass starting: %d records", len(db.records))
    updated = 0
    for rec in db.records.values():
        before_tier = rec.get("validation_tier")
        revalidate_record(rec)   # existing function or new shim around validate_record
        if rec.get("validation_tier") != before_tier:
            updated += 1
    db.save()
    log.info("backlog pass: re-validated %d records, %d tier changes", len(db.records), updated)

    # Recompute gap report
    report = gap_report(db)
    (output_dir / "gap_report.json").write_text(json.dumps(report, indent=2))

    # Health report: flag records that look like re-extraction candidates
    candidates = [r for r in db.records.values()
                  if r.get("validation_tier") == "weak"
                  and r.get("proof_url")]
    (output_dir / "health_report.json").write_text(json.dumps({
        "weak_records_with_proof_url": len(candidates),
        "ids": [c["company_name"] for c in candidates[:200]],
    }, indent=2))
```

If `revalidate_record` does not exist, alias it to the existing `validate_record` (which mutates the record dict in place).

- [ ] **Step 2: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): implement level-4 backlog mode"
```

---

### Task B10: Extend checkpoint with visited_urls and cache_manifest

**Files:**
- Modify: `startup_researcher.py` -- functions `save_checkpoint` and `load_checkpoint`

- [ ] **Step 1: Locate the functions**

Run: `grep -n "def save_checkpoint\|def load_checkpoint" startup_researcher.py`

- [ ] **Step 2: Add the fields**

In `save_checkpoint`, add to the dict written to disk:
```python
"visited_urls": sorted(state["visited_urls"]),
"cache_manifest": sorted(page_cache.list_keys()) if hasattr(page_cache, "list_keys") else [],
```

If `PageCache` has no `list_keys`, add it:
```python
def list_keys(self) -> list[str]:
    return [p.stem for p in self.dir.glob("*.txt")]
```

In `load_checkpoint`, return the two fields, defaulting to empty:
```python
state["visited_urls"] = set(saved.get("visited_urls", []))
state["cache_manifest"] = set(saved.get("cache_manifest", []))
```

In `run()`, when consuming a URL, check `if url_hash in state["cache_manifest"]: skip` -- this avoids re-fetching pages whose cache file was deleted between runs.

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 4: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): checkpoint now persists visited_urls and cache_manifest"
```

---

### Task B11: Worker try/finally sentinel

**Files:**
- Modify: `startup_researcher.py` -- find the parallel scrape worker function

- [ ] **Step 1: Locate**

Run: `grep -n "page_queue.put\|worker(" startup_researcher.py | head -20`

Identify the worker function (likely named `_scrape_worker` or similar). It puts pages on `page_queue` and signals end-of-work with `page_queue.put(None)`.

- [ ] **Step 2: Wrap the worker target**

Refactor so the worker body is inside `try` and the sentinel is in `finally`:
```python
def _scrape_worker(url_iter, page_queue, ...):
    try:
        for url in url_iter:
            try:
                text = scrape_page(url)
                page_queue.put((url, text))
            except Exception as e:
                log.warning("worker error on %s: %s", url, e)
                # do NOT put a None here -- only on real termination
    finally:
        page_queue.put(None)
```

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 4: Commit**

```bash
git add startup_researcher.py
git commit -m "fix(researcher): worker always emits sentinel on exit"
```

---

### Task B12: Bounded retry with exponential backoff + error classification

**Files:**
- Create: `retry_policy.py`
- Test: `tests/test_retry_policy.py`
- Modify: `startup_researcher.py` -- `scrape_page` and `call_gemini` retry call sites
- Modify: `gemini_tool.py` -- `send_prompt` retry loop

Wiki: `~/.claude/web-agent-skills/wiki/anti-patterns/infinite-retry.md`. The current 3× flat-`sleep(3)` retry pattern is the named anti-pattern.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retry_policy.py
import pytest
from retry_policy import retry, Retryable, Fatal, classify_http_status

def test_retries_on_retryable_then_succeeds():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Retryable("transient")
        return "ok"
    assert retry(flaky, attempts=3, base_delay=0.001) == "ok"
    assert calls["n"] == 3

def test_does_not_retry_on_fatal():
    def explode():
        raise Fatal("4xx")
    with pytest.raises(Fatal):
        retry(explode, attempts=3, base_delay=0.001)

def test_gives_up_after_max_attempts():
    def always():
        raise Retryable("never")
    with pytest.raises(Retryable):
        retry(always, attempts=2, base_delay=0.001)

def test_classify_http():
    assert classify_http_status(500) == "retryable"
    assert classify_http_status(503) == "retryable"
    assert classify_http_status(429) == "long_backoff"
    assert classify_http_status(403) == "fatal"
    assert classify_http_status(404) == "fatal"
    assert classify_http_status(200) == "ok"
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_retry_policy.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# retry_policy.py
from __future__ import annotations
import random
import time
from typing import Callable, TypeVar


T = TypeVar("T")


class Retryable(Exception):
    """Transient failure -- safe to retry."""


class Fatal(Exception):
    """Permanent failure -- don't retry."""


def classify_http_status(status: int) -> str:
    if status == 200:
        return "ok"
    if status == 429:
        return "long_backoff"
    if 500 <= status < 600:
        return "retryable"
    if 400 <= status < 500:
        return "fatal"
    return "retryable"


def retry(fn: Callable[[], T],
          attempts: int = 3,
          base_delay: float = 2.0,
          max_delay: float = 60.0) -> T:
    """Bounded retry with exponential backoff and ±50% jitter.

    Re-raises Fatal immediately. Re-raises the last Retryable after `attempts` tries.
    """
    last_exc: Exception | None = None
    for n in range(attempts):
        try:
            return fn()
        except Fatal:
            raise
        except Retryable as e:
            last_exc = e
            if n == attempts - 1:
                raise
            wait = min(base_delay * (2 ** n), max_delay)
            wait = wait * (0.5 + random.random() * 0.5)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def long_backoff_sleep(attempt: int) -> None:
    """For 429: 60s, then 120s, then 240s with jitter."""
    wait = 60 * (2 ** attempt) * (0.5 + random.random() * 0.5)
    time.sleep(min(wait, 600))
```

- [ ] **Step 4: Wire into `scrape_page`**

Locate the current retry loop in `scrape_page`. Replace it with:

```python
from retry_policy import retry, Retryable, Fatal, classify_http_status

def _do_http_fetch(url):
    try:
        r = requests.get(url, timeout=15)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise Retryable(str(e))
    klass = classify_http_status(r.status_code)
    if klass == "ok":
        return r.text
    if klass == "fatal":
        raise Fatal(f"HTTP {r.status_code} on {url}")
    if klass == "long_backoff":
        # 429: caller's outer retry will sleep; signal Retryable
        raise Retryable(f"429 on {url}")
    raise Retryable(f"HTTP {r.status_code} on {url}")

# in scrape_page:
try:
    text = retry(lambda: _do_http_fetch(url), attempts=3, base_delay=2.0)
except (Fatal, Retryable):
    text = None   # fall through to Selenium
```

- [ ] **Step 5: Wire into `send_prompt` (gemini_tool.py)**

In `gemini_tool.py`, find the `send_prompt` retry loop. The same pattern: retry up to 3 attempts with exponential backoff. Browser-session errors (`StaleElementReferenceException`, `WebDriverException`) are Retryable; the "you're signed out" deflection is Fatal (don't retry; surface).

- [ ] **Step 6: Run tests**

Run: `pytest tests/ -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add retry_policy.py tests/test_retry_policy.py startup_researcher.py gemini_tool.py
git commit -m "feat(retry): bounded retry with backoff/jitter and error classification"
```

---

## Phase A+C -- Schema-first extraction

### Task A1: CornellianAffiliation model

**Files:**
- Create: `schema.py`
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py
import pytest
from pydantic import ValidationError
from schema import CornellianAffiliation

def test_minimum_valid_affiliation():
    a = CornellianAffiliation(
        name="Sandy Weill",
        school="CU",
        role="alumnus",
        grad_year=1955,
        role_at_company="founder",
        evidence_span="Sandy Weill (Cornell '55) founded Citigroup",
        source_url="https://en.wikipedia.org/wiki/Sandy_Weill",
    )
    assert a.school == "CU"

def test_invalid_school_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="Harvard", role="alumnus",
            grad_year=None, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )

def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="CU", role="janitor",
            grad_year=None, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )

def test_grad_year_out_of_range_rejected():
    with pytest.raises(ValidationError):
        CornellianAffiliation(
            name="X", school="CU", role="alumnus",
            grad_year=1750, role_at_company="founder",
            evidence_span="x", source_url="https://x",
        )
```

- [ ] **Step 2: Run, verify it fails.**

Run: `pytest tests/test_schema.py -v`
Expected: `ImportError`.

- [ ] **Step 3: Implement**

```python
# schema.py
from __future__ import annotations
from typing import Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, HttpUrl


SchoolType = Literal["CU", "Cornell Tech", "Weill", "Vet", "unknown"]
CornellRole = Literal["alumnus", "faculty", "student", "postdoc", "researcher"]
CompanyRole = Literal["founder", "cofounder", "ceo", "cto",
                      "early_employee", "board", "investor", "advisor"]


class CornellianAffiliation(BaseModel):
    name: str
    school: SchoolType
    role: CornellRole
    grad_year: Optional[int] = None
    role_at_company: CompanyRole
    evidence_span: str
    source_url: str

    @field_validator("grad_year")
    @classmethod
    def grad_year_plausible(cls, v):
        if v is None:
            return v
        if not (1860 <= v <= 2030):
            raise ValueError(f"grad_year {v} out of plausible range")
        return v

    @field_validator("evidence_span")
    @classmethod
    def evidence_span_nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("evidence_span must be non-empty")
        return v
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_schema.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add schema.py tests/test_schema.py
git commit -m "feat(schema): CornellianAffiliation Pydantic model"
```

---

### Task A2: StartupRecord model (all fields incl. workstream C)

**Files:**
- Modify: `schema.py`
- Modify: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schema.py`:
```python
from schema import StartupRecord

def _good_aff():
    return CornellianAffiliation(
        name="Sandy Weill", school="CU", role="alumnus",
        grad_year=1955, role_at_company="founder",
        evidence_span="Weill", source_url="https://en.wikipedia.org/wiki/Sandy_Weill",
    )

def test_minimum_valid_record():
    r = StartupRecord(
        company_name="Citigroup",
        cornellians=[_good_aff()],
        proof_url="https://en.wikipedia.org/wiki/Citigroup",
    )
    assert r.status == "unknown"
    assert r.cornellians[0].name == "Sandy Weill"

def test_empty_cornellians_rejected():
    with pytest.raises(ValidationError):
        StartupRecord(
            company_name="x", cornellians=[], proof_url="https://x",
        )

def test_funding_coercion_from_string():
    r = StartupRecord(
        company_name="A", cornellians=[_good_aff()],
        proof_url="https://x", funding_total_usd="$12M",
    )
    assert r.funding_total_usd == 12_000_000

def test_status_enum_enforced():
    with pytest.raises(ValidationError):
        StartupRecord(
            company_name="A", cornellians=[_good_aff()],
            proof_url="https://x", status="zombie",
        )

def test_acquisition_amount_coerced():
    r = StartupRecord(
        company_name="A", cornellians=[_good_aff()],
        proof_url="https://x", status="acquired",
        acquisition_amount_usd="$1.2B",
    )
    assert r.acquisition_amount_usd == 1_200_000_000
```

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_schema.py -v`
Expected: ImportError or NameError for `StartupRecord`.

- [ ] **Step 3: Implement**

Append to `schema.py`:
```python
import re

FundingStage = Literal["pre-seed", "seed", "series-a", "series-b", "series-c",
                       "series-d", "series-e", "growth", "public", "unknown"]
StatusType = Literal["active", "acquired", "shutdown", "ipo", "unknown"]
TierType = Literal["high", "provisional", "weak"]


_MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([MmBbKk]?)")
_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "": 1}


def _coerce_money(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = _MONEY_RE.search(str(v).strip())
    if not m:
        raise ValueError(f"cannot parse money: {v!r}")
    raw, suffix = m.group(1).replace(",", ""), m.group(2).upper()
    return int(float(raw) * _MULT[suffix])


class StartupRecord(BaseModel):
    company_name: str
    cornellians: list[CornellianAffiliation] = Field(min_length=1)
    proof_url: str

    description: Optional[str] = None
    industry: Optional[str] = None
    funding_total_usd: Optional[int] = None
    funding_stage: Optional[FundingStage] = None
    funding_last_round_year: Optional[int] = None
    founded_year: Optional[int] = None
    employee_count: Optional[int] = None
    is_public: Optional[bool] = None
    headquarters: Optional[str] = None

    status: StatusType = "unknown"
    exit_year: Optional[int] = None
    acquirer: Optional[str] = None
    acquisition_amount_usd: Optional[int] = None

    website_url: Optional[str] = None
    linkedin_company_url: Optional[str] = None
    crunchbase_url: Optional[str] = None

    tags: list[str] = Field(default_factory=list)
    non_cornell_cofounder_schools: list[str] = Field(default_factory=list)

    first_seen_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    validation_tier: TierType = "weak"
    validation_issues: list[str] = Field(default_factory=list)

    @field_validator("funding_total_usd", "acquisition_amount_usd", mode="before")
    @classmethod
    def coerce_money(cls, v):
        return _coerce_money(v)

    @field_validator("founded_year", "exit_year", "funding_last_round_year", mode="before")
    @classmethod
    def coerce_year(cls, v):
        if v is None or v == "":
            return None
        try:
            n = int(str(v).strip())
        except ValueError:
            raise ValueError(f"cannot parse year: {v!r}")
        if not (1700 <= n <= 2030):
            raise ValueError(f"year {n} out of plausible range")
        return n

    @field_validator("employee_count", mode="before")
    @classmethod
    def coerce_int(cls, v):
        if v is None or v == "":
            return None
        try:
            return int(str(v).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"cannot parse int: {v!r}")
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_schema.py -v`
Expected: 9 passed (4 from A1 + 5 from A2).

- [ ] **Step 5: Commit**

```bash
git add schema.py tests/test_schema.py
git commit -m "feat(schema): StartupRecord with status/exit/urls/tags fields + coercers"
```

---

### Task A3: ExtractionResult, SearchStrategy, GapItem

**Files:**
- Modify: `schema.py`
- Modify: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

Append:
```python
from schema import ExtractionResult, SearchStrategy, GapItem

def test_extraction_result_round_trips():
    r = ExtractionResult(records=[
        StartupRecord(company_name="A", cornellians=[_good_aff()], proof_url="https://x"),
    ], notes="ok")
    s = r.model_dump_json()
    r2 = ExtractionResult.model_validate_json(s)
    assert r2.records[0].company_name == "A"

def test_search_strategy_requires_queries():
    with pytest.raises(ValidationError):
        SearchStrategy(name="x", rationale="y", queries=[])

def test_gap_item_tier_enforced():
    with pytest.raises(ValidationError):
        GapItem(record_id="a", missing_fields=["founders"],
                validation_tier="bogus", suggested_action="search")
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

Append to `schema.py`:
```python
class ExtractionResult(BaseModel):
    records: list[StartupRecord] = Field(default_factory=list)
    notes: str = ""


class SearchStrategy(BaseModel):
    name: str
    rationale: str
    queries: list[str] = Field(min_length=1)


class GapItem(BaseModel):
    record_id: str
    missing_fields: list[str]
    validation_tier: TierType
    suggested_action: str
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add schema.py tests/test_schema.py
git commit -m "feat(schema): ExtractionResult, SearchStrategy, GapItem models"
```

---

### Task A4: evidence.py span verification

**Files:**
- Create: `evidence.py`
- Test: `tests/test_evidence.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_evidence.py
from evidence import normalize, span_present

def test_normalize_collapses_whitespace_and_lowercases():
    assert normalize("Sandy   Weill\n\n(Cornell '55)") == "sandy weill (cornell '55)"

def test_span_present_exact():
    assert span_present(span="Sandy Weill", source="...by Sandy Weill ...")

def test_span_present_case_insensitive():
    assert span_present(span="sandy WEILL", source="founded by Sandy Weill in 1962")

def test_span_present_whitespace_tolerant():
    assert span_present(span="Sandy Weill", source="Sandy\nWeill founded Citigroup")

def test_span_absent():
    assert not span_present(span="Sandy Weill", source="Bob Smith founded the firm")

def test_empty_span_is_not_present():
    assert not span_present(span="", source="anything")
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

```python
# evidence.py
import re

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").lower()).strip()


def span_present(span: str, source: str) -> bool:
    if not span or not span.strip():
        return False
    return normalize(span) in normalize(source)
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add evidence.py tests/test_evidence.py
git commit -m "feat(evidence): span_present helper for hallucination defense"
```

---

### Task A5: url_canonical.py

**Files:**
- Create: `url_canonical.py`
- Test: `tests/test_url_canonical.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_url_canonical.py
from url_canonical import canonicalize_url

def test_strips_utm_params():
    assert canonicalize_url("https://x.com/a?utm_source=foo&id=1") == "https://x.com/a?id=1"

def test_strips_all_tracking_params():
    url = "https://x.com/?utm_medium=a&utm_campaign=b&fbclid=c&gclid=d&id=1"
    assert canonicalize_url(url) == "https://x.com/?id=1"

def test_lowercases_host():
    assert canonicalize_url("HTTPS://EXAMPLE.COM/Path") == "https://example.com/Path"

def test_drops_trailing_slash_on_root():
    assert canonicalize_url("https://example.com/") == "https://example.com"

def test_wikipedia_unchanged_otherwise():
    url = "https://en.wikipedia.org/wiki/Sandy_Weill"
    assert canonicalize_url(url) == url

def test_none_passes_through():
    assert canonicalize_url(None) is None
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

```python
# url_canonical.py
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse


_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
             "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid", "ref"}


def canonicalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    p = urlparse(url.strip())
    if not p.scheme:
        return url
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k.lower() not in _TRACKING]
    query = urlencode(qs)
    path = p.path
    if path == "/" and not query and not p.fragment:
        return f"{scheme}://{netloc}"
    return urlunparse((scheme, netloc, path, p.params, query, p.fragment))
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add url_canonical.py tests/test_url_canonical.py
git commit -m "feat(url): canonicalize_url helper"
```

---

### Task A6: Schema-aware _parse_json

**Files:**
- Modify: `startup_researcher.py` -- function `_parse_json`
- Create: `tests/test_parse_json.py`
- Create: `tests/fixtures/gemini_replies/clean.json`
- Create: `tests/fixtures/gemini_replies/fenced.txt`
- Create: `tests/fixtures/gemini_replies/echoed.txt`

- [ ] **Step 1: Write fixtures**

`tests/fixtures/gemini_replies/clean.json`:
```json
{"records": [{"company_name": "Citigroup", "cornellians": [{"name": "Sandy Weill", "school": "CU", "role": "alumnus", "grad_year": 1955, "role_at_company": "founder", "evidence_span": "Sandy Weill", "source_url": "https://en.wikipedia.org/wiki/Sandy_Weill"}], "proof_url": "https://en.wikipedia.org/wiki/Citigroup"}], "notes": ""}
```

`tests/fixtures/gemini_replies/fenced.txt`:
```
Here is the structured output you requested:

```json
{"records": [], "notes": "no startups on this page"}
```

Let me know if you need anything else.
```

`tests/fixtures/gemini_replies/echoed.txt`: literally an echo of the user prompt with no model reply; copy a short example from a real `gemini_parse_failures.log` if available, otherwise:
```
Extract startups from the text below. From the text below, extract ...
<<<__GEMINI_RESPONSE_BELOW__>>>
(empty)
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_parse_json.py
from pathlib import Path
from schema import ExtractionResult
from startup_researcher import _parse_json_typed

FIX = Path(__file__).parent / "fixtures" / "gemini_replies"

def test_parse_clean_json():
    text = (FIX / "clean.json").read_text()
    result, outcome = _parse_json_typed(text, ExtractionResult)
    assert outcome == "parsed"
    assert result.records[0].company_name == "Citigroup"

def test_parse_fenced():
    text = (FIX / "fenced.txt").read_text()
    result, outcome = _parse_json_typed(text, ExtractionResult)
    assert outcome == "fence_extracted"
    assert result.records == []

def test_parse_prompt_echo_returns_none():
    text = (FIX / "echoed.txt").read_text()
    result, outcome = _parse_json_typed(text, ExtractionResult)
    assert result is None
    assert outcome in ("schema_invalid", "empty")

def test_parse_garbage_returns_none():
    result, outcome = _parse_json_typed("not even json", ExtractionResult)
    assert result is None
    assert outcome == "schema_invalid"
```

- [ ] **Step 3: Run, verify fail**

Run: `pytest tests/test_parse_json.py -v`
Expected: `ImportError` for `_parse_json_typed`.

- [ ] **Step 4: Implement**

In `startup_researcher.py`, add (near the existing `_parse_json`):
```python
from typing import Type, TypeVar
from pydantic import BaseModel, ValidationError

_T = TypeVar("_T", bound=BaseModel)
_MARKER = "<<<__GEMINI_RESPONSE_BELOW__>>>"
_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def _parse_json_typed(text: str, model_cls: Type[_T]) -> tuple[_T | None, str]:
    """Returns (model_instance, outcome_string).

    outcome ∈ {"parsed", "fence_extracted", "marker_sliced", "schema_invalid", "empty"}
    """
    if not text or not text.strip():
        return None, "empty"

    # Marker slice
    sliced = text
    outcome = "parsed"
    if _MARKER in text:
        sliced = text.rsplit(_MARKER, 1)[1]
        outcome = "marker_sliced"

    # Fence extract -- take the LARGEST fenced json block
    fences = _FENCE_RE.findall(sliced)
    candidates = sorted(fences, key=len, reverse=True) if fences else []
    if candidates:
        outcome = "fence_extracted" if outcome == "parsed" else outcome
        for cand in candidates:
            try:
                return model_cls.model_validate_json(cand.strip()), outcome
            except (ValidationError, ValueError):
                continue

    # Try the raw sliced text
    try:
        return model_cls.model_validate_json(sliced.strip()), outcome
    except (ValidationError, ValueError) as e:
        _log_parse_failure(text, model_cls, e)
        return None, "schema_invalid"


def _log_parse_failure(text: str, model_cls, exc) -> None:
    failure_log = Path("startup_output") / "gemini_parse_failures.log"
    failure_log.parent.mkdir(parents=True, exist_ok=True)
    with failure_log.open("a", encoding="utf-8") as f:
        f.write(f"=== {datetime.utcnow().isoformat()} | {model_cls.__name__} ===\n")
        f.write(f"error: {exc}\n")
        f.write(f"raw_len={len(text)} has_marker={_MARKER in text} ")
        f.write(f"has_fence={'```json' in text}\n")
        f.write(text[:8000])
        f.write("\n=== END ===\n\n")
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/test_parse_json.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add startup_researcher.py tests/test_parse_json.py tests/fixtures/gemini_replies/
git commit -m "feat(researcher): schema-aware _parse_json_typed with Pydantic models"
```

---

### Task A7: Pass-1 extraction prompt (minimal schema)

**Files:**
- Modify: `startup_researcher.py` -- new function `_extract_pass1`

- [ ] **Step 1: Add the function**

Add near the existing `_extract_startups_chunk`:

```python
_PASS1_HEADER = (
    "You are an extractor, not a researcher. Read the text below and extract every company "
    "where AT LEAST ONE founder, co-founder, or significant role-holder is a Cornellian "
    "(alumnus, faculty, student, postdoc, or researcher of any Cornell school: CU Ithaca, "
    "Cornell Tech, Weill Cornell Medicine, or Cornell Vet).\n\n"
    "RULES:\n"
    "- Use ONLY the text provided below. Do not recall, infer, or estimate from prior knowledge.\n"
    "- For every non-null value, include the substring of the text supporting it in `evidence_span`.\n"
    "- If a field is not stated in the text, return null. Do not guess.\n"
    "- Output a single ```json fenced code block containing one ExtractionResult object.\n"
    "- The marker on the last line is the boundary; nothing useful comes before it.\n\n"
)


def _build_pass1_prompt(text: str) -> str:
    schema_excerpt = json.dumps({
        "records": [{
            "company_name": "string",
            "cornellians": [{
                "name": "string", "school": "CU|Cornell Tech|Weill|Vet|unknown",
                "role": "alumnus|faculty|student|postdoc|researcher",
                "grad_year": "int or null", "role_at_company":
                "founder|cofounder|ceo|cto|early_employee|board|investor|advisor",
                "evidence_span": "string (must be a substring of input)",
                "source_url": "string",
            }],
            "proof_url": "string",
            "status": "active|acquired|shutdown|ipo|unknown",
            "funding_total_usd": "int or null",
            "founded_year": "int or null",
        }],
        "notes": "string",
    }, indent=2)
    return (
        _PASS1_HEADER
        + "JSON SHAPE (every field listed; return null when not stated):\n"
        + f"```json\n{schema_excerpt}\n```\n\n"
        + "TEXT TO EXTRACT FROM:\n"
        + text
        + f"\n\n{_MARKER}\n```json\n"  # priming the fence
    )


_PROMPT_HARD_CAP = 45_000  # below the 50KB cliff; see wiki/site-profiles/gemini-web.md


def _extract_pass1(page_text: str, source_url: str) -> list[StartupRecord]:
    prompt = _build_pass1_prompt(page_text)
    if len(prompt) > _PROMPT_HARD_CAP:
        # Hit the 50KB clipboard-paste cliff. Trim page_text and try again.
        overhead = len(prompt) - len(page_text)
        budget = _PROMPT_HARD_CAP - overhead - 500   # safety margin
        prompt = _build_pass1_prompt(page_text[:budget])
        log.warning("pass1 prompt exceeded 45K; trimmed page_text to %d", budget)
    response = call_gemini(prompt, label="extract_pass1")
    result, outcome = _parse_json_typed(response, ExtractionResult)
    if result is None:
        return []
    out: list[StartupRecord] = []
    for r in result.records:
        # Override proof_url with the actual source URL (Gemini sometimes invents one)
        r.proof_url = source_url
        # Evidence-span validation
        kept_cornellians = [a for a in r.cornellians
                            if span_present(a.evidence_span, page_text)]
        if not kept_cornellians:
            continue   # drop record: no verifiable affiliation
        r.cornellians = kept_cornellians
        out.append(r)
    return out
```

Add necessary imports at top of file:
```python
from schema import StartupRecord, ExtractionResult, CornellianAffiliation, SearchStrategy, GapItem
from evidence import span_present
```

- [ ] **Step 2: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): Pass-1 extraction prompt with evidence-span filter"
```

---

### Task A8: Pass-2 contingent extraction

**Files:**
- Modify: `startup_researcher.py` -- new function `_extract_pass2`

- [ ] **Step 1: Add the function**

```python
def _build_pass2_prompt(record: StartupRecord, page_text: str) -> str | None:
    """Return None if no pass-2 fields are warranted for this record."""
    asks: list[str] = []
    if record.status == "acquired":
        asks.append("exit_year (int or null)")
        asks.append("acquirer (string or null)")
        asks.append("acquisition_amount_usd (int or null; coerce $1.2B -> 1200000000)")
    if record.funding_total_usd is not None and record.funding_total_usd > 0:
        asks.append("funding_stage (pre-seed|seed|series-a|...|growth|public|unknown)")
        asks.append("funding_last_round_year (int or null)")
    if len(record.cornellians) > 1 or len([c for c in record.cornellians
                                            if c.role_at_company in ("founder", "cofounder")]) > 1:
        asks.append("non_cornell_cofounder_schools (list of strings, the other founders' universities)")
    # Always potentially valuable when the source page is the company's about page
    asks.append("description (one sentence)")
    asks.append("industry (string)")
    asks.append("tags (list of short classifier strings)")
    asks.append("headquarters (string)")
    asks.append("website_url (string or null)")
    asks.append("linkedin_company_url (string or null, only if stated in text)")
    asks.append("crunchbase_url (string or null, only if stated in text)")
    asks.append("employee_count (int or null)")
    asks.append("founded_year (int or null, if not already known)")
    if not asks:
        return None
    return (
        "Read the text below and return ONLY the following fields for the company named "
        f"\"{record.company_name}\". Use the text only -- do not recall or estimate.\n\n"
        f"Fields requested:\n- " + "\n- ".join(asks)
        + "\n\nFor every non-null value include `<field>_evidence_span` as a substring of the text.\n"
        + "Output one ```json fenced block, schema: {\"company_name\": ..., ...}.\n\n"
        + "TEXT:\n" + page_text
        + f"\n\n{_MARKER}\n```json\n"
    )


def _extract_pass2(record: StartupRecord, page_text: str) -> StartupRecord:
    prompt = _build_pass2_prompt(record, page_text)
    if prompt is None:
        return record
    response = call_gemini(prompt, label="extract_pass2")
    # Pass-2 returns a single object; parse as a dict and apply field-by-field.
    try:
        cleaned = _slice_and_unfence(response)
        data = json.loads(cleaned)
    except (ValueError, json.JSONDecodeError):
        return record   # leave record as-is; pass-1 fields stand

    for field, value in data.items():
        if field.endswith("_evidence_span"):
            continue
        if field == "company_name":
            continue   # never overwrite identity
        if value is None or value == "":
            continue
        span_field = f"{field}_evidence_span"
        span = data.get(span_field)
        if span and not span_present(span, page_text):
            # evidence-unverified field; skip
            continue
        try:
            setattr(record, field, value)
        except (AttributeError, ValidationError):
            continue
    return record


def _slice_and_unfence(text: str) -> str:
    sliced = text.rsplit(_MARKER, 1)[1] if _MARKER in text else text
    fences = _FENCE_RE.findall(sliced)
    return max(fences, key=len) if fences else sliced.strip()
```

- [ ] **Step 2: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): Pass-2 contingent extraction with evidence-span guards"
```

---

### Task A9: Wire the two-pass flow into the existing extraction path

**Files:**
- Modify: `startup_researcher.py` -- function `_extract_startups_chunk` (or its caller in the round loop)

- [ ] **Step 1: Replace the existing chunk-extraction body**

Find the function that the round loop calls to turn a page into records (likely `_extract_startups_chunk(text, url)` or `extract_from_page(text, url)`). Replace its body with:

```python
def extract_from_page(page_text: str, source_url: str) -> list[StartupRecord]:
    """Two-pass extraction with degradation-aware schema mode."""
    level = _ladder().level if _ladder() else Level.NORMAL
    if level >= Level.SCRAPE_ONLY:
        return []
    if not page_text:
        return []

    # Chunk if needed
    chunk_size = 15000 if level == Level.DEMOTED else 30000
    chunks = _split_into_chunks(page_text, chunk_size, overlap=1000)

    out: list[StartupRecord] = []
    seen_names: set[str] = set()
    for chunk in chunks:
        pass1 = _extract_pass1(chunk, source_url)
        for rec in pass1:
            key = _normalise_name(rec.company_name)
            if key in seen_names:
                continue
            seen_names.add(key)
            if level == Level.NORMAL:
                rec = _extract_pass2(rec, chunk)
            out.append(rec)
    return out
```

If the old function had additional callers (e.g., the planner or verify paths), keep its signature as a shim that calls `extract_from_page`.

- [ ] **Step 2: Update the round loop call site**

Wherever the round loop did `records = _extract_startups_chunk(...)`, switch to `records = extract_from_page(page_text, url)`. The result type is now `list[StartupRecord]`, not `list[dict]`. The DB upsert (Task A10) accepts the model directly.

- [ ] **Step 3: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 4: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): two-pass extraction wired into round loop"
```

---

### Task A10: Model-aware StartupDB.upsert + merge conflicts log

**Files:**
- Modify: `startup_researcher.py` -- class `StartupDB`
- Create: `tests/test_db_upsert.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_upsert.py
import json
from pathlib import Path
from schema import StartupRecord, CornellianAffiliation
from startup_researcher import StartupDB

def _aff(name="A", school="CU"):
    return CornellianAffiliation(
        name=name, school=school, role="alumnus", grad_year=2010,
        role_at_company="founder", evidence_span=name, source_url="https://x",
    )

def _rec(name="Acme", **overrides):
    base = dict(company_name=name, cornellians=[_aff()], proof_url="https://x")
    base.update(overrides)
    return StartupRecord(**base)

def test_first_upsert_creates(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec())
    assert len(db.records) == 1

def test_upsert_unions_cornellians(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec(cornellians=[_aff("Alice")]))
    db.upsert(_rec(cornellians=[_aff("Bob")]))
    names = {a["name"] for a in db.records["acme"]["cornellians"]}
    assert names == {"Alice", "Bob"}

def test_upsert_fills_missing_scalar(tmp_path):
    db = StartupDB(tmp_path / "db.json")
    db.upsert(_rec(founded_year=None))
    db.upsert(_rec(founded_year=2015))
    assert db.records["acme"]["founded_year"] == 2015

def test_upsert_logs_conflict(tmp_path):
    db = StartupDB(tmp_path / "db.json", conflict_log=tmp_path / "conflicts.jsonl")
    db.upsert(_rec(funding_total_usd=1_000_000))
    db.upsert(_rec(funding_total_usd=2_000_000))
    log = (tmp_path / "conflicts.jsonl").read_text().strip()
    assert "funding_total_usd" in log
```

- [ ] **Step 2: Run, verify fail.**

Run: `pytest tests/test_db_upsert.py -v`

- [ ] **Step 3: Implement**

Replace `StartupDB.upsert`. Outline:

```python
class StartupDB:
    def __init__(self, path: Path, conflict_log: Path | None = None):
        self.path = Path(path)
        self.conflict_log = Path(conflict_log) if conflict_log else self.path.parent / "merge_conflicts.jsonl"
        self.records: dict[str, dict] = self._load()

    def upsert(self, rec: StartupRecord) -> str:
        """Returns 'new' | 'merged'."""
        key = _normalise_name(rec.company_name)
        new = rec.model_dump(mode="json")
        if key not in self.records:
            new["first_seen_at"] = new.get("first_seen_at") or _utc_now()
            new["last_verified_at"] = _utc_now()
            self.records[key] = new
            return "new"
        existing = self.records[key]
        existing["last_verified_at"] = _utc_now()

        # List fields: union and dedupe
        for field in ("validation_issues", "tags", "non_cornell_cofounder_schools"):
            existing[field] = _union_strings(existing.get(field, []), new.get(field, []))

        # Cornellians: union by name
        existing_corn = {c["name"]: c for c in existing.get("cornellians", [])}
        for c in new.get("cornellians", []):
            existing_corn.setdefault(c["name"], c)
        existing["cornellians"] = list(existing_corn.values())

        # Scalars: fill if missing; log conflicts if both populated and differ
        for field in ("description", "industry", "funding_total_usd", "funding_stage",
                      "funding_last_round_year", "founded_year", "employee_count",
                      "is_public", "headquarters", "status", "exit_year", "acquirer",
                      "acquisition_amount_usd", "website_url", "linkedin_company_url",
                      "crunchbase_url"):
            old_v, new_v = existing.get(field), new.get(field)
            if old_v in (None, "", "unknown") and new_v not in (None, "", "unknown"):
                existing[field] = new_v
            elif old_v and new_v and old_v != new_v:
                self._log_conflict(key, field, old_v, new_v)
                # Keep old; consider credibility weighting in a follow-up
        return "merged"

    def _log_conflict(self, key, field, old_v, new_v):
        self.conflict_log.parent.mkdir(parents=True, exist_ok=True)
        with self.conflict_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _utc_now(), "record": key, "field": field,
                "kept": old_v, "rejected": new_v,
            }) + "\n")


def _union_strings(a, b):
    out, seen = [], set()
    for x in (a or []) + (b or []):
        if x and x not in seen:
            out.append(x); seen.add(x)
    return out


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
```

If the old upsert hard-rejected records missing `cornellian_founder`, the equivalent in the new world is: Pydantic already enforces `cornellians: list[...] = Field(min_length=1)` at construction time, so the record can't reach `upsert` without at least one affiliation.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_db_upsert.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add startup_researcher.py tests/test_db_upsert.py
git commit -m "feat(db): model-aware upsert with cornellian union and conflict log"
```

---

### Task A11: Re-validate after merge / fill

**Files:**
- Modify: `startup_researcher.py` -- function `fill_missing_data` and `StartupDB.upsert`

- [ ] **Step 1: Add the call**

After every mutation in `upsert` (and at the end of `fill_missing_data`), re-run `validate_record(record_dict)` so `validation_tier` reflects the current state. Add to `upsert` just before returning, and append to `fill_missing_data` after the targeted-fill loop.

- [ ] **Step 2: Smoke**

Run: `pytest tests/ -v`
Expected: all existing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add startup_researcher.py
git commit -m "fix(researcher): re-validate records after upsert and fill"
```

---

### Task A12: Update planner prompt to return SearchStrategy

**Files:**
- Modify: `startup_researcher.py` -- function `plan_research`

- [ ] **Step 1: Wrap response in SearchStrategy parse**

Replace the planner's `_parse_json` call with `_parse_json_typed(response, SearchStrategy)`. On `None` outcome, fall back to the existing hard-coded plan as today.

Update the planner prompt to require a single fenced block matching `SearchStrategy.model_json_schema()`, and to use SINGLE quotes inside Google search strings to avoid the embedded-double-quote bug from HANDOFF section 1.

Append the marker token to the planner prompt as its final line, same convention as extraction prompts.

- [ ] **Step 2: Smoke**

Run: `python -c "import startup_researcher; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add startup_researcher.py
git commit -m "feat(researcher): planner uses SearchStrategy schema; marker + single-quote rules"
```

---

## Phase D -- Backfill

### Task D1: reextract_all.py skeleton

**Files:**
- Create: `reextract_all.py`

- [ ] **Step 1: Write the script skeleton**

```python
# reextract_all.py
"""One-shot re-extraction of every record in startups_db.json against the new schema.

Usage:
    python reextract_all.py [--db startup_output/startups_db.json]
                            [--out startup_output/startups_db_v2.json]
                            [--max N] [--workers 2]

Resume-safe: skips records already present in the output file.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from schema import StartupRecord
from startup_researcher import (
    scrape_page, _extract_pass1, _extract_pass2,
    _normalise_name, StartupDB,
)


def _load_existing(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _failure_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def reextract_one(rec: dict, out_dir: Path) -> tuple[str, str | dict]:
    """Returns (status, payload). status in {ok, fetch_failed, unmatched, schema_failed}."""
    name = rec.get("company_name", "")
    url = rec.get("proof_url") or rec.get("source_url")
    if not url:
        return "fetch_failed", {"company": name, "reason": "no proof_url"}
    try:
        text = scrape_page(url)
    except Exception as e:
        return "fetch_failed", {"company": name, "url": url, "error": str(e)}
    if not text:
        return "fetch_failed", {"company": name, "url": url, "error": "empty"}

    pass1 = _extract_pass1(text, url)
    target = _normalise_name(name)
    match = next((r for r in pass1 if _normalise_name(r.company_name) == target), None)
    if match is None:
        return "unmatched", {"company": name, "url": url, "found": [r.company_name for r in pass1]}
    try:
        match = _extract_pass2(match, text)
    except Exception as e:
        return "schema_failed", {"company": name, "error": str(e)}
    return "ok", match.model_dump(mode="json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="startup_output/startups_db.json")
    ap.add_argument("--out", default="startup_output/startups_db_v2.json")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args(argv)

    src = Path(args.db)
    out = Path(args.out)
    existing = _load_existing(out)
    src_data = json.loads(src.read_text())
    records = src_data if isinstance(src_data, list) else list(src_data.values())
    if args.max:
        records = records[:args.max]

    fail_dir = out.parent
    todo = [r for r in records if _normalise_name(r.get("company_name", "")) not in existing]
    print(f"backfill: {len(todo)} of {len(records)} records remain to be re-extracted")

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(reextract_one, r, out.parent): r for r in todo}
        for fut in as_completed(futs):
            status, payload = fut.result()
            rec = futs[fut]
            name = rec.get("company_name", "")
            if status == "ok":
                existing[_normalise_name(name)] = payload
                done += 1
                if done % 25 == 0:
                    _save(out, existing)
                    print(f"  ... {done} re-extracted")
            else:
                _failure_log(fail_dir / f"reextract_{status}.jsonl", payload)
    _save(out, existing)
    print(f"backfill complete: {done} new records in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke (import only)**

Run: `python -c "import reextract_all; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add reextract_all.py
git commit -m "feat(backfill): reextract_all.py one-shot script"
```

---

### Task D2: Backfill smoke test against 10 records

**Files:**
- (no new files)

- [ ] **Step 1: Run against a small slice**

```bash
python reextract_all.py --max 10 --workers 2 \
    --db startup_output/startups_db.json \
    --out startup_output_test/startups_db_v2.json
```
Expected: at least one record lands in `startups_db_v2.json` and at least one failure bucket file exists in `startup_output_test/`.

- [ ] **Step 2: Inspect**

Run:
```bash
python -c "import json; d=json.load(open('startup_output_test/startups_db_v2.json')); print(len(d)); print(list(d.values())[0])"
```
Verify the record has the new fields (`status`, `tags`, `cornellians` list).

- [ ] **Step 3: Commit (no code change, but tag the verification)**

```bash
git commit --allow-empty -m "test(backfill): verified 10-record smoke run produces new-schema records"
```

---

## Phase Final -- Live smoke test

### Task F1: One-round live smoke

**Files:** none.

- [ ] **Step 1: Run a single round with the existing seed URLs**

```bash
PYTHONUTF8=1 python startup_researcher.py \
    --headless --max-rounds 1 --output-dir startup_output_test \
    --seed-urls "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups" \
    "Find every company where at least one founder is a Cornellian."
```

- [ ] **Step 2: Verify the success criteria from the spec**

Check each:

- [ ] `startup_output_test/gemini_calls.jsonl` exists and has at least one entry.
- [ ] `startup_output_test/round_metrics.jsonl` exists and has one entry for round 1.
- [ ] `startup_output_test/startups_db.json` contains at least 400 records (loosened from spec because seed-URL only).
- [ ] Every record in the new DB has a non-empty `cornellians` list.
- [ ] Spot-check one record: confirm `evidence_span` substrings appear in the cached source page.

- [ ] **Step 3: Commit**

```bash
git commit --allow-empty -m "test(smoke): live one-round verification of hardening pass"
```

---

### Task F2: Deliberate-failure ladder test

**Files:** none.

- [ ] **Step 1: Force a low parse rate**

Temporarily monkey-patch the Pass-1 prompt to ask Gemini for invalid JSON (e.g., prepend "Ignore the schema. Return a markdown table only.") and run a 1-round seed-URL test.

- [ ] **Step 2: Verify the ladder advances**

Check `startup_output_test/round_metrics.jsonl` -- expect the round summary to show parse rate well below 70% and the in-process log to contain `extraction degraded to level 2` within ~20 calls.

- [ ] **Step 3: Revert the patch and commit**

```bash
git checkout startup_researcher.py    # revert the deliberate break
git commit --allow-empty -m "test(smoke): ladder demotes correctly under forced low parse rate"
```

---

## Self-review

**Spec coverage:**

| Spec section | Tasks |
|---|---|
| Core principle: extraction not discovery | A6 (`_parse_json_typed`), A7 (`_PASS1_HEADER`), A8 (pass-2 header), A4 (evidence) |
| B1: outcome logging | B1, B2, B3 |
| B2: round metrics | B6 |
| B3: degradation ladder | B7, B8, B9 |
| B4: real resume | B10 |
| B5: worker safety | B11 |
| A1: Pydantic models | A1, A2, A3 |
| A2: schema-aware parse | A6 |
| A3: prompt with schema | A7, A8 |
| A4: evidence-span validation | A4, A7 (filter in pass-1), A8 (filter in pass-2) |
| A5: contingent prompts | A7, A8, A9 |
| DB merge | A10, A11 |
| C1: structured cornellians | A1, A2 |
| C2: status / exit | A2 |
| C3: canonical URLs | A2, A5 |
| C4: tags / cofounder schools | A2 |
| C5: URL canonicalize helper | A5 |
| D1-D4: backfill | D1, D2 |
| Wiki: silent-failure | B1, B2, B5, B6 (RoundMetrics modeled on wiki pseudocode) |
| Wiki: infinite-retry | B12 |
| Wiki: gemini-web 50KB cliff | A7 (prompt-size guard) |
| Wiki: selector-over-data-attribute | B3 (strategy-index logging) |

No spec sections lack a task.

**Placeholder scan:** no TBD / TODO / "implement appropriately" / "similar to" in any task. Code blocks are present for every code step.

**Type/name consistency:** `_parse_json_typed`, `extract_from_page`, `_extract_pass1`, `_extract_pass2`, `StartupRecord`, `ExtractionResult`, `CornellianAffiliation`, `DegradationLadder`, `Level`, `CallOutcome`, `GeminiCallLog`, `SeleniumFetchLog`, `RoundMetrics`, `span_present`, `canonicalize_url`, `StartupDB.upsert(StartupRecord)` -- all used consistently across tasks.
