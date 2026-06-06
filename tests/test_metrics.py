# tests/test_metrics.py
import time
import json
import pytest
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
