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
