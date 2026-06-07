"""Probe harness: ask Gemini to extract records from a known cached page,
under multiple prompt variants, and measure evidence-span match rate.

Goal: figure out a prompt strategy that produces consistently substring-matching
evidence_spans, so the procedural-defense filter doesn't drop everything.

Updates findings to probe_results.jsonl for analysis + wiki update.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import ExtractionResult
from evidence import normalize, span_present

# Lazy imports of researcher (it boots a session on call_gemini)
import startup_researcher as sr
from startup_researcher import call_gemini, _parse_json_typed, _END_OF_PROMPT_MARKER
from gemini_tool import GeminiSession


CACHE = Path("startup_output_test/cache/49a820a00b38e069.txt")
RESULTS = Path("probe_results.jsonl")
RESPONSE_DIR = Path("probe_responses")
RESPONSE_DIR.mkdir(exist_ok=True)


def _start_session():
    if sr._gemini is None:
        sr._gemini = GeminiSession(headless=True)
        sr._gemini.start()


def _save_response(variant: str, response: str) -> Path:
    p = RESPONSE_DIR / f"{variant}.txt"
    p.write_text(response, encoding="utf-8")
    return p


def _eval(records, page_text: str) -> dict:
    """Count how many records would survive the evidence-span filter, and why
    others would not."""
    survived = 0
    dropped_no_match = 0
    span_match_rate = []
    sample_bad = []
    sample_good = []
    for r in records:
        good = 0
        for a in r.cornellians:
            if span_present(a.evidence_span, page_text):
                good += 1
                if len(sample_good) < 3:
                    sample_good.append({"name": a.name, "span": a.evidence_span[:160]})
            else:
                if len(sample_bad) < 5:
                    sample_bad.append({"name": a.name, "span": a.evidence_span[:160]})
        denom = max(len(r.cornellians), 1)
        span_match_rate.append(good / denom)
        if good > 0:
            survived += 1
        else:
            dropped_no_match += 1
    avg_match = sum(span_match_rate) / max(len(span_match_rate), 1)
    return {
        "n_records": len(records),
        "survived": survived,
        "dropped_no_match": dropped_no_match,
        "avg_span_match_rate": round(avg_match, 3),
        "sample_good_spans": sample_good,
        "sample_bad_spans": sample_bad,
    }


# ---- Prompt variants -------------------------------------------------------

def variant_minimal(text: str) -> str:
    return (
        "Extract every company mentioned in the TEXT below where at least one "
        "founder, co-founder, CEO, CTO, board member, or significant employee "
        "is a Cornell alumnus, faculty, student, postdoc, or researcher "
        "(any Cornell school).\n\n"
        "OUTPUT FORMAT:\n"
        "- One ```json fenced code block containing {\"records\": [...], \"notes\": \"\"}.\n"
        "- For each record: company_name, cornellians: [{name, school, role, "
        "grad_year, role_at_company, evidence_span, source_url}], proof_url, "
        "status, funding_total_usd, founded_year.\n\n"
        "STRICT RULE: `evidence_span` MUST be a verbatim substring (40-160 chars) "
        "copy-pasted from the TEXT below. If you cannot copy-paste a span that "
        "PROVES the affiliation, omit the cornellian. Do NOT paraphrase. Do NOT "
        "summarize. Do NOT use ellipsis. Do NOT include text from outside the TEXT.\n\n"
        f"TEXT:\n{text}\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


def variant_with_examples(text: str) -> str:
    return (
        "Extract every Cornell-affiliated company from the TEXT below.\n\n"
        "GOOD evidence_span example:\n"
        "  TEXT contains: \"Co-founded by Jane Smith (Cornell '08)\"\n"
        "  GOOD span: \"Co-founded by Jane Smith (Cornell '08)\"\n\n"
        "BAD evidence_span example:\n"
        "  TEXT contains: \"Co-founded by Jane Smith (Cornell '08)\"\n"
        "  BAD span: \"Jane Smith, a Cornell alumna, co-founded the company\"\n"
        "    (this is a paraphrase; not present verbatim in TEXT)\n\n"
        "If you cannot find a verbatim substring proving the affiliation, OMIT that cornellian.\n\n"
        "OUTPUT: one ```json fenced block with shape "
        "{\"records\": [{\"company_name\": ..., \"cornellians\": [...], "
        "\"proof_url\": ..., \"status\": ..., \"funding_total_usd\": null, "
        "\"founded_year\": null}], \"notes\": \"\"}.\n\n"
        f"TEXT:\n{text}\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


def variant_quote_only(text: str) -> str:
    return (
        "You are a quote-extractor. Read the TEXT below. For every Cornell-affiliated "
        "company you find, return ONE direct quote from the TEXT that mentions the "
        "company name AND the Cornellian's name AND the Cornell connection in the same "
        "sentence (or in two adjacent sentences).\n\n"
        "Return one ```json block with shape: {\"records\": [{\"company_name\": ..., "
        "\"cornellians\": [{\"name\": ..., \"school\": \"CU|Cornell Tech|Weill|Vet|unknown\", "
        "\"role\": \"alumnus|faculty|student|postdoc|researcher\", \"grad_year\": null, "
        "\"role_at_company\": \"founder|cofounder|ceo|cto|early_employee|board|investor|advisor\", "
        "\"evidence_span\": \"DIRECT QUOTE\", \"source_url\": \"\"}], "
        "\"proof_url\": \"\", \"status\": \"active\", \"funding_total_usd\": null, "
        "\"founded_year\": null}], \"notes\": \"\"}.\n\n"
        "Each `evidence_span` is a DIRECT QUOTE: COPY THE LITERAL CHARACTERS from TEXT.\n"
        "If you must shorten a quote, use 40-200 contiguous characters from TEXT, no edits.\n\n"
        f"TEXT:\n{text}\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


def variant_anchor_word(text: str) -> str:
    """Don't ask for full span — ask for a single short anchor phrase (likely
    to be a verbatim substring) plus a separate plain-language explanation."""
    return (
        "Extract every Cornell-affiliated company from the TEXT below.\n\n"
        "For each Cornellian, provide:\n"
        "  - `evidence_span`: a SHORT phrase (10-40 characters) copied verbatim from the TEXT "
        "that contains the person's name OR a unique identifying phrase. Must be a substring of TEXT.\n"
        "  - `affiliation_explanation`: your free-text summary of why this person is Cornell-affiliated, "
        "based on the TEXT.\n\n"
        "Return one ```json block with shape: {\"records\": [{\"company_name\": ..., "
        "\"cornellians\": [{\"name\": ..., \"school\": ..., \"role\": ..., \"grad_year\": null, "
        "\"role_at_company\": ..., \"evidence_span\": \"SHORT VERBATIM\", \"source_url\": \"\"}], "
        "\"proof_url\": \"\", \"status\": \"active\", \"funding_total_usd\": null, "
        "\"founded_year\": null}], \"notes\": \"\"}\n\n"
        f"TEXT:\n{text}\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


VARIANTS = {
    "minimal": variant_minimal,
    "with_examples": variant_with_examples,
    "quote_only": variant_quote_only,
    "anchor_word": variant_anchor_word,
}


# ---- Main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS.keys()))
    ap.add_argument("--text-chars", type=int, default=8000,
                    help="how much of the cached page to send (smaller = faster probe)")
    args = ap.parse_args()

    if not CACHE.exists():
        print(f"Cache file not found: {CACHE}")
        sys.exit(1)

    full_text = CACHE.read_text(encoding="utf-8")
    text = full_text[:args.text_chars]
    print(f"Page total: {len(full_text):,} chars | probe slice: {len(text):,} chars")

    _start_session()

    for vname in args.variants:
        if vname not in VARIANTS:
            print(f"unknown variant: {vname}")
            continue
        builder = VARIANTS[vname]
        prompt = builder(text)
        print(f"\n=== {vname} ===\n  prompt size: {len(prompt):,} chars")
        t0 = time.time()
        try:
            response = call_gemini(prompt, label=f"probe_{vname}")
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        _save_response(vname, response)
        result, outcome = _parse_json_typed(response, ExtractionResult)
        if result is None:
            metrics = {
                "variant": vname,
                "outcome": outcome,
                "n_records": 0,
                "latency_s": round(dt, 1),
                "response_chars": len(response),
                "prompt_chars": len(prompt),
                "error": "parse_failed",
            }
        else:
            ev = _eval(result.records, text)
            metrics = {
                "variant": vname,
                "outcome": outcome,
                "latency_s": round(dt, 1),
                "response_chars": len(response),
                "prompt_chars": len(prompt),
                **ev,
            }
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")
        print(f"  {json.dumps({k: v for k, v in metrics.items() if k not in ('sample_good_spans','sample_bad_spans')})}")
        if "sample_bad_spans" in metrics and metrics["sample_bad_spans"]:
            print("  bad spans (sample):")
            for s in metrics["sample_bad_spans"][:3]:
                print(f"    - {s['name']}: {s['span']!r}")
        if "sample_good_spans" in metrics and metrics["sample_good_spans"]:
            print("  good spans (sample):")
            for s in metrics["sample_good_spans"][:2]:
                print(f"    - {s['name']}: {s['span']!r}")


if __name__ == "__main__":
    main()
