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
