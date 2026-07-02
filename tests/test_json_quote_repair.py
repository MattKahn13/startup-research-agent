"""Regression tests for the unescaped-inner-quotes JSON repair.

Found during a live overnight-run audit (2026-07-02): Gemini's planner and
gap-fill responses periodically embed literal unescaped double-quotes inside
a JSON string value whenever the natural content itself wants a quote --
a Google search phrase-match operator ("Cornell University") or a person's
nickname (Maofan "Ted" Yin). This breaks json.loads at the string-literal
level even though the object/array bracket boundaries are perfectly correct.
9+ occurrences confirmed in one overnight run despite the prompt already
instructing "use single quotes for phrase matching" -- prompt compliance
alone isn't reliable, hence a parser-side repair pass.

See wiki/anti-patterns/llm-json-unescaped-quotes.md for the general pattern.
"""
import json
from startup_researcher import _repair_unescaped_json_quotes, _parse_json


def test_repairs_bare_array_element_with_inner_quotes():
    """Real example pulled from the overnight run's planner output: the
    captured line was ""Anaconda" site:crunchbase.com organization" """
    broken = '[\n""Anaconda" site:crunchbase.com organization"\n]'
    repaired = _repair_unescaped_json_quotes(broken)
    parsed = json.loads(repaired)
    assert parsed == ['"Anaconda" site:crunchbase.com organization']


def test_repairs_double_quoted_phrase_pair():
    """Real example: a query with TWO phrase-match operators on one line."""
    broken = '[\n""Liquid AI" founder "Cornell""\n]'
    repaired = _repair_unescaped_json_quotes(broken)
    parsed = json.loads(repaired)
    assert parsed == ['"Liquid AI" founder "Cornell"']


def test_repairs_key_value_pair_with_inner_nickname_quotes():
    """Real example: a founders field containing a bare nickname in quotes."""
    broken = (
        '{\n'
        '"founders": "Emin Gun Sirer, Kevin Sekniqi, Maofan "Ted" Yin",\n'
        '"found_useful_info": true\n'
        '}'
    )
    repaired = _repair_unescaped_json_quotes(broken)
    parsed = json.loads(repaired)
    assert parsed["founders"] == 'Emin Gun Sirer, Kevin Sekniqi, Maofan "Ted" Yin'
    assert parsed["found_useful_info"] is True


def test_leaves_already_valid_json_unchanged_in_effect():
    """A normal key:value pair with no inner quotes should parse identically
    before and after the repair pass (the repair is a no-op for clean JSON)."""
    clean = '{\n"company_name": "Acme",\n"found_useful_info": true\n}'
    repaired = _repair_unescaped_json_quotes(clean)
    assert json.loads(repaired) == json.loads(clean)


def test_parse_json_recovers_via_repair_pass_end_to_end():
    """The full _parse_json entry point should recover a real planner-style
    payload that fails both the direct parse AND the bracket-matching
    fallback, via the new quote-repair attempt. Matches the REAL shape
    captured from the live run: a top-level JSON ARRAY of strategy objects
    (not a single object), which is why _parse_json tries array-boundary
    matching first."""
    raw = (
        'Here is the strategy:\n'
        '[\n'
        '{\n'
        '"type": "fill_gaps",\n'
        '"target": "Liquid AI",\n'
        '"queries": [\n'
        '""Liquid AI" founder "Cornell""\n'
        '],\n'
        '"rationale": "Liquid AI has strong academic roots."\n'
        '}\n'
        ']\n'
    )
    result = _parse_json(raw, fallback=None)
    assert result is not None, "expected the quote-repair pass to recover this payload"
    assert result[0]["target"] == "Liquid AI"
    assert result[0]["queries"] == ['"Liquid AI" founder "Cornell"']
