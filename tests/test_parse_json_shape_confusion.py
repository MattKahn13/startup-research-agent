"""Regression tests for a shape-confusion crash in `_parse_json`.

Found live (2026-07-02 ~17:21 UTC): the detached overnight run crashed with
`AttributeError: 'list' object has no attribute 'get'` at the start of Round
2, right after `generate_gap_filling_strategy()` returned. Root cause, traced
and isolated with a one-variable repro (no quote-escaping involved at all):

`_parse_json`'s bracket-boundary fallback tries array boundaries (`[`...`]`)
before object boundaries (`{`...`}`) -- a comment at the call site says "try
array first since we expect arrays from extract", i.e. this ordering was
chosen for the extraction caller, which always wants a bare list. But
`_parse_json` is a SHARED helper used by three other callers that always
expect a dict wrapping a nested array (`generate_gap_filling_strategy`'s
`{"thinking":..., "actions": [...]}`, `fill_missing_data`'s per-record fill
result, and the verify-batch `{"decisions": [...]}`). Whenever the model's
raw text has any leading prose before the real JSON object (common -- e.g.
"Here is my analysis.\n\n{...}") and the direct whole-text `json.loads` fails
because of that prose, the bracket-boundary fallback finds the *inner*
`actions` array's own `[`...`]` boundaries before it ever tries the outer
object's `{`...`}` boundaries, silently parses successfully to a bare LIST
(just the actions), and returns that instead of the intended dict. The
caller then does `strategy.get("thinking", "")` on a list and crashes.

This is NOT the unescaped-quotes bug fixed earlier tonight -- the repro
below has no stray quotes anywhere; it is a pre-existing latent ordering
flaw in the bracket-fallback that predates tonight's quote-repair addition
(confirmed by reproducing it with the quote-repair function never touched).

Fix: `_parse_json` takes an optional `expect_type` hint. When given, any
successful parse of the wrong type is treated as a failed attempt and the
search continues (through the other bracket order, then the quote-repair
pass, then finally the caller's `fallback`) instead of being returned.
"""
import json

from startup_researcher import _parse_json


REALISTIC_STRATEGY_RESPONSE = """Here is my analysis.

{
"thinking": "We need to find more companies in biotech.",
"actions": [
    {"type": "discovery", "target": "biotech", "queries": ["biotech startups"], "rationale": "gap"}
]
}
"""


def test_bare_dict_json_parses_directly_regardless_of_hint():
    """Sanity check: when the direct parse already succeeds and matches,
    expect_type doesn't change anything."""
    clean = '{"thinking": "ok", "actions": []}'
    result = _parse_json(clean, fallback=None, expect_type=dict)
    assert result == {"thinking": "ok", "actions": []}


def test_leading_prose_before_dict_no_longer_mis_parses_as_bare_list():
    """The exact real-world crash shape: prose prefix defeats the direct
    parse, and the pre-existing array-first bracket search used to return
    just the inner 'actions' list instead of the outer dict."""
    result = _parse_json(
        REALISTIC_STRATEGY_RESPONSE,
        fallback={"thinking": "FALLBACK", "actions": []},
        expect_type=dict,
    )
    assert isinstance(result, dict), (
        f"expected a dict (expect_type=dict should reject the mis-matched "
        f"inner-array parse and keep searching), got {type(result)}: {result!r}"
    )
    assert result["thinking"] == "We need to find more companies in biotech."
    assert result["actions"][0]["target"] == "biotech"


def test_without_expect_type_hint_existing_array_first_behavior_is_unchanged():
    """Backward compatibility: callers that don't pass expect_type (e.g. the
    extraction shim, which legitimately wants array-first) see no behavior
    change at all."""
    raw = (
        'Here is the strategy:\n'
        '[\n'
        '{\n'
        '"type": "fill_gaps",\n'
        '"target": "Liquid AI",\n'
        '"queries": [\n'
        '"Liquid AI Cornell founder"\n'
        '],\n'
        '"rationale": "Liquid AI has strong academic roots."\n'
        '}\n'
        ']\n'
    )
    result = _parse_json(raw, fallback=None)
    assert isinstance(result, list)
    assert result[0]["target"] == "Liquid AI"


def test_expect_type_falls_back_when_nothing_of_the_right_shape_parses():
    """If every parse attempt yields the wrong type, expect_type must not
    crash -- it should exhaust all attempts and return the caller's
    fallback, same as any other unparseable response."""
    raw = "[1, 2, 3]"  # parses fine, but it's a list, not a dict
    result = _parse_json(raw, fallback={"thinking": "FALLBACK", "actions": []}, expect_type=dict)
    assert result == {"thinking": "FALLBACK", "actions": []}
