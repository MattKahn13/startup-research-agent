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


def test_parse_json_label_prefix():
    """Gemini's rendered code-block language label ('JSON') leaks into the
    scraped innerText as a bare prefix with no backtick fence. The real
    smoke run produced 390 of these; each was a valid extraction thrown away.
    """
    text = (
        'JSON{\n'
        '  "records": [\n'
        '    {\n'
        '      "company_name": "Sage",\n'
        '      "cornellians": [\n'
        '        {"name": "Raj Mehra", "school": "CU", "role": "alumnus",\n'
        '         "grad_year": 2009, "role_at_company": "founder",\n'
        '         "evidence_span": "Raj Mehra, MBA 09",\n'
        '         "source_url": "https://bigredai.org/startups"}\n'
        '      ],\n'
        '      "proof_url": "https://bigredai.org/startups",\n'
        '      "status": "active", "funding_total_usd": null, "founded_year": null\n'
        '    }\n'
        '  ],\n'
        '  "notes": ""\n'
        '}'
    )
    result, outcome = _parse_json_typed(text, ExtractionResult)
    assert result is not None, "JSON-label-prefixed response should parse"
    assert result.records[0].company_name == "Sage"


def test_parse_json_prefix_with_trailing_prose():
    """Prefix label AND trailing prose around the payload."""
    text = (
        'JSON{"records": [], "notes": "no startups found"}\n'
        'Let me know if you need anything else!'
    )
    result, outcome = _parse_json_typed(text, ExtractionResult)
    assert result is not None
    assert result.records == []
