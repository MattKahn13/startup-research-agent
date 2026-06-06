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
