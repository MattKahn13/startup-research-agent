# evidence.py
import re

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").lower()).strip()


def span_present(span: str, source: str) -> bool:
    if not span or not span.strip():
        return False
    return normalize(span) in normalize(source)
