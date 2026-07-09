"""Detect when the record's founder name and the evidence disagree on WHO
founded the company (the Ava Labs bug -> a caught contradiction, not a silent
wrong pick). Pure.
"""
import re

_FOUND_NEAR = re.compile(r"\b(founder|co-?founder|founded by)\b[^.]{0,40}", re.I)


def _last(name):
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return parts[-1].lower() if parts else ""


def founder_matches_evidence(founder, evidence):
    """False only when the evidence explicitly names a DIFFERENT person as the
    founder. Absence of the name (thin evidence) is not a contradiction."""
    ev = evidence or ""
    if _last(founder) and _last(founder) in ev.lower():
        return True
    m = _FOUND_NEAR.search(ev)
    if not m:
        return True  # no explicit founder claim in evidence -> can't contradict
    named = m.group(0)
    return _last(founder) in named.lower() if _last(founder) else True
