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
