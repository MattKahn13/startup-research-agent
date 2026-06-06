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
        self.latency_ms: int = 0

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
        handle.latency_ms = latency_ms
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


from collections import Counter


class RoundMetrics:
    def __init__(self, round_number: int):
        self.round_number = round_number
        self.gemini_outcomes: Counter = Counter()
        self.gemini_latency_total = 0
        self.gemini_calls = 0
        self.selenium_outcomes: Counter = Counter()
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
        pct = round(100 * parsed / self.gemini_calls) if self.gemini_calls else 0
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
