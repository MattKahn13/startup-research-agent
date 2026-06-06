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
