"""Aggregate verification signals into a confidence score + provenance chain.

Pure: no I/O. The published dataset keeps only publishable edges, each carrying
its confidence and the reasons behind it, so every row is defensible on its face.
"""

SIGNAL_WEIGHTS = {
    "source_directory": 0.45,   # a curated Cornell-startup directory listing
    "api_confirmed": 0.35,      # OpenCorporates/EDGAR/Wikidata confirmed the founding edge
    "per_corroboration": 0.12,  # each independent source beyond the first
    "cornell_strong": 0.20,     # confirmed Cornell education/affiliation
    "llm_founder": 0.25,        # the source-aware adjudicator said FOUNDER
}
PUBLISH_THRESHOLD = 0.70


def score_edge(source_tier, corroborations, api_confirmed, cornell_tie, llm_verdict):
    prov, score = [], 0.0
    if source_tier == "directory":
        score += SIGNAL_WEIGHTS["source_directory"]
        prov.append("directory-source")
    if api_confirmed:
        score += SIGNAL_WEIGHTS["api_confirmed"]
        prov.append("api-confirmed")
    extra = max(0, corroborations - 1)
    if extra:
        score += extra * SIGNAL_WEIGHTS["per_corroboration"]
        prov.append(f"corroborations={corroborations}")
    if cornell_tie == "strong":
        score += SIGNAL_WEIGHTS["cornell_strong"]
        prov.append("cornell-strong")
    if llm_verdict == "FOUNDER":
        score += SIGNAL_WEIGHTS["llm_founder"]
        prov.append("llm-founder")
    score = min(1.0, score)
    # Structured-agreement shortcut (spec): two authoritative sources agreeing on
    # the founding edge (an API confirmation + a second independent corroboration)
    # is publishable on its own, with NO LLM call. Encode it explicitly rather
    # than hoping the weights happen to clear the threshold.
    structured_agreement = api_confirmed and corroborations >= 2
    if structured_agreement:
        score = max(score, PUBLISH_THRESHOLD)
        prov.append("structured-agreement")
    return {"confidence": round(score, 3),
            "publishable": score >= PUBLISH_THRESHOLD or structured_agreement,
            "provenance": prov}
