"""Regression guard: the quality-verify pass must NOT disqualify a company for
being large/established.

The capture criterion (_PASS1_HEADER) is "a company where at least one founder,
co-founder, or significant role-holder is a Cornellian" -- size-agnostic. A
Fortune 500 / Big Tech company founded or led by a Cornellian is exactly in
scope. The verify prompt used to carry a "REMOVE if this is a large/established
company (Fortune 500, Big Tech) even if an alum works there" rule, which
contradicted the capture criterion and would have deleted legitimate large
Cornellian-founded companies once the pass fired. This locks that out.
"""
import startup_researcher as sr


def test_verify_prompt_keeps_large_cornellian_founded_companies(monkeypatch):
    captured = {}

    def fake_call_gemini(prompt, label=""):
        captured["prompt"] = prompt
        return '{"decisions": []}'   # remove nothing

    monkeypatch.setattr(sr, "call_gemini", fake_call_gemini)

    rec = {
        "company_name": "Accor",
        "founders": "Jane Doe",
        "proof_url": "https://example.com/accor",
        "affiliation_type": "Alumnus",
        "affiliation_evidence": "co-founded by Jane Doe, Cornell '95",
        "source_url": "https://example.com/accor",
        "description": "global hospitality company",
    }
    sr._gemini_verify_batch([rec], prompt="Find every company with a Cornellian founder",
                            batch_size=1)

    p = captured["prompt"].lower()
    # the size-based removal rule must be gone...
    assert "large/established company (fortune 500" not in p, \
        "verify prompt still carries the size-based REMOVE rule"
    # ...and the prompt must affirmatively say size doesn't disqualify
    assert "size is irrelevant" in p, \
        "verify prompt must state that company size does not disqualify"
