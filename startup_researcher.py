"""
startup_researcher.py
─────────────────────
Purpose-built deep-research orchestrator for collecting a structured directory
of startups affiliated with a university (or any institution).

Key differences from the generic research_agent.py:
  • The atomic unit is a STARTUP RECORD, not a free-text "fact".
  • The system inspects its own database after each round to find holes
    (missing founders, missing URLs, etc.) and generates targeted searches.
  • A creative-strategy engine proposes lateral searches (LinkedIn lookups,
    Crunchbase cross-refs, press-release mining) based on what's already known.
  • Deduplication is by normalised company name, not by text hash.
  • Outputs both JSON and CSV (always).

Reuses low-level infra from the sibling files:
  • gemini_tool.py   → LLM calls (GeminiSession)
  • Selenium / uc    → Google search + page scraping

Usage:
    python startup_researcher.py                           # runs perpetually (Ctrl+C to pause, --resume to continue)
    python startup_researcher.py "Every MIT-affiliated startup"
    python startup_researcher.py --resume                  # continue from checkpoint
    python startup_researcher.py --inspect                 # print gap report and exit
    python startup_researcher.py --max-rounds 10           # stop after 10 rounds

Dependencies (same as research_agent.py):
    pip install selenium undetected-chromedriver beautifulsoup4 lxml
    + gemini_tool.py in the same directory
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import textwrap
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError
from urllib.parse import quote_plus, urlparse

from schema import StartupRecord, ExtractionResult, CornellianAffiliation, SearchStrategy, PlannerResponse, GapItem
from evidence import span_present

from metrics import gemini_call, GeminiCallLog, CallOutcome, selenium_fetch, SeleniumFetchLog, RoundMetrics

_SELENIUM_LOG = SeleniumFetchLog(Path("startup_output/selenium_fetches.jsonl"))

_GEMINI_CALL_LOG = GeminiCallLog(Path("startup_output/gemini_calls.jsonl"))

_ROUND_METRICS_HOLDER = {"rm": None}
def _current_round_metrics():
    return _ROUND_METRICS_HOLDER["rm"]

from degradation import DegradationLadder, Level
_LADDER_HOLDER = {"ladder": None}
def _ladder():
    return _LADDER_HOLDER["ladder"]

def _rm_record_selenium(handle):
    _rm = _current_round_metrics()
    if _rm is not None:
        _rm.record_selenium(handle.outcome, latency_ms=0)
    if _ladder() is not None:
        _ladder().observe_selenium(handle.outcome)

from bs4 import BeautifulSoup

# Use the LOCAL gemini_tool.py in this directory. We previously imported the
# canonical copy from ../../pipelines/parcelle_pipeline/, but that file lives
# in a different project and we need response-extractor edits specific to
# the long extraction prompts this script sends. Keep parcelle_pipeline
# untouched; ship our diverged copy here.
_LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
if _LOCAL_DIR not in sys.path:
    sys.path.insert(0, _LOCAL_DIR)
from gemini_tool import GeminiSession

from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    import undetected_chromedriver as uc
except ImportError:
    raise SystemExit("pip install undetected-chromedriver")


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════

DEFAULT_PROMPT = (
    "Find every company where AT LEAST ONE founder is a Cornellian — meaning "
    "they attended Cornell as a student (any school: Cornell University, "
    "Cornell Tech, Weill Cornell Medicine, Cornell Vet, etc.) or were Cornell "
    "faculty / researchers. Include companies of ANY size and age — pre-seed "
    "startups, mid-size private companies, and Fortune 500 public companies "
    "(e.g. Citigroup founded by Sandy Weill '55) all qualify. For each one I "
    "need: company name, the specific Cornellian founder, all co-founders, "
    "description, proof URL, founded year, total funding, funding stage, "
    "employee count, headquarters, and affiliation evidence."
)

CHECKPOINT_FILE       = "startup_checkpoint.json"
COOKIE_FILE           = "browser_cookies.json"
LOG_FILE              = "startup_researcher.log"
OUTPUT_DIR            = "startup_output"

PAGE_TIMEOUT          = 30
BETWEEN_PAGES_MIN     = 2.0
BETWEEN_PAGES_MAX     = 5.0
BETWEEN_SEARCHES_MIN  = 3.0
BETWEEN_SEARCHES_MAX  = 6.0
MAX_RETRIES           = 3
CONSECUTIVE_FAIL_HALT = 6
MAX_RESULTS_PER_QUERY = 10
MAX_PAGE_CHARS        = 60_000
MAX_CONTENT_PER_CALL  = 8_000    # capped so the user message stays small enough
                                 # that the JS response extractor can locate the
                                 # model reply (the ext'r picks the LAST text
                                 # block; if user msg dominates it gets picked
                                 # by mistake). Smaller chunks also reduce
                                 # Gemini's tendency to render output as a table.
MAX_ROUNDS            = 0             # 0 = perpetual (no limit)
RESTART_EVERY         = 200

# Perpetual-mode timing
COOLDOWN_BASE_SECS    = 300          # 5 min cooldown when a cycle finds nothing
COOLDOWN_MAX_SECS     = 3600         # cap at 1 hour
COOLDOWN_BACKOFF      = 1.5          # multiply cooldown on consecutive dry runs
URL_EXPIRY_ROUNDS     = 20           # re-allow visiting a URL after N rounds

# Gap-filling: run a dedicated per-record fill pass every N rounds
GAP_FILL_INTERVAL     = 3            # run targeted fill every 3 rounds
GAP_FILL_BATCH_SIZE   = 15           # how many incomplete records to target per pass

# Parallel scraping
NUM_WORKERS           = 2            # Selenium browser instances per round

# Inline quality control
INLINE_GEMINI_VERIFY_INTERVAL = 7    # Gemini verify sample every N rounds
INLINE_VERIFY_SAMPLE_SIZE     = 20   # records per inline verify pass

# queries_used trim: cap list length to prevent checkpoint bloat
MAX_QUERIES_HISTORY   = 800

# Cookie / login configuration
LOGIN_DOMAINS = [
    {
        "name": "LinkedIn",
        "login_url": "https://www.linkedin.com/login",
        "check_url": "https://www.linkedin.com/feed/",
        "success_indicator": "/feed",   # URL fragment that proves logged-in
    },
    # Add more domains here as needed, e.g.:
    # {
    #     "name": "Crunchbase",
    #     "login_url": "https://www.crunchbase.com/login",
    #     "check_url": "https://www.crunchbase.com/home",
    #     "success_indicator": "/home",
    # },
]

GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}"

# Source credibility tiers (1=untrusted, 5=authoritative). The score is
# attached to each record at scrape time and consulted by `validate_record`
# downstream. Specific-domain matches override the generic .edu/.gov/.org
# defaults.
SOURCE_TIERS: dict[str, int] = {
    ".edu": 5, ".gov": 5, ".org": 4,
    "crunchbase.com": 5, "pitchbook.com": 5, "techcrunch.com": 4,
    "linkedin.com": 3, "reuters.com": 4, "bloomberg.com": 4,
    "forbes.com": 3, "wikipedia.org": 3,
    "medium.com": 2, "reddit.com": 2, "quora.com": 1,
    # Per the Cornell-startups handoff: bigredai.org is a community-curated
    # list of "Big Red Startups" but its accuracy is unverified — treat
    # records sourced *only* from there as provisional unless cross-
    # referenced from a canonical source.
    "bigredai.org": 2,
}

# Non-startup blocklist — large/established companies, orgs, programs, & funds
# that frequently appear on university pages but are NOT university startups.
# Normalised (lowercase, stripped of Inc/LLC/etc.) for matching.

def _normalise_name(name: str) -> str:
    """Normalise a company name for dedup: lowercase, strip Inc/LLC/etc."""
    n = name.strip().lower()
    n = re.sub(r"\s*,?\s*(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|plc\.?)$", "", n)
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n

#  ──────────────────────────────────────────────────────────────────────────
#  NOTE: Big public companies (Citigroup, Goldman, etc.) are NO LONGER
#  blocklisted — they qualify if a Cornellian was a founder. The blocklist
#  is now limited to:
#    1) Funds / VCs / accelerators (NOT companies, regardless of founder)
#    2) University programs, offices, departments, hackathons
#    3) Generic placeholders / hallucinated names with no real referent
#  ──────────────────────────────────────────────────────────────────────────
NON_STARTUP_BLOCKLIST: set[str] = {
    _normalise_name(n) for n in [
        # Funds / VCs / angel groups (not companies, regardless of founders)
        "Upstate Capital", "Cayuga Venture Fund", "Excell",
        "Triphammer Ventures", "Big Red Ventures", "Red Bear Angels",
        "Gorges Ventures", "Dorm Room Fund", "Rough Draft Ventures",
        "BR Ventures", "Chloe Capital", "Canaan Partners",
        "Triple Impact Capital", "Launch Factory",
        # University programs, offices, hackathons (not companies)
        "Rev: Ithaca Startup Works", "Upstate NY I-Corps Node", "START-UP NY",
        "LaunchNY", "FuzeHub", "Grow-NY", "76West", "43North",
        "Southern Tier Startup Alliance",
        "The Business Incubator Association of New York State",
        "STEAMpact Foundation",
        "Weill Cornell Medicine Enterprise Innovation",
        "Center for Technology Licensing at Cornell University (CTL)",
        "Tri-Institutional Therapeutics Discovery Institute, Inc. (Tri-I TDI)",
        "Design Consulting at Cornell",
        "Animal Health Hackathon (2026 Participants)",
        # Placeholder / hallucinated generic entries
        "[Venture in formation]",
        "AI-Driven Crop Optimization", "Advanced Battery Materials",
        "Precision Medicine Platform", "Sustainable Protein Production",
        "Quantum Computing Algorithms", "Smart Infrastructure Sensors",
        "Cornell AI software startup",
    ]
}


def _is_blocklisted(name: str) -> bool:
    """Check if a company name matches the non-startup blocklist."""
    return _normalise_name(name) in NON_STARTUP_BLOCKLIST


def _looks_like_non_startup(record: dict) -> str | None:
    """
    Fast heuristic check. Returns a reason string if the record looks like
    it doesn't belong, or None if it passes.
    """
    name = record.get("company_name", "")
    norm = _normalise_name(name)

    # 1) Explicit blocklist
    if norm in NON_STARTUP_BLOCKLIST:
        return f"blocklisted: '{name}' is a known non-startup / program / fund"

    # 2) Generic placeholder names
    if norm.startswith("[") or norm.startswith("venture in"):
        return f"placeholder entry: '{name}'"

    # 3) Looks like a program/fund/org rather than a company
    org_signals = ["venture fund", "ventures fund", "angel", "capital fund",
                   "incubator", "accelerator program", "i-corps",
                   "hackathon", "startup alliance", "design consulting"]
    name_lower = name.lower()
    for sig in org_signals:
        if sig in name_lower:
            return f"looks like org/program/fund, not a startup: '{name}'"

    return None  # passes heuristic


# ═════════════════════════════════════════════════════════════════════════════
#  RECORD VALIDATION  (tier-based, no false-rejection of alumni Fortune 500s)
# ═════════════════════════════════════════════════════════════════════════════

# Words that suggest a "name" is actually a placeholder or hallucination
_GENERIC_NAME_TOKENS = {
    "ai-driven", "ai driven", "platform", "solution", "solutions", "technology",
    "technologies", "system", "systems", "lorem", "ipsum", "example",
    "todo", "tbd", "n/a", "unknown", "anonymous", "various", "multiple",
}

# Looks-like-a-person-name regex: at least two whitespace-separated tokens,
# each starting with an upper-case letter or unicode letter, no digits.
_HUMAN_NAME_RE = re.compile(r"^[A-ZÀ-Ý][\w\.'\-]+(\s+[A-ZÀ-Ý][\w\.'\-]+)+$")

# A funding figure that's clearly bogus
_FUNDING_MAX = 5_000_000_000_000   # $5 trillion ceiling — anything above is a typo


def _looks_like_human_name(name: str) -> bool:
    """Heuristic: 'Sandy Weill' → True, 'Unknown' → False, 'Various' → False."""
    if not name:
        return False
    n = name.strip()
    if not n or n.lower() in {"unknown", "n/a", "tbd", "anonymous", "various"}:
        return False
    # Reject single-word names (most real founder pages give first + last)
    if " " not in n:
        return False
    return bool(_HUMAN_NAME_RE.match(n))


def _is_url_well_formed(url: str) -> bool:
    """Check URL has scheme + netloc and a real-looking host."""
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc or "." not in parsed.netloc:
        return False
    if parsed.netloc.lower() in ("example.com", "localhost", "n/a", "unknown"):
        return False
    return True


def _coerce_funding_amount(val) -> tuple[Optional[float], Optional[str]]:
    """
    Convert mixed-format funding values into a clean float (USD) plus an
    issue string if anything looks off. Returns (amount_or_None, issue_or_None).
    Accepts: 12000000, "12000000", "$12M", "12.5 million", "1.2B", "" (empty).
    """
    if val in (None, "", "Unknown", "unknown", "N/A", "n/a"):
        return None, None
    if isinstance(val, (int, float)):
        amt = float(val)
        if amt < 0 or amt > _FUNDING_MAX:
            return None, f"funding_total_usd out of plausible range: {amt}"
        return amt, None
    s = str(val).strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
    if not s:
        return None, None
    # Strip trailing "+" / "approx" / "around"
    s = re.sub(r"[+~]+", "", s)
    s = re.sub(r"\b(approx|around|over|under|about|circa)\b", "", s).strip()
    mult = 1.0
    if s.endswith("k") or s.endswith(" thousand"):
        mult = 1e3; s = s.rstrip("k").replace(" thousand", "").strip()
    elif s.endswith("m") or s.endswith(" million"):
        mult = 1e6; s = s.rstrip("m").replace(" million", "").strip()
    elif s.endswith("b") or s.endswith(" billion"):
        mult = 1e9; s = s.rstrip("b").replace(" billion", "").strip()
    elif s.endswith("t") or s.endswith(" trillion"):
        mult = 1e12; s = s.rstrip("t").replace(" trillion", "").strip()
    try:
        amt = float(s) * mult
    except ValueError:
        return None, f"could not parse funding_total_usd: {val!r}"
    if amt < 0 or amt > _FUNDING_MAX:
        return None, f"funding_total_usd out of plausible range: {amt}"
    return amt, None


def _coerce_year(val) -> tuple[Optional[int], Optional[str]]:
    """Coerce a year value to int. Year must be 1800-2030."""
    if val in (None, "", "Unknown", "unknown", "N/A", "n/a"):
        return None, None
    try:
        y = int(str(val).strip())
    except ValueError:
        return None, f"unparseable year: {val!r}"
    if y < 1800 or y > 2030:
        return None, f"year out of plausible range (1800-2030): {y}"
    return y, None


def validate_record(record: dict) -> dict:
    """
    Validate and normalise a record IN PLACE. Sets `validation_tier` and
    `validation_issues`.

    Tiers:
      • "high"        — passes all required checks AND has corroborating
                         signals (≥2 sources, or canonical proof URL like
                         Wikipedia/Crunchbase/SEC)
      • "provisional" — passes required checks but is single-sourced or
                         missing minor fields
      • "weak"        — passes affiliation rule but has soft issues
                         (missing founders, vague description, etc.)

    Returns the record (mutated) so it can be chained.
    """
    issues: list[str] = []

    # ── REQUIRED: company name ────────────────────────────────────────────
    name = (record.get("company_name") or "").strip()
    if not name:
        issues.append("missing company_name")
    elif _normalise_name(name) in NON_STARTUP_BLOCKLIST:
        issues.append("name matches non-startup blocklist (fund/program/placeholder)")
    else:
        # Reject obvious placeholder names
        nlow = name.lower().strip()
        if any(tok == nlow or nlow.startswith(tok + " ") or nlow.endswith(" " + tok)
               for tok in _GENERIC_NAME_TOKENS):
            issues.append(f"company_name looks generic/placeholder: {name!r}")
        if name.startswith("[") or name.endswith("]"):
            issues.append(f"company_name looks like a placeholder: {name!r}")

    # ── REQUIRED: cornellian_founder ──────────────────────────────────────
    cf = (record.get("cornellian_founder") or "").strip()
    affiliation_type = (record.get("affiliation_type") or "").strip().lower()
    if not cf and affiliation_type != "licensed tech":
        issues.append("missing cornellian_founder (required unless Licensed Tech)")
    elif cf and not _looks_like_human_name(cf):
        # Don't reject — Cornellian might be listed by first name only on some
        # pages; flag for follow-up enrichment instead.
        issues.append(f"cornellian_founder doesn't look like a full human name: {cf!r}")

    # ── REQUIRED: proof_url ───────────────────────────────────────────────
    proof_url = (record.get("proof_url") or "").strip()
    if not proof_url:
        issues.append("missing proof_url")
    elif not _is_url_well_formed(proof_url):
        issues.append(f"proof_url malformed: {proof_url!r}")

    # ── REQUIRED: affiliation_evidence ────────────────────────────────────
    evidence = (record.get("affiliation_evidence") or "").strip()
    if not evidence:
        issues.append("missing affiliation_evidence")
    elif len(evidence) < 15:
        issues.append(f"affiliation_evidence too short: {evidence!r}")

    # ── OPTIONAL: funding amounts (total, last round, valuation, exit) ────
    for amt_key in ("funding_total_usd", "last_round_amount_usd",
                    "valuation_usd", "exit_value_usd"):
        amt, amt_issue = _coerce_funding_amount(record.get(amt_key))
        record[amt_key] = amt if amt is not None else ""
        if amt_issue:
            issues.append(f"{amt_key}: {amt_issue}")

    # ── OPTIONAL: years (founded, last round, exit, cornellian grad) ──────
    for ykey in ("founded_year", "funding_last_round_year",
                 "exit_year", "cornellian_grad_year"):
        y, y_issue = _coerce_year(record.get(ykey))
        record[ykey] = y if y is not None else ""
        if y_issue:
            issues.append(f"{ykey}: {y_issue}")

    # ── OPTIONAL: is_public coercion ──────────────────────────────────────
    pub = record.get("is_public")
    if isinstance(pub, str):
        pls = pub.strip().lower()
        if pls in ("true", "yes", "y"):     record["is_public"] = True
        elif pls in ("false", "no", "n"):   record["is_public"] = False
        elif pls in ("", "unknown", "n/a"): record["is_public"] = None
        else: record["is_public"] = None

    # ── OPTIONAL: founders looks like names ───────────────────────────────
    founders = (record.get("founders") or "").strip()
    if founders and founders.lower() not in ("unknown", "n/a", ""):
        # Each comma-separated name should look like a person
        bad_founders = []
        for f in [x.strip() for x in founders.split(",") if x.strip()]:
            if not _looks_like_human_name(f):
                bad_founders.append(f)
        if bad_founders:
            issues.append(f"founders contain non-name tokens: {bad_founders}")

    # ── ASSIGN TIER ───────────────────────────────────────────────────────
    # Required field checks (anything in this list = automatic "weak" or worse)
    required_missing = [i for i in issues if i.startswith("missing ") or i.startswith("name matches")]

    proof_domain = ""
    if proof_url and _is_url_well_formed(proof_url):
        proof_domain = urlparse(proof_url).netloc.lower()
    canonical_sources = ("wikipedia.org", "crunchbase.com", "sec.gov",
                         "pitchbook.com", "linkedin.com/company", "techcrunch.com",
                         "bloomberg.com", "reuters.com", "forbes.com")
    has_canonical = any(s in proof_url.lower() for s in canonical_sources) if proof_url else False
    multi_sourced = len(record.get("all_sources", [])) >= 2

    # Surface a flag when the record is single-sourced AND the source is
    # known to be low-quality (e.g. community-curated lists like
    # bigredai.org whose accuracy is unverified).
    src_cred = record.get("source_credibility")
    if (isinstance(src_cred, int) and src_cred <= 2 and not multi_sourced
            and not has_canonical):
        issues.append(
            f"single-source from low-credibility origin "
            f"(source_credibility={src_cred}); cross-reference recommended"
        )

    if required_missing:
        tier = "weak"
    elif issues:
        tier = "provisional"
    elif has_canonical or multi_sourced:
        tier = "high"
    else:
        tier = "provisional"

    record["validation_tier"] = tier
    record["validation_issues"] = issues
    return record


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE CACHE  (in-memory + file-backed, thread-safe for parallel workers)
# ═════════════════════════════════════════════════════════════════════════════

class PageCache:
    """
    Dict-compatible page cache that persists every scraped page to disk.
    On restart, reloads all cached pages so we never re-download a known URL.
    Thread-safe: multiple Selenium workers can write concurrently.
    """

    def __init__(self, output_dir: str):
        self._dir = os.path.join(output_dir, "cache")
        self._mem: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load_from_disk()

    def _url_hash(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _load_from_disk(self):
        if not os.path.exists(self._dir):
            return
        count = 0
        for fname in os.listdir(self._dir):
            if not fname.endswith(".txt"):
                continue
            try:
                with open(os.path.join(self._dir, fname), "r", encoding="utf-8") as f:
                    content = f.read()
                first_line = content.split("\n", 1)[0]
                if first_line.startswith("URL: "):
                    url = first_line[5:].strip()
                    self._mem[url] = content
                    count += 1
            except Exception:
                pass
        if count:
            UI.found(f"Loaded {count} cached pages from disk (no re-downloads needed)")

    def __contains__(self, url: str) -> bool:
        return url in self._mem

    def __getitem__(self, url: str) -> str:
        return self._mem[url]

    def __setitem__(self, url: str, text: str) -> None:
        self.put(url, text)

    def __len__(self) -> int:
        return len(self._mem)

    def list_keys(self) -> list[str]:
        """Return the stable cache keys (filename stems) of all on-disk entries."""
        if not os.path.exists(self._dir):
            return []
        return [os.path.splitext(f)[0] for f in os.listdir(self._dir) if f.endswith(".txt")]

    def get(self, url: str) -> str | None:
        return self._mem.get(url)

    def put(self, url: str, text: str) -> None:
        with self._lock:
            self._mem[url] = text
            os.makedirs(self._dir, exist_ok=True)
            fname = self._url_hash(url) + ".txt"
            try:
                with open(os.path.join(self._dir, fname), "w", encoding="utf-8") as f:
                    f.write(f"URL: {url}\nCAPTURED: {datetime.now().isoformat()}\n\n{text}")
            except OSError:
                pass


# ═════════════════════════════════════════════════════════════════════════════
#  RECORD SCHEMA
# ═════════════════════════════════════════════════════════════════════════════

RECORD_FIELDS = [
    "company_name",           # required — the canonical name
    "founders",               # comma-separated founder names (or "Unknown")
    "cornellian_founder",     # name of the specific Cornell-affiliated founder (REQUIRED for inclusion)
    "description",            # 1-2 sentence description of what the company does
    "proof_url",              # URL proving the company exists (website/LinkedIn/Crunchbase/press/Wikipedia)
    "website_url",            # company's own website (NEW — distinct from proof_url)
    "linkedin_url",           # company LinkedIn page (NEW — supports the planned employee-count follow-up)
    "affiliation_type",       # Faculty / Alumni / Student / Licensed Tech / Incubator / Unknown
    "affiliation_evidence",   # 1-sentence proof of institutional connection (which Cornell program/year)
    "cornellian_school",      # NEW — which Cornell school/college (e.g. "Engineering", "Cornell Tech",
                              #       "Weill", "CALS", "Johnson", "Vet"). "Unknown" if unspecified.
    "cornellian_grad_year",   # NEW — year the founder graduated/affiliated (int or "Unknown")
    "industry",               # sector or vertical
    "status",                 # Active / Acquired / Closed / Public / Unknown
    "founded_year",           # int year (or "Unknown")
    "funding_total_usd",      # numeric total raised in USD (or "" / "Unknown")
    "funding_stage",          # Pre-seed / Seed / Series A / ... / IPO / Acquired / Bootstrapped / Unknown
    "funding_last_round_year", # int year of latest round (or "Unknown")
    "last_round_amount_usd",  # NEW — most recent round's $ raised (numeric, or "")
    "lead_investors",         # NEW — top 1-3 lead investors, comma-separated
    "valuation_usd",          # NEW — most recent valuation in USD (numeric, or "")
    "employee_count",         # int (or range like "1000-5000") (or "Unknown")
    "is_public",              # True / False / Unknown
    "headquarters",           # city, state/country (or "Unknown")
    "exit_year",              # NEW — year of acquisition or IPO (int or "Unknown")
    "exit_value_usd",         # NEW — acquisition price or IPO market cap (numeric, or "")
    "acquirer",               # NEW — name of acquiring company (if status=Acquired)
    "source_url",             # where we found this record
    "source_credibility",     # 1-5
    "verified",               # True if cross-referenced from ≥2 sources
    "validation_tier",        # "high" / "provisional" / "weak" — set by validate_record()
    "validation_issues",      # list of issues found by validate_record()
    "notes",                  # free-text notes
]

CSV_COLUMNS = [
    "Company Name",
    "Cornellian Founder",
    "All Founders",
    "Description",
    "Proof URL",
    "Website URL",
    "LinkedIn URL",
    "Affiliation Type",
    "Affiliation Evidence",
    "Cornellian School",
    "Cornellian Grad Year",
    "Industry",
    "Status",
    "Founded Year",
    "Funding Total (USD)",
    "Funding Stage",
    "Last Round Year",
    "Last Round Amount (USD)",
    "Lead Investors",
    "Valuation (USD)",
    "Employee Count",
    "Public Company",
    "HQ",
    "Exit Year",
    "Exit Value (USD)",
    "Acquirer",
    "Source URL",
    "Source Credibility",
    "Verified",
    "Validation Tier",
    "Validation Issues",
    "Notes",
]


# ═════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═════════════════════════════════════════════════════════════════════════════

class _SafeFlushHandler(logging.StreamHandler):
    """StreamHandler that swallows flush() errors (Google Drive VFS on Windows)."""
    def flush(self):
        try:
            super().flush()
        except OSError:
            pass

class _SafeFlushFileHandler(logging.FileHandler):
    """FileHandler that swallows flush() errors."""
    def flush(self):
        try:
            super().flush()
        except OSError:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _SafeFlushFileHandler(LOG_FILE, encoding="utf-8"),
        _SafeFlushHandler(),
    ],
)
# Prevent gemini_tool's logger from propagating to root (avoids duplicate console lines).
logging.getLogger("gemini_tool").propagate = False
log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  TERMINAL UI
# ═════════════════════════════════════════════════════════════════════════════

_TERM_WIDTH = min(shutil.get_terminal_size().columns, 90)

class UI:
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    DIM    = "\033[2m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    @staticmethod
    def banner(text):
        print(f"\n{'═' * _TERM_WIDTH}")
        print(f"  {UI.BOLD}{text}{UI.RESET}")
        print(f"{'═' * _TERM_WIDTH}")

    @staticmethod
    def phase(name):
        print(f"\n{'─' * _TERM_WIDTH}")
        print(f"  {UI.CYAN}{UI.BOLD}▶ {name}{UI.RESET}")
        print(f"{'─' * _TERM_WIDTH}")

    @staticmethod
    def thinking(text):
        for line in textwrap.fill(text, width=_TERM_WIDTH - 6).split("\n"):
            print(f"  {UI.DIM}💭 {line}{UI.RESET}")

    @staticmethod
    def action(text):
        print(f"  {UI.BLUE}🔧 {text}{UI.RESET}")

    @staticmethod
    def search(query):
        print(f"  {UI.YELLOW}🔍 Searching: {query}{UI.RESET}")

    @staticmethod
    def reading(url):
        print(f"  {UI.DIM}📄 Reading: {urlparse(url).netloc}{UI.RESET}")

    @staticmethod
    def found(text):
        print(f"  {UI.GREEN}✓ {text}{UI.RESET}")

    @staticmethod
    def warn(text):
        print(f"  {UI.YELLOW}⚠ {text}{UI.RESET}")

    @staticmethod
    def error(text):
        print(f"  {UI.RED}✗ {text}{UI.RESET}")

    @staticmethod
    def progress(round_num, total_records, complete_records, urls_visited):
        bar_len = 20
        ratio = complete_records / max(total_records, 1)
        filled = int(bar_len * ratio)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\n  {UI.BOLD}Round {round_num}{UI.RESET}  "
              f"Records: {total_records}  "
              f"Complete: [{bar}] {complete_records}/{total_records}  "
              f"URLs: {urls_visited}")


# ═════════════════════════════════════════════════════════════════════════════
#  GEMINI SESSION  (persistent browser, stays open for all LLM calls)
# ═════════════════════════════════════════════════════════════════════════════

_gemini: GeminiSession | None = None
_gemini_init_kwargs: dict = {}  # preserved so crash-restarts use the same params

def start_gemini(chrome_major=None):
    global _gemini, _gemini_init_kwargs
    if _gemini is not None:
        return
    UI.action("Starting persistent Gemini session …")
    kwargs = {"verbose": True}
    if chrome_major:
        kwargs["chrome_major"] = chrome_major
    _gemini_init_kwargs = kwargs  # store for restart
    _gemini = GeminiSession(**kwargs)
    _gemini.start()
    UI.found("Gemini session ready.")

def stop_gemini():
    global _gemini
    if _gemini is not None:
        try:
            _gemini.stop()
        except Exception:
            pass
        _gemini = None

class GeminiUnavailable(RuntimeError):
    """Raised when Gemini fails persistently and the caller should give up."""
    pass


def _looks_like_prompt_echo(response: str, prompt: str) -> bool:
    """Detect when the response is just a regurgitation of the prompt.

    The wait loop sometimes captures the user prompt instead of the model reply.
    The last 200 chars of the prompt include the end-of-prompt marker; if that
    tail appears in the response, it's almost certainly an echo.
    """
    if not response or len(response) < 20:
        return False
    tail = prompt[-200:].strip()
    return bool(tail) and tail in response


def call_gemini(prompt: str, label: str = "Gemini") -> str:
    """Send `prompt` to the persistent Gemini session.

    Returns the raw text response, or an empty string if Gemini fails
    (after one restart attempt). Callers should pass the result to
    `_parse_json(..., fallback=...)`, which already handles the empty
    case — no caller should ever crash because this function returned ""
    instead of raising. (Earlier behaviour was to raise RuntimeError on
    empty, which killed long perpetual runs in the middle of the night.)

    Every call now records a structured outcome (parsed / empty /
    prompt_echoed / timeout / crash) to startup_output/gemini_calls.jsonl
    via the metrics.gemini_call context manager.
    """
    global _gemini
    if _gemini is None:
        log.warning("Gemini session not started; cannot call.")
        return ""
    UI.action(f"Calling {label} ({len(prompt):,} chars) …")

    def _attempt(p: str, call) -> tuple[str, bool]:
        """Run one prompt attempt; returns (response, should_return_directly).

        Sets the outcome on `call` and returns the value to return from
        call_gemini. The second tuple element is True if this is a definitive
        answer (parsed / empty / echo) and False if we should restart and retry.
        """
        try:
            out = _gemini.prompt(p)
        except TimeoutException:
            call.set_outcome(CallOutcome.TIMEOUT)
            _rm = _current_round_metrics()
            if _rm is not None:
                _rm.record_gemini(call.outcome, latency_ms=0, label=label)
            if _ladder() is not None:
                _ladder().observe_gemini(call.outcome)
            raise GeminiUnavailable("timeout")
        strategy = getattr(_gemini, "last_extractor_strategy", None)
        call.set_response(out or "", strategy=strategy)
        if not out:
            call.set_outcome(CallOutcome.EMPTY)
            _rm = _current_round_metrics()
            if _rm is not None:
                _rm.record_gemini(call.outcome, latency_ms=0, label=label)
            if _ladder() is not None:
                _ladder().observe_gemini(call.outcome)
            return "", False  # signal: needs restart attempt
        if _looks_like_prompt_echo(out, p):
            call.set_outcome(CallOutcome.PROMPT_ECHOED)
            _rm = _current_round_metrics()
            if _rm is not None:
                _rm.record_gemini(call.outcome, latency_ms=0, label=label)
            if _ladder() is not None:
                _ladder().observe_gemini(call.outcome)
            return "", True
        call.set_outcome(CallOutcome.PARSED)
        _rm = _current_round_metrics()
        if _rm is not None:
            _rm.record_gemini(call.outcome, latency_ms=0, label=label)
        if _ladder() is not None:
            _ladder().observe_gemini(call.outcome)
        return out, True

    try:
        with gemini_call(_GEMINI_CALL_LOG, label=label, prompt=prompt) as call:
            try:
                out, definitive = _attempt(prompt, call)
                if definitive:
                    return out
                UI.warn("  Gemini returned empty response. Restarting session …")
            except GeminiUnavailable:
                raise
            except Exception as exc:
                UI.warn(f"  Gemini call raised {type(exc).__name__}: {exc!r}. "
                        f"Restarting session …")
                # context manager will set CRASH on raise; we want to retry
                # via restart, so handle here instead of propagating.

        # Single restart attempt (logged as its own gemini_call entry).
        try:
            try:
                _gemini.stop()
            except Exception:
                pass
            _gemini = GeminiSession(**_gemini_init_kwargs)
            _gemini.start()
        except Exception as exc:
            log.error(f"  Gemini restart failed: {exc!r}; returning empty.")
            return ""

        with gemini_call(_GEMINI_CALL_LOG, label=f"{label} (retry)", prompt=prompt) as call:
            try:
                out, _definitive = _attempt(prompt, call)
                return out
            except GeminiUnavailable:
                raise
    except GeminiUnavailable:
        raise
    except Exception:
        log.exception("call_gemini crashed")
        return ""
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  SELENIUM HELPERS  (driver init, google search, page scraping)
# ═════════════════════════════════════════════════════════════════════════════

def _detect_chrome_major() -> Optional[int]:
    import platform, subprocess as sp
    plat = platform.system()
    cmds = {
        "Windows": [
            ["reg", "query", r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon", "/v", "version"],
        ],
        "Darwin": [["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"]],
    }.get(plat, [["google-chrome", "--version"], ["chromium-browser", "--version"]])
    for cmd in cmds:
        try:
            out = sp.check_output(cmd, stderr=sp.DEVNULL, timeout=10).decode()
            m = re.search(r"(\d+)\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


_INIT_DRIVER_LOCK = threading.Lock()


def init_driver(headless=True, chrome_major=None):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    for flag in ["--start-maximized", "--disable-notifications",
                 "--disable-popup-blocking", "--lang=en-US",
                 "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-extensions",
                 "--disable-blink-features=AutomationControlled"]:
        options.add_argument(flag)
    version = chrome_major or _detect_chrome_major()
    kw = {"options": options}
    if version:
        kw["version_main"] = version

    # Serialize the FIRST call across workers so undetected_chromedriver's
    # patcher (download/rename) can't race itself. After the first worker
    # patches and renames, subsequent workers reuse the patched executable
    # — they hit the lock briefly but get past instantly. Retry the
    # `uc.Chrome` ctor on FileExistsError as a belt-and-braces measure
    # for cases where the lock isn't enough (e.g. a stale half-patched
    # file from a previous crashed run).
    last_exc = None
    for attempt in range(3):
        try:
            with _INIT_DRIVER_LOCK:
                driver = uc.Chrome(**kw)
            driver.set_page_load_timeout(PAGE_TIMEOUT)
            try:
                driver.minimize_window()
            except Exception:
                pass
            return driver
        except FileExistsError as exc:
            # Patched chromedriver already at the destination; clean up
            # the half-extracted copy and retry. uc's patcher.unzip_package
            # leaves the extracted file at <zip_path>/chromedriver-win32/
            last_exc = exc
            log.warning(f"  init_driver attempt {attempt + 1}: "
                        f"chromedriver patcher race ({exc!r}); retrying.")
            time.sleep(2)
        except Exception as exc:
            last_exc = exc
            log.warning(f"  init_driver attempt {attempt + 1} failed: "
                        f"{type(exc).__name__}: {exc!r}")
            time.sleep(2)
    raise RuntimeError(f"init_driver failed after 3 retries: {last_exc!r}")


def _prompt_with_focused_browser(driver, message: str) -> str:
    """Pop the (possibly minimized) browser to the foreground, ask the user
    to act on it, then re-minimize once they hit Enter. Falls back to a
    plain input() if the WebDriver doesn't support window state changes
    (e.g. some headless drivers).

    Returns whatever input() returned (caller can ignore). On EOFError /
    KeyboardInterrupt, re-raises so the caller's existing handlers fire.
    """
    restored = False
    try:
        # `maximize_window` un-minimizes (Windows treats it as a Restore
        # from minimized) and is supported by every UC chromedriver build
        # we've seen. Keep it cheap — don't focus tabs.
        driver.maximize_window()
        restored = True
    except Exception:
        pass
    try:
        return input(message)
    finally:
        if restored:
            try:
                driver.minimize_window()
            except Exception:
                pass


def warm_up_browser(driver):
    try:
        driver.get("https://www.google.com")
        time.sleep(2)
    except Exception:
        pass
    print("\n" + "=" * 60)
    print("  Chrome is open on google.com.")
    print("  Please accept cookie banners / solve CAPTCHAs if shown.")
    print("  Then press Enter here.")
    print("=" * 60 + "\n")
    _prompt_with_focused_browser(driver, "  Press Enter when ready… ")


# ─── Cookie persistence ─────────────────────────────────────────────────────

def _cookie_path() -> str:
    """Return the absolute path to the cookie file."""
    return os.path.abspath(COOKIE_FILE)


_AUTH_MARKER_COOKIES = {
    # Per-domain "this is a real session" marker. If the existing file has
    # one of these and the new save doesn't, the save is refused -- per
    # wiki/anti-patterns/silent-failure.md 2026-06-07 lesson.
    "linkedin.com": "li_at",
    "google.com": "__Secure-1PSID",
}


def _has_marker(cookies, marker_name, marker_domain):
    return any(
        c.get("name") == marker_name
        and marker_domain in (c.get("domain") or "")
        for c in cookies
    )


def save_cookies(driver, path: str | None = None):
    """Persist all browser cookies to a JSON file.

    Refuses to overwrite an existing file when doing so would lose a known
    auth marker (e.g. LinkedIn li_at). Prevents the no-op-login footgun
    where a script with piped empty stdin saves pre-login junk over a valid
    cookie file. See wiki/anti-patterns/silent-failure.md 2026-06-07.
    """
    path = path or _cookie_path()
    new_cookies = driver.get_cookies()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            for domain, marker in _AUTH_MARKER_COOKIES.items():
                had_marker = _has_marker(existing, marker, domain)
                has_marker = _has_marker(new_cookies, marker, domain)
                if had_marker and not has_marker:
                    log.warning(
                        f"refusing to overwrite {path}: existing has "
                        f"{marker!r} for {domain}, new does not. "
                        f"Likely a no-op login."
                    )
                    UI.warn(
                        f"Refused to overwrite cookies: would lose {marker} "
                        f"for {domain}. New session may not be logged in."
                    )
                    return
        except Exception as e:
            log.warning(f"could not read existing cookie file for marker check: {e}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_cookies, f, ensure_ascii=False, indent=2)
    log.info(f"Saved {len(new_cookies)} cookies → {path}")
    UI.found(f"Saved {len(new_cookies)} cookies to {os.path.basename(path)}")


def load_cookies(driver, path: str | None = None) -> bool:
    """
    Load cookies from disk and inject them into the browser.
    Returns True if cookies were loaded successfully.
    """
    path = path or _cookie_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not cookies:
            return False

        # Group cookies by domain so we can navigate to each domain first
        # (browsers require being on the right domain to set a cookie)
        domain_cookies: dict[str, list[dict]] = {}
        for c in cookies:
            domain = c.get("domain", "").lstrip(".")
            domain_cookies.setdefault(domain, []).append(c)

        loaded = 0
        for domain, clist in domain_cookies.items():
            try:
                # Navigate to the domain so cookie injection works
                driver.get(f"https://{domain}")
                time.sleep(1)
                for c in clist:
                    # Clean up fields that can cause errors
                    cookie = {k: v for k, v in c.items()
                              if k in ("name", "value", "domain", "path",
                                       "secure", "httpOnly", "expiry", "sameSite")}
                    # Ensure sameSite has a valid value
                    if "sameSite" in cookie:
                        valid = {"Strict", "Lax", "None"}
                        if cookie["sameSite"] not in valid:
                            cookie.pop("sameSite")
                    try:
                        driver.add_cookie(cookie)
                        loaded += 1
                    except Exception:
                        pass  # skip individual bad cookies silently
            except Exception as e:
                log.debug(f"Could not load cookies for {domain}: {e}")

        UI.found(f"Loaded {loaded}/{len(cookies)} cookies from {os.path.basename(path)}")
        log.info(f"Loaded {loaded}/{len(cookies)} cookies from {path}")
        return loaded > 0
    except Exception as e:
        UI.warn(f"Could not load cookies: {e}")
        return False


def interactive_login(driver):
    """
    First-run flow: navigate to each LOGIN_DOMAINS entry, let the user
    log in manually, then save all cookies for future sessions.
    """
    if not LOGIN_DOMAINS:
        return

    print("\n" + "=" * 60)
    print("  FIRST-RUN LOGIN")
    print("  ────────────────")
    print("  To avoid being blocked on sites like LinkedIn,")
    print("  please log in to each site when prompted.")
    print("  Your session cookies will be saved locally so")
    print("  you only need to do this once.")
    print("=" * 60 + "\n")

    for site in LOGIN_DOMAINS:
        name = site["name"]
        login_url = site["login_url"]
        check_url = site.get("check_url", "")
        success_frag = site.get("success_indicator", "")

        print(f"\n  → Opening {name} login page …")
        try:
            driver.get(login_url)
            time.sleep(2)
        except Exception as e:
            UI.warn(f"Could not open {name}: {e}")
            continue

        _prompt_with_focused_browser(
            driver,
            f"  Log in to {name} in the browser window, then press Enter here … ",
        )

        # Verify login succeeded (best effort)
        if check_url:
            try:
                driver.get(check_url)
                time.sleep(2)
                if success_frag and success_frag in driver.current_url:
                    UI.found(f"{name} login verified ✓")
                else:
                    UI.warn(f"{name} login could not be verified "
                            f"(current URL: {driver.current_url}). Proceeding anyway.")
            except Exception:
                UI.warn(f"Could not verify {name} login. Proceeding anyway.")

    # Save all accumulated cookies
    save_cookies(driver)
    print()


def setup_browser_session(driver, headless: bool):
    """
    High-level session setup:
    - If cookies exist on disk → load them (skip interactive login).
    - If no cookies → run interactive login flow (non-headless only),
      then save cookies.
    - Always warm up the browser on Google afterward.
    """
    cookie_path = _cookie_path()
    has_cookies = os.path.exists(cookie_path)

    if has_cookies:
        UI.action("Loading saved cookies …")
        loaded = load_cookies(driver)
        if loaded:
            UI.found("Session cookies restored from previous run.")
        else:
            UI.warn("Cookie file exists but no cookies could be loaded.")
            if not headless:
                interactive_login(driver)
    else:
        if not headless:
            interactive_login(driver)
        else:
            UI.warn("No saved cookies and running headless — "
                    "LinkedIn scraping may be limited. "
                    "Run once without --headless to log in.")

    # Always warm up on Google (accept banners, etc.)
    if not headless:
        warm_up_browser(driver)


def google_search(driver, query: str) -> list[str]:
    encoded = quote_plus(query)
    url = GOOGLE_SEARCH_URL.format(query=encoded)
    for attempt in range(MAX_RETRIES):
        try:
            driver.get(url)
            WebDriverWait(driver, PAGE_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1.5)
            page = driver.page_source.lower()
            current_url = (driver.current_url or "").lower()
            # Detect a real CAPTCHA challenge (not pages that merely mention
            # the word "captcha" in help links or zero-result tips). Google
            # serves challenges from /sorry/ or with a recaptcha widget /
            # captcha-form. The "unusual traffic" phrase only appears on the
            # actual interstitial.
            is_captcha = (
                "/sorry/" in current_url
                or "google.com/sorry" in current_url
                or "our systems have detected unusual traffic" in page
                or 'id="captcha-form"' in page
                or "g-recaptcha" in page
                or "recaptcha/api" in page
            )
            if is_captcha:
                interactive = sys.stdin.isatty()
                if interactive:
                    UI.warn("CAPTCHA detected — popping the Chrome window forward. "
                            "Solve it; the script will auto-continue when it clears.")
                    restored = False
                    try:
                        driver.maximize_window()
                        restored = True
                    except Exception:
                        pass

                    # Poll the page for the CAPTCHA challenge to disappear.
                    # No Enter keypress required — once Google routes us
                    # back to a real results page, we proceed.
                    SOLVE_DEADLINE = time.time() + 600  # 10 min cap
                    cleared = False
                    while time.time() < SOLVE_DEADLINE:
                        time.sleep(1.5)
                        try:
                            now = driver.page_source.lower()
                            now_url = (driver.current_url or "").lower()
                        except (TimeoutException, WebDriverException):
                            continue
                        still_blocked = (
                            "/sorry/" in now_url
                            or "our systems have detected unusual traffic" in now
                            or 'id="captcha-form"' in now
                            or "g-recaptcha" in now
                            or "recaptcha/api" in now
                        )
                        if not still_blocked:
                            cleared = True
                            break

                    if restored:
                        try:
                            driver.minimize_window()
                        except Exception:
                            pass

                    if cleared:
                        UI.found("CAPTCHA cleared — extracting results from "
                                 "the current page (no re-fetch).")
                        # Fall through to the extractor below using the
                        # post-CAPTCHA page Google just redirected us to.
                        # (Don't `continue` — re-fetching the same URL can
                        # immediately re-trigger CAPTCHA.)
                    else:
                        log.warning(
                            "CAPTCHA not cleared within 10 min — "
                            "treating as non-interactive and skipping."
                        )
                        interactive = False
                if not interactive:
                    log.warning(
                        "CAPTCHA detected in non-TTY context — "
                        f"backing off (attempt {attempt + 1}/{MAX_RETRIES}) "
                        "and skipping query if it persists."
                    )
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(30)
                        continue
                    return []
            elems = driver.find_elements(By.CSS_SELECTOR, "div#search a[href]")
            urls, seen = [], set()
            for el in elems:
                href = el.get_attribute("href") or ""
                if not href.startswith("http"):
                    continue
                d = urlparse(href).netloc
                if any(s in d for s in ["google.com", "google.", "gstatic",
                                        "googleapis", "youtube.com", "webcache"]):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                urls.append(href)
                if len(urls) >= MAX_RESULTS_PER_QUERY:
                    break
            return urls
        except (TimeoutException, WebDriverException):
            time.sleep(3)
    return []


# Domains that require JavaScript rendering — always use Selenium for these.
_JS_HEAVY_DOMAINS = frozenset({
    "linkedin.com", "crunchbase.com", "pitchbook.com",
    "angel.co", "wellfound.com",
    # Cornell SPA-style sites that hydrate after readyState=complete:
    "eship.cornell.edu",
})


def _wait_for_body_to_stabilise(driver, max_wait: float = 8.0,
                                stable_for: float = 1.0,
                                poll: float = 0.4) -> int:
    """Poll document.body.innerText.length until it stops growing.

    Many SPAs (Cornell eship, Vue/React apps) report readyState=complete the
    moment the shell loads, then hydrate content over the next few seconds.
    A fixed sleep is brittle; poll until the length is unchanged for
    `stable_for` seconds, capped at `max_wait`. Returns the final length.
    """
    deadline = time.time() + max_wait
    last_len = -1
    stable_since = None
    while time.time() < deadline:
        try:
            cur = driver.execute_script(
                "return (document.body && document.body.innerText)"
                "  ? document.body.innerText.length : 0;"
            ) or 0
        except Exception:
            cur = 0
        if cur != last_len:
            last_len = cur
            stable_since = time.time()
        elif stable_since and (time.time() - stable_since) >= stable_for:
            return cur
        time.sleep(poll)
    return last_len

def _soup_to_text(soup: BeautifulSoup, url: str) -> tuple[str, str]:
    """Shared HTML → clean text extractor used by both HTTP and Selenium paths."""
    title = ""
    t = soup.find("title")
    if t:
        title = t.get_text(strip=True)
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "form", "noscript", "iframe",
                     "svg", "button", "input", "select", "textarea"]):
        tag.decompose()
    container = (soup.find("article") or soup.find("main")
                 or soup.find("div", {"role": "main"}) or soup.find("body"))
    if not container:
        return "", "empty"
    text = container.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)[:MAX_PAGE_CHARS]
    if len(text) < 50:
        return "", "empty"
    if title:
        text = f"[Page title: {title}]\n[URL: {url}]\n\n{text}"
    return text, "ok"


def _http_scrape(url: str) -> tuple[str, str]:
    """
    Plain HTTP GET — no Chrome window, zero bot risk for static pages.
    Returns (text, status) where status is 'ok' | 'empty' | 'blocked' | 'error'.
    'blocked' signals the caller to fall back to Selenium.
    """
    try:
        import requests as _req
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        resp = _req.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code in (403, 429, 503):
            return "", "blocked"
        if resp.status_code != 200:
            return "", "error"
        soup = BeautifulSoup(resp.text, "lxml")
        return _soup_to_text(soup, url)
    except Exception:
        return "", "error"


def scrape_page(driver, url: str, cache) -> tuple[str, str]:
    """
    Fetch a page and return (text, status).
    Strategy: HTTP-first for static pages, Selenium fallback for JS-heavy domains
    or when HTTP is blocked/fails.  Results are stored in cache (PageCache or dict).
    """
    if url in cache:
        with selenium_fetch(_SELENIUM_LOG, url=url, path="cache") as _h:
            cached_text = cache[url]
            _h.set_result(chars=len(cached_text), outcome="ok")
            _rm_record_selenium(_h)
            return cached_text, "ok"

    domain = urlparse(url).netloc.lower()
    needs_selenium = any(d in domain for d in _JS_HEAVY_DOMAINS)

    if not needs_selenium:
        with selenium_fetch(_SELENIUM_LOG, url=url, path="http") as _h:
            text, status = _http_scrape(url)
            if status == "ok":
                cache[url] = text
                _h.set_result(chars=len(text), outcome="ok")
                _rm_record_selenium(_h)
                return text, "ok"
            # "empty", "blocked", "error" — fall through to Selenium for a second
            # chance. Static pages sometimes return empty body to plain requests
            # but render full content under a real browser (anti-bot defences).
            _http_outcome = "empty" if status == "empty" else ("blocked" if status == "blocked" else "crash")
            _h.set_result(chars=len(text or ""), outcome=_http_outcome)
            _rm_record_selenium(_h)

    # Selenium path: required for JS-heavy sites or when HTTP failed
    # TODO: migrate to retry_policy.retry (currently tangled with HTTP-fallback
    # and selenium-handle side effects; see retry_policy.py for bounded
    # backoff + jitter + error classification)
    with selenium_fetch(_SELENIUM_LOG, url=url, path="selenium") as _h:
        for attempt in range(MAX_RETRIES):
            try:
                driver.get(url)
                WebDriverWait(driver, PAGE_TIMEOUT).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                # readyState=complete fires before SPA hydration on many sites;
                # poll for the body text to stabilise instead of a fixed sleep.
                _wait_for_body_to_stabilise(driver, max_wait=8.0, stable_for=1.0)
                soup = BeautifulSoup(driver.page_source, "lxml")
                text, status = _soup_to_text(soup, url)
                if status == "ok":
                    cache[url] = text
                    _h.set_result(chars=len(text), outcome="ok")
                    _rm_record_selenium(_h)
                    return text, status
                # Still empty — give it one more chance with a longer settle window
                # before declaring the page truly empty.
                if status == "empty" and attempt < MAX_RETRIES - 1:
                    _wait_for_body_to_stabilise(driver, max_wait=10.0, stable_for=2.0)
                    soup = BeautifulSoup(driver.page_source, "lxml")
                    text, status = _soup_to_text(soup, url)
                    if status == "ok":
                        cache[url] = text
                        _h.set_result(chars=len(text), outcome="ok")
                        _rm_record_selenium(_h)
                        return text, status
                _h.set_result(chars=len(text or ""), outcome="empty" if status == "empty" else status)
                _rm_record_selenium(_h)
                return text, status
            except (TimeoutException, WebDriverException):
                if attempt < MAX_RETRIES - 1:
                    time.sleep(3)
                else:
                    _h.set_result(chars=0, outcome="timeout")
                    _rm_record_selenium(_h)
            except Exception:
                _h.set_result(chars=0, outcome="crash")
                _rm_record_selenium(_h)
                break
        _h.set_result(chars=0, outcome="empty")
        _rm_record_selenium(_h)
        return "", "error"


def score_source(url: str) -> int:
    """Match the LONGEST pattern in SOURCE_TIERS that's a substring of the
    URL's domain — so a specific entry like "bigredai.org" wins over the
    generic ".org" tier. Default tier when nothing matches: 2."""
    domain = urlparse(url).netloc.lower()
    matched = sorted(
        ((pat, tier) for pat, tier in SOURCE_TIERS.items() if pat in domain),
        key=lambda x: -len(x[0]),
    )
    return matched[0][1] if matched else 2


# ═════════════════════════════════════════════════════════════════════════════
#  JSON PARSING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

# Unique marker appended at the END of every extract prompt. The Gemini-
# response extractor (in gemini_tool.py) sometimes captures the user prompt
# as well as (or instead of) the model reply. If the marker shows up in the
# captured text, we know everything BEFORE its last occurrence is prompt
# echo and only what comes AFTER is candidate model output. Pick a string
# that won't appear in real Cornell company data.
_END_OF_PROMPT_MARKER = "===END_PROMPT===GEMINI_RESPONSE_BELOW==="

def _clean_json(raw: str) -> str:
    """Strip prose / fences around a JSON payload.

    Order of attempts:
      1. If the captured text echoes our prompt's end-of-prompt marker, slice
         everything after the LAST occurrence — that's the candidate model reply.
      2. If the (possibly sliced) text contains a ``` fence, return the LARGEST
         fenced block.
      3. Otherwise strip a leading ```json fence + trailing ```.
      4. Otherwise return the stripped text.
    """
    text = raw.strip()
    if _END_OF_PROMPT_MARKER in text:
        text = text.rsplit(_END_OF_PROMPT_MARKER, 1)[1].strip()
    fences = _FENCE_RE.findall(text)
    if fences:
        return max(fences, key=len).strip()
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

_PARSE_FAIL_LOG_PATH: str = "gemini_parse_failures.log"

_T = TypeVar("_T", bound=BaseModel)


def _parse_json_typed(text: str, model_cls: Type[_T]) -> tuple[Optional[_T], str]:
    """Returns (model_instance, outcome_string).

    outcome in {"parsed", "fence_extracted", "marker_sliced", "schema_invalid", "empty"}
    """
    if not text or not text.strip():
        return None, "empty"

    # Marker slice
    sliced = text
    outcome = "parsed"
    if _END_OF_PROMPT_MARKER in text:
        sliced = text.rsplit(_END_OF_PROMPT_MARKER, 1)[1]
        outcome = "marker_sliced"

    # Fence extract -- take the LARGEST fenced json block first
    fences = _FENCE_RE.findall(sliced)
    candidates = sorted(fences, key=len, reverse=True) if fences else []
    if candidates:
        outcome = "fence_extracted" if outcome == "parsed" else outcome
        for cand in candidates:
            try:
                return model_cls.model_validate_json(cand.strip()), outcome
            except (ValidationError, ValueError):
                continue

    # Try the raw sliced text
    try:
        return model_cls.model_validate_json(sliced.strip()), outcome
    except (ValidationError, ValueError) as e:
        _log_parse_failure(text, model_cls, e)
        return None, "schema_invalid"


def _log_parse_failure(text: str, model_cls, exc) -> None:
    failure_log = Path("startup_output") / "gemini_parse_failures.log"
    failure_log.parent.mkdir(parents=True, exist_ok=True)
    with failure_log.open("a", encoding="utf-8") as f:
        f.write(f"=== {datetime.utcnow().isoformat()} | {model_cls.__name__} ===\n")
        f.write(f"error: {exc}\n")
        f.write(f"raw_len={len(text)} has_marker={_END_OF_PROMPT_MARKER in text} ")
        f.write(f"has_fence={'```json' in text}\n")
        f.write(text[:8000])
        f.write("\n=== END ===\n\n")


# ═════════════════════════════════════════════════════════════════════════════
#  PASS-1 / PASS-2 EXTRACTION PROMPTS  (evidence-span guarded)
# ═════════════════════════════════════════════════════════════════════════════

_PROMPT_HARD_CAP = 45_000  # below the 50KB cliff; see wiki/site-profiles/gemini-web.md


_PASS1_HEADER = (
    "You are an extractor, not a researcher. Read the text below and extract every company "
    "where AT LEAST ONE founder, co-founder, or significant role-holder is a Cornellian "
    "(alumnus, faculty, student, postdoc, or researcher of any Cornell school: CU Ithaca, "
    "Cornell Tech, Weill Cornell Medicine, or Cornell Vet).\n\n"
    "RULES:\n"
    "- Use ONLY the text provided below. Do not recall, infer, or estimate from prior knowledge.\n"
    "- For every non-null value, include the substring of the text supporting it in `evidence_span`.\n"
    "- If a field is not stated in the text, return null. Do not guess.\n"
    "- Output a single ```json fenced code block containing one ExtractionResult object.\n"
    "- The marker on the last line is the boundary; nothing useful comes before it.\n\n"
)


def _build_pass1_prompt(text: str) -> str:
    schema_excerpt = json.dumps({
        "records": [{
            "company_name": "string",
            "cornellians": [{
                "name": "string", "school": "CU|Cornell Tech|Weill|Vet|unknown",
                "role": "alumnus|faculty|student|postdoc|researcher",
                "grad_year": "int or null", "role_at_company":
                "founder|cofounder|ceo|cto|early_employee|board|investor|advisor",
                "evidence_span": "string (must be a substring of input)",
                "source_url": "string",
            }],
            "proof_url": "string",
            "status": "active|acquired|shutdown|ipo|unknown",
            "funding_total_usd": "int or null",
            "founded_year": "int or null",
        }],
        "notes": "string",
    }, indent=2)
    return (
        _PASS1_HEADER
        + "JSON SHAPE (every field listed; return null when not stated):\n"
        + f"```json\n{schema_excerpt}\n```\n\n"
        + "TEXT TO EXTRACT FROM:\n"
        + text
        + f"\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


def _extract_pass1(page_text: str, source_url: str) -> list[StartupRecord]:
    prompt = _build_pass1_prompt(page_text)
    if len(prompt) > _PROMPT_HARD_CAP:
        overhead = len(prompt) - len(page_text)
        budget = _PROMPT_HARD_CAP - overhead - 500
        prompt = _build_pass1_prompt(page_text[:budget])
        log.warning("pass1 prompt exceeded 45K; trimmed page_text to %d", budget)
    response = call_gemini(prompt, label="extract_pass1")
    result, outcome = _parse_json_typed(response, ExtractionResult)
    if result is None:
        return []
    out: list[StartupRecord] = []
    for r in result.records:
        # Override proof_url with the actual source URL (Gemini sometimes invents one)
        r.proof_url = source_url
        # Evidence-span validation
        kept_cornellians = [a for a in r.cornellians
                            if span_present(a.evidence_span, page_text)]
        if not kept_cornellians:
            continue   # drop record: no verifiable affiliation
        r.cornellians = kept_cornellians
        out.append(r)
    return out


def _build_pass2_prompt(record: StartupRecord, page_text: str) -> str | None:
    """Return None if no pass-2 fields are warranted for this record."""
    asks: list[str] = []
    if record.status == "acquired":
        asks.append("exit_year (int or null)")
        asks.append("acquirer (string or null)")
        asks.append("acquisition_amount_usd (int or null; coerce $1.2B -> 1200000000)")
    if record.funding_total_usd is not None and record.funding_total_usd > 0:
        asks.append("funding_stage (pre-seed|seed|series-a|...|growth|public|unknown)")
        asks.append("funding_last_round_year (int or null)")
    if len(record.cornellians) > 1 or len([c for c in record.cornellians
                                            if c.role_at_company in ("founder", "cofounder")]) > 1:
        asks.append("non_cornell_cofounder_schools (list of strings, the other founders' universities)")
    # Always potentially valuable when the source page is the company's about page
    asks.append("description (one sentence)")
    asks.append("industry (string)")
    asks.append("tags (list of short classifier strings)")
    asks.append("headquarters (string)")
    asks.append("website_url (string or null)")
    asks.append("linkedin_company_url (string or null, only if stated in text)")
    asks.append("crunchbase_url (string or null, only if stated in text)")
    asks.append("employee_count (int or null)")
    asks.append("founded_year (int or null, if not already known)")
    if not asks:
        return None
    return (
        "Read the text below and return ONLY the following fields for the company named "
        f"\"{record.company_name}\". Use the text only -- do not recall or estimate.\n\n"
        f"Fields requested:\n- " + "\n- ".join(asks)
        + "\n\nFor every non-null value include `<field>_evidence_span` as a substring of the text.\n"
        + "Output one ```json fenced block, schema: {\"company_name\": ..., ...}.\n\n"
        + "TEXT:\n" + page_text
        + f"\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )


def _extract_pass2(record: StartupRecord, page_text: str) -> StartupRecord:
    prompt = _build_pass2_prompt(record, page_text)
    if prompt is None:
        return record
    response = call_gemini(prompt, label="extract_pass2")
    try:
        cleaned = _slice_and_unfence(response)
        data = json.loads(cleaned)
    except (ValueError, json.JSONDecodeError):
        return record

    for field, value in data.items():
        if field.endswith("_evidence_span"):
            continue
        if field == "company_name":
            continue
        if value is None or value == "":
            continue
        span_field = f"{field}_evidence_span"
        span = data.get(span_field)
        if span and not span_present(span, page_text):
            continue
        try:
            setattr(record, field, value)
        except (AttributeError, ValidationError):
            continue
    return record


def _simple_chunk(text: str, size: int, overlap: int) -> list[str]:
    """Minimal sliding-window chunker; used by extract_from_page."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    step = max(size - overlap, 1)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
        if start >= len(text):
            break
    # Cap at 12 chunks — covers ~60K of content with 8K chunks; beyond that
    # the page is probably navigation noise.
    return chunks[:12]


def extract_from_page(page_text: str, source_url: str) -> list[StartupRecord]:
    """Two-pass extraction with degradation-aware schema mode.

    A9: replaces the single-pass `_extract_startups_chunk` flow with
    pass1 (discovery + evidence-validated) plus optional pass2 (enrichment)
    when the ladder is at NORMAL. At DEMOTED we skip pass2 and use smaller
    chunks; at SCRAPE_ONLY or worse we skip extraction entirely.
    """
    level = _ladder().level if _ladder() else Level.NORMAL
    if level >= Level.SCRAPE_ONLY:
        return []
    page_text = (page_text or "").strip()
    if not page_text:
        return []

    chunk_size = 15000 if level == Level.DEMOTED else 30000
    chunks = _simple_chunk(page_text, chunk_size, 1000)

    out: list[StartupRecord] = []
    seen_names: set[str] = set()
    for chunk in chunks:
        pass1 = _extract_pass1(chunk, source_url)
        for rec in pass1:
            key = _normalise_name(rec.company_name)
            if not key or key in seen_names:
                continue
            seen_names.add(key)
            if level == Level.NORMAL:
                rec = _extract_pass2(rec, chunk)
            out.append(rec)
    return out


def _slice_and_unfence(text: str) -> str:
    sliced = text.rsplit(_END_OF_PROMPT_MARKER, 1)[1] if _END_OF_PROMPT_MARKER in text else text
    fences = _FENCE_RE.findall(sliced)
    return max(fences, key=len) if fences else sliced.strip()


def _parse_json(raw: str, fallback=None):
    cleaned = _clean_json(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Try to find the outermost JSON structure
    for opener, closer in [("[", "]"), ("{", "}")]:  # try array first since we expect arrays from extract
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    # Save the failed response for debugging
    log.warning(f"Could not parse Gemini JSON response (raw_len={len(raw)}, "
                f"cleaned_len={len(cleaned)}, "
                f"has_fence={'```' in raw}, "
                f"has_bracket={'[' in cleaned})")
    try:
        with open(_PARSE_FAIL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now().isoformat()} (raw_len={len(raw)}) ===\n")
            f.write(raw[:8000])
            f.write("\n")
    except Exception:
        pass
    return fallback if fallback is not None else {}


# ═════════════════════════════════════════════════════════════════════════════
#  STARTUP DATABASE  (the core data structure)
# ═════════════════════════════════════════════════════════════════════════════


class StartupDB:
    """
    In-memory database of startup records with JSON persistence.
    Records are keyed by normalised company name for dedup.
    """

    def __init__(self, path, conflict_log=None):
        self.path = str(path)
        self.conflict_log = Path(conflict_log) if conflict_log else Path(self.path).parent / "merge_conflicts.jsonl"
        self.records: dict[str, dict] = {}  # norm_name → record
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for r in data:
                        key = _normalise_name(r.get("company_name", ""))
                        if key:
                            self.records[key] = r
                elif isinstance(data, dict) and "records" in data:
                    for r in data["records"]:
                        key = _normalise_name(r.get("company_name", ""))
                        if key:
                            self.records[key] = r
            except Exception as e:
                log.warning(f"Could not load DB: {e}")

    def save(self):
        """Persist DB atomically. Writes to a sibling temp file then renames
        over the target. Retries up to 5 times because the production
        directory is on Google Drive, which intermittently holds files
        open for sync — that races with our write and gives
        `OSError 22 / 33 (sharing violation)`."""
        payload = {
            "records": list(self.records.values()),
            "count": len(self.records),
            "last_updated": datetime.now().isoformat(),
        }
        tmp_path = self.path + ".tmp"
        last_err = None
        for attempt in range(5):
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                # os.replace is atomic on Windows when both paths are on
                # the same volume. If Drive has the target locked, this
                # raises PermissionError — retry below.
                os.replace(tmp_path, self.path)
                return
            except (OSError, PermissionError) as e:
                last_err = e
                log.warning(f"  db.save attempt {attempt + 1}/5 failed: {e!r}")
                # Clean up stray tmp on failure
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                # Back off — Drive sync usually completes in a few seconds.
                time.sleep(2 + attempt * 2)
        # All retries failed: log loudly but don't kill the run.
        # Caller (run loop) will re-attempt at next round.
        log.error(f"  db.save FAILED after 5 retries; data remains in memory. "
                  f"Last error: {last_err!r}")

    def _log_conflict(self, key, field, old_v, new_v):
        self.conflict_log.parent.mkdir(parents=True, exist_ok=True)
        with self.conflict_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "record": key, "field": field,
                "kept": old_v, "rejected": new_v,
            }) + "\n")

    def upsert(self, record) -> bool:
        """Insert or merge a record. Accepts StartupRecord (Pydantic) or dict (legacy).

        For Pydantic StartupRecord: unions list fields (cornellians by name,
        validation_issues, tags, non_cornell_cofounder_schools), fills missing
        scalars, logs conflicts when both populated and differ.

        For dict (legacy): preserves old behavior, including blocklist gate and
        hard rule that requires a cornellian_founder.

        Returns True if record was new."""
        # ── Pydantic StartupRecord branch ─────────────────────────────────
        if hasattr(record, "model_dump"):
            new = record.model_dump(mode="json")
            name = (record.company_name or "").strip()
            if not name:
                return False
            key = _normalise_name(name)
            if not key:
                return False

            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            if key not in self.records:
                new["first_seen_at"] = new.get("first_seen_at") or now
                new["last_verified_at"] = now
                # Re-validate so validation_tier reflects current state
                try:
                    validate_record(new)
                except Exception as e:
                    log.warning("revalidate after upsert failed: %s", e)
                self.records[key] = new
                return True

            existing = self.records[key]
            existing["last_verified_at"] = now

            # List fields: union and dedupe
            for field in ("validation_issues", "tags", "non_cornell_cofounder_schools"):
                merged_list, seen = [], set()
                for x in (existing.get(field) or []) + (new.get(field) or []):
                    if x and x not in seen:
                        merged_list.append(x); seen.add(x)
                existing[field] = merged_list

            # Cornellians: union by name
            existing_corn = {c["name"]: c for c in existing.get("cornellians", []) if isinstance(c, dict) and c.get("name")}
            for c in new.get("cornellians", []):
                if isinstance(c, dict) and c.get("name"):
                    existing_corn.setdefault(c["name"], c)
            existing["cornellians"] = list(existing_corn.values())

            # Scalars: fill if missing; log conflicts if both populated and differ
            scalar_fields = (
                "description", "industry", "funding_total_usd", "funding_stage",
                "funding_last_round_year", "founded_year", "employee_count",
                "is_public", "headquarters", "status", "exit_year", "acquirer",
                "acquisition_amount_usd", "website_url", "linkedin_company_url",
                "crunchbase_url",
            )
            for field in scalar_fields:
                old_v, new_v = existing.get(field), new.get(field)
                if old_v in (None, "", "unknown") and new_v not in (None, "", "unknown"):
                    existing[field] = new_v
                elif old_v and new_v and old_v != new_v and old_v != "unknown" and new_v != "unknown":
                    self._log_conflict(key, field, old_v, new_v)
                    # Keep old
            # Re-validate so validation_tier reflects current state
            try:
                validate_record(existing)
            except Exception as e:
                log.warning("revalidate after upsert failed: %s", e)
            return False

        # ── Legacy dict branch (preserves old behavior) ───────────────────
        name = record.get("company_name", "").strip()
        if not name:
            return False
        key = _normalise_name(name)
        if not key:
            return False

        # ── Gate: reject blocklisted / heuristically-bad records ──────
        if key in NON_STARTUP_BLOCKLIST:
            log.info(f"Blocked insert of non-startup: {name}")
            return False
        reason = _looks_like_non_startup(record)
        if reason:
            log.info(f"Blocked insert: {reason}")
            return False

        # ── Hard rule: must have a cornellian_founder OR be Licensed Tech
        cf = (record.get("cornellian_founder") or "").strip()
        aff = (record.get("affiliation_type") or "").strip().lower()
        if not cf and aff != "licensed tech":
            log.info(f"Blocked insert (no Cornellian founder identified): {name}")
            return False

        if key in self.records:
            # Merge: fill blanks and append sources
            existing = self.records[key]
            for field in RECORD_FIELDS:
                old_val = existing.get(field, "")
                new_val = record.get(field, "")
                # Skip overwriting list/dict/bool fields with empty merges
                if isinstance(new_val, (list, dict)):
                    if not old_val and new_val:
                        existing[field] = new_val
                    continue
                if (not old_val or old_val in ("Unknown", "unknown", "")) and new_val:
                    existing[field] = new_val
            # If we now have ≥2 distinct source URLs, mark verified
            old_src = existing.get("source_url", "")
            new_src = record.get("source_url", "")
            if old_src and new_src and old_src != new_src:
                existing["verified"] = True
                # Keep a list of all sources
                all_srcs = set(existing.get("all_sources", [old_src]))
                all_srcs.add(new_src)
                existing["all_sources"] = list(all_srcs)
            validate_record(existing)
            self.records[key] = existing
            return False
        else:
            record.setdefault("verified", False)
            record.setdefault("all_sources", [record.get("source_url", "")])
            validate_record(record)
            self.records[key] = record
            return True

    def remove(self, name: str) -> bool:
        """Remove a record by company name. Returns True if it existed."""
        key = _normalise_name(name)
        if key in self.records:
            del self.records[key]
            return True
        return False

    def remove_many(self, names: list[str]) -> int:
        """Remove multiple records. Returns count of records removed."""
        removed = 0
        for name in names:
            if self.remove(name):
                removed += 1
        return removed

    def all_records(self) -> list[dict]:
        return list(self.records.values())

    def count(self) -> int:
        return len(self.records)

    # ── Gap analysis ──────────────────────────────────────────────────────

    def gap_report(self) -> dict:
        """Analyse the database and return a structured gap report."""
        total = len(self.records)
        if total == 0:
            return {
                "total": 0,
                "missing_founders": [],
                "missing_proof_url": [],
                "missing_description": [],
                "unverified": [],
                "complete_count": 0,
                "summary": "Database is empty.",
            }

        missing_founders  = []
        missing_url       = []
        missing_desc      = []
        unverified        = []

        for key, r in self.records.items():
            name = r.get("company_name", key)
            f = (r.get("founders") or "").strip().lower()
            if not f or f in ("unknown", "n/a", ""):
                missing_founders.append(name)
            u = (r.get("proof_url") or "").strip()
            if not u or u.lower() in ("unknown", "n/a", ""):
                missing_url.append(name)
            d = (r.get("description") or "").strip()
            if not d or d.lower() in ("unknown", "n/a", ""):
                missing_desc.append(name)
            if not r.get("verified"):
                unverified.append(name)

        return {
            "total": total,
            "missing_founders": missing_founders,
            "missing_proof_url": missing_url,
            "missing_description": missing_desc,
            "unverified": unverified,
            "complete_count": total - len(
                set(missing_founders) | set(missing_url) | set(missing_desc)
            ),
            "summary": (
                f"{total} records total. "
                f"{len(missing_founders)} missing founders, "
                f"{len(missing_url)} missing proof URL, "
                f"{len(missing_desc)} missing description, "
                f"{len(unverified)} unverified (single-source)."
            ),
        }

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_COLUMNS)
        for r in sorted(self.records.values(),
                        key=lambda x: x.get("company_name", "").lower()):
            issues = r.get("validation_issues", [])
            issues_str = "; ".join(issues) if isinstance(issues, list) else str(issues)
            is_pub = r.get("is_public")
            pub_str = ("Yes" if is_pub is True else
                       "No"  if is_pub is False else
                       str(is_pub) if is_pub else "Unknown")
            writer.writerow([
                r.get("company_name", ""),
                r.get("cornellian_founder", ""),
                r.get("founders", ""),
                r.get("description", ""),
                r.get("proof_url", ""),
                r.get("website_url", ""),
                r.get("linkedin_url", ""),
                r.get("affiliation_type", ""),
                r.get("affiliation_evidence", ""),
                r.get("cornellian_school", ""),
                r.get("cornellian_grad_year", ""),
                r.get("industry", ""),
                r.get("status", ""),
                r.get("founded_year", ""),
                r.get("funding_total_usd", ""),
                r.get("funding_stage", ""),
                r.get("funding_last_round_year", ""),
                r.get("last_round_amount_usd", ""),
                r.get("lead_investors", ""),
                r.get("valuation_usd", ""),
                r.get("employee_count", ""),
                pub_str,
                r.get("headquarters", ""),
                r.get("exit_year", ""),
                r.get("exit_value_usd", ""),
                r.get("acquirer", ""),
                r.get("source_url", ""),
                r.get("source_credibility", ""),
                "Yes" if r.get("verified") else "No",
                r.get("validation_tier", ""),
                issues_str,
                r.get("notes", ""),
            ])
        return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════════
#  CHECKPOINT / RESUME
# ═════════════════════════════════════════════════════════════════════════════

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return {}
        # Backward-compat: older checkpoints lack these fields.
        if "visited_urls" not in saved:
            saved["visited_urls"] = []
        if "cache_manifest" not in saved:
            saved["cache_manifest"] = []
        return saved
    return {}

def save_checkpoint(state: dict, page_cache=None):
    # Normalize visited_urls: callers may pass a set or a list.
    vu = state.get("visited_urls", [])
    if isinstance(vu, set):
        vu = sorted(vu)
    payload = dict(state)
    payload["visited_urls"] = vu
    if page_cache is not None and hasattr(page_cache, "list_keys"):
        payload["cache_manifest"] = sorted(page_cache.list_keys())
    else:
        # Preserve any pre-existing manifest the caller stuffed into state.
        payload["cache_manifest"] = sorted(state.get("cache_manifest", []))
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1 — PLANNING  (one-time, builds the initial search strategy)
# ═════════════════════════════════════════════════════════════════════════════

def plan_research(prompt: str) -> dict:
    """Ask Gemini to decompose the research into search strategies."""

    plan_prompt_body = textwrap.dedent(f"""\
    You are an expert research strategist. The user wants to build a
    COMPREHENSIVE directory of startups.

    USER REQUEST: {prompt}

    Plan a systematic research campaign. Think about ALL the places
    startup data lives:
      - Official university/institution startup lists and annual reports
      - Technology transfer / licensing office databases
      - Accelerator and incubator alumni lists (e.g. eLab, Runway, Y Combinator)
      - Venture capital portfolio pages and databases (Crunchbase, PitchBook)
      - News coverage and press releases
      - LinkedIn company searches
      - Alumni network directories
      - SEC/funding announcement databases
      - Departmental spin-out pages
      - Competition winners (business plan competitions, demo days)
      - Angel investor group portfolios

    Schema (a PlannerResponse object wrapping a list of SearchStrategy):

    ```
    {{
        "strategies": [
            {{
                "name": "Short label for this search angle",
                "description": "What we expect to find",
                "rationale": "Why this angle is promising",
                "priority": "high",
                "queries": [
                    "site:ycombinator.com 'Cornell University' founders",
                    "Cornell Tech Runway alumni companies"
                ]
            }}
        ]
    }}
    ```

    Generate 8-15 strategies with 3-5 queries each.
    Order by priority. Be AMBITIOUS — we want hundreds of startups.
    """)

    plan_prompt = (
        plan_prompt_body
        + "\n\nFORMAT RULES:\n"
        + "- Output exactly one ```json fenced block containing a PlannerResponse object.\n"
        + "- Inside Google search queries, use SINGLE quotes for phrase matching (e.g. site:linkedin.com 'Cornell University' founder).\n"
        + "- Do not include trailing text after the fenced block.\n"
        + f"\n\n{_END_OF_PROMPT_MARKER}\n```json\n"
    )

    raw = call_gemini(plan_prompt, label="Gemini (Planning)")
    fallback = {
        "strategies": [{"name": "General", "description": "Broad search",
                        "rationale": "fallback when planner JSON unparseable",
                        "priority": "high", "queries": [prompt]}]
    }
    result, outcome = _parse_json_typed(raw, PlannerResponse)
    if result is None:
        log.warning("planner returned unparseable JSON (outcome=%s); falling back to default strategy", outcome)
        return fallback
    # Return dict form so existing downstream consumers (which read
    # plan["strategies"][i]["name"], etc.) keep working unchanged.
    return result.model_dump(mode="json")


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 2 — EXTRACTION  (pull structured records from a scraped page)
# ═════════════════════════════════════════════════════════════════════════════

def extract_startups(
    page_text: str,
    page_url: str,
    prompt: str,
    strategy_name: str,
) -> list[dict]:
    """Extract structured startup records from a single page.

    A9: now delegates to the two-pass `extract_from_page` (pass1 discovery +
    optional pass2 enrichment). Result models are dumped to dicts here so
    existing downstream callers (DB upsert, gap report) keep working until
    Task A10 makes them model-aware.

    The `prompt` and `strategy_name` arguments are accepted for backward
    compatibility but no longer threaded into the extraction prompts —
    pass1/pass2 use their own schema-driven prompts.
    """
    records = extract_from_page(page_text, page_url)
    if not records:
        return []

    # Temporary shim (A9 -> A10): convert StartupRecord models to dicts so
    # downstream code that expects dicts (DB upsert, gap report) still works.
    # Attach the same source-metadata fields the old flow added.
    cred = score_source(page_url)
    domain = urlparse(page_url).netloc
    out: list[dict] = []
    for rec in records:
        d = rec.model_dump(mode="json")
        d["source_url"] = page_url
        d["source_credibility"] = cred
        d["source_domain"] = domain
        out.append(d)
    return out


def _extract_startups_chunk(
    content: str,
    page_url: str,
    prompt: str = "",
    strategy_name: str = "",
    chunk_idx: int = 0,
    total_chunks: int = 1,
) -> list[dict]:
    """Backward-compat shim (A9). Forwards to the two-pass `extract_from_page`
    and returns dicts so any lingering callers keep working. The legacy
    single-pass prompt below is unreachable and kept only for reference until
    A10 lands; it will be removed once nothing imports this name."""
    records = extract_from_page(content, page_url)
    if not records:
        return []
    cred = score_source(page_url)
    domain = urlparse(page_url).netloc
    out: list[dict] = []
    for rec in records:
        d = rec.model_dump(mode="json")
        d["source_url"] = page_url
        d["source_credibility"] = cred
        d["source_domain"] = domain
        out.append(d)
    return out


def _extract_startups_chunk_legacy_unreachable(
    content: str,
    page_url: str,
    prompt: str,
    strategy_name: str,
    chunk_idx: int = 0,
    total_chunks: int = 1,
) -> list[dict]:
    """LEGACY single-pass extraction. No longer reachable after A9 wired the
    two-pass flow into `extract_startups`. Retained for reference; delete once
    A10 confirms downstream parity."""

    # B8: Degradation ladder short-circuit. At SCRAPE_ONLY or worse we skip
    # extraction entirely; A9 will add the level-2 (DEMOTED) two-pass path.
    level = _ladder().level if _ladder() else Level.NORMAL
    if level >= Level.SCRAPE_ONLY:
        return []

    chunk_note = (f"\n    (CHUNK {chunk_idx+1} of {total_chunks} from this page)"
                  if total_chunks > 1 else "")

    extract_prompt = textwrap.dedent(f"""\
    You are extracting CORNELL-AFFILIATED COMPANY records from a web page.

    OUTPUT FORMAT (NON-NEGOTIABLE):
    Your ENTIRE response is a single JSON array wrapped in a fenced code
    block tagged json. No tables, no prose, no bullet lists, no second
    fence. If zero qualifying companies are on the page, return an empty
    array inside the fence.

    RESEARCH GOAL: {prompt}
    SEARCH STRATEGY: {strategy_name}
    SOURCE URL: {page_url}{chunk_note}

    PAGE CONTENT:
    {content}

    ════════════════════════════════════════════════════════════
    CRITICAL — AFFILIATION RULE (THE ONLY HARD RULE):
    ════════════════════════════════════════════════════════════
    A company qualifies if-and-only-if AT LEAST ONE FOUNDER (or co-founder)
    is a Cornellian — meaning they attended Cornell as a student, were
    Cornell faculty, or did research at Cornell. This includes ALL Cornell
    schools (Cornell University, Cornell Tech, Weill Cornell Medicine,
    Cornell College of Veterinary Medicine, etc.).

    Company size, age, IPO status, and whether it is "still a startup" DO
    NOT MATTER. Citigroup (Sandy Weill, Cornell '55) qualifies. Coursera
    (Daphne Koller, Cornell faculty) qualifies. A pre-seed two-person
    startup also qualifies.

    A company does NOT qualify if:
      • Only employees (not founders) are Cornellians
      • Only investors / advisors / board members are Cornellians
      • The company licensed Cornell tech but no founder is a Cornellian
        (record it but mark cornellian_founder = "" and affiliation_type = "Licensed Tech")
      • It is a venture fund, accelerator program, university office, or
        student club (these are NOT companies)

    If you cannot identify a specific Cornellian founder by name, OMIT the
    company. The cornellian_founder field is REQUIRED.
    ════════════════════════════════════════════════════════════

    INSTRUCTIONS:
    1. Find every company on this page where at least one founder is a
       Cornellian. Be EXHAUSTIVE — if a page lists 50, extract all 50.
    2. For each company, fill EVERY field in the schema below. Use the
       page's stated info; only use "" or "Unknown" when the page truly
       does not say. Convert dollar amounts to integers ("$12M" → 12000000).
    3. If founder names aren't fully clear, put "Unknown" for `founders` BUT
       you still must fill `cornellian_founder` with the specific Cornellian.
    4. NEVER fabricate. If a field isn't on the page, leave it "" or "Unknown".

    REQUIRED FIELDS PER OBJECT (one object per qualifying company).
    Fill EVERY field. Use "" or "Unknown" only when the page truly does
    not state the answer — never fabricate.

      company_name              string
      cornellian_founder        string (full name of the Cornell-affiliated founder)
      founders                  string (all co-founders, comma-separated)
      description               string (1-2 sentences)
      proof_url                 string (best URL — official site, Wikipedia, etc.)
      website_url               string (the company's OWN website, if linked or named)
      linkedin_url              string (company LinkedIn page, e.g. linkedin.com/company/X)
      affiliation_type          string ("Alumni", "Faculty", "Student", "PhD", "Postdoc", or "Licensed Tech")
      affiliation_evidence      string (one sentence stating the Cornell tie)
      cornellian_school         string (which Cornell college: "Engineering", "Cornell Tech",
                                "Weill Cornell Medicine", "CALS", "Johnson", "Vet", "Hotel",
                                "ILR", "Arts & Sciences", or "Unknown")
      cornellian_grad_year      integer (year founder graduated/affiliated) or "Unknown"
      industry                  string
      status                    string ("Active", "Acquired", "Closed", "Public", or "Unknown")
      founded_year              integer or "Unknown"
      funding_total_usd         integer (USD), or "" if not stated
      funding_stage             string (e.g. "Seed", "Series A", "IPO", "Acquired", "Bootstrapped", "Unknown")
      funding_last_round_year   integer or "Unknown"
      last_round_amount_usd     integer (USD raised in the most recent round), or "" if not stated
      lead_investors            string (1-3 lead investors, comma-separated; "" if not stated)
      valuation_usd             integer (most recent USD valuation), or "" if not stated
      employee_count            string ("12", "1000-5000", or "Unknown")
      is_public                 true, false, or "Unknown"
      headquarters              string ("City, State" or "City, Country" or "Unknown")
      exit_year                 integer (year of acquisition or IPO), or "Unknown"
      exit_value_usd            integer (acquisition price or IPO market cap), or ""
      acquirer                  string (acquiring company's name if status="Acquired"; "" otherwise)

    HARD RULES:
      • Only include companies actually mentioned on the page above.
      • Do NOT invent companies. Do NOT include any object you cannot back up
        with text from the page.
      • Use double-quoted strings, valid JSON booleans (true/false), and
        valid numbers — no Python None, no JS undefined.
      • REMINDER: wrap the entire JSON array in a single ```json … ``` fenced
        code block. No table. No prose. No second fence.

    Begin your fenced JSON output now. Everything that follows the marker
    line below is your response.

    {_END_OF_PROMPT_MARKER}
    """)

    # If the Gemini session can't respond (empty after restart, network blip,
    # session corrupt) we return an empty result and let the caller continue
    # — losing one chunk is dramatically better than crashing the whole run.
    try:
        raw = call_gemini(extract_prompt, label="Gemini (Extract)")
    except RuntimeError as exc:
        log.warning(f"  Extract call gave up: {exc!r}; skipping this chunk.")
        return []
    except Exception as exc:
        log.warning(f"  Extract call raised {type(exc).__name__}: {exc!r}; "
                    f"skipping this chunk.")
        return []
    data = _parse_json(raw, fallback=[])
    if isinstance(data, dict):
        data = data.get("startups", data.get("companies", [data]))
    if not isinstance(data, list):
        return []

    # Attach source metadata
    cred = score_source(page_url)
    domain = urlparse(page_url).netloc
    for rec in data:
        if isinstance(rec, dict) and rec.get("company_name"):
            rec["source_url"] = page_url
            rec["source_credibility"] = cred
            rec["source_domain"] = domain

    return [r for r in data if isinstance(r, dict) and r.get("company_name")]


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3 — GAP-FILLING STRATEGY  (the creative-thinking engine)
# ═════════════════════════════════════════════════════════════════════════════

def generate_gap_filling_strategy(
    prompt: str,
    db: StartupDB,
    queries_used: list[str],
    round_num: int,
    consecutive_dry: int = 0,
) -> dict:
    """
    Look at the current state of the database, identify holes, and generate
    creative search strategies to fill them.
    """

    gap = db.gap_report()
    all_records = db.all_records()

    # Build a statistical summary covering the ENTIRE DB, not just first 100.
    # Gemini can reason about patterns without seeing every record.
    by_industry: dict[str, int] = {}
    by_affiliation: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in all_records:
        ind = r.get("industry") or "Unknown"
        aff = r.get("affiliation_type") or "Unknown"
        sta = r.get("status") or "Unknown"
        by_industry[ind] = by_industry.get(ind, 0) + 1
        by_affiliation[aff] = by_affiliation.get(aff, 0) + 1
        by_status[sta] = by_status.get(sta, 0) + 1

    record_summary = json.dumps({
        "total": len(all_records),
        "by_industry": dict(sorted(by_industry.items(), key=lambda x: -x[1])[:15]),
        "by_affiliation_type": by_affiliation,
        "by_status": by_status,
        "sample_company_names": [r.get("company_name") for r in all_records[:20]],
    }, indent=1)

    # Show companies missing data
    missing_founders_sample = gap["missing_founders"][:15]
    missing_url_sample = gap["missing_proof_url"][:15]

    strategy_prompt = textwrap.dedent(f"""\
    You are a creative research strategist reviewing a startup directory
    that is UNDER CONSTRUCTION. Your job is to figure out what's missing
    and propose SPECIFIC, ACTIONABLE searches to fill the gaps.

    RESEARCH GOAL: {prompt}
    ROUND: {round_num}

    CURRENT DATABASE STATUS:
    {gap['summary']}

    CURRENT RECORDS SUMMARY (of {gap['total']} total):
    {record_summary[:10000]}

    COMPANIES MISSING FOUNDERS (sample of {len(gap['missing_founders'])}):
    {json.dumps(missing_founders_sample)}

    COMPANIES MISSING PROOF URL (sample of {len(gap['missing_proof_url'])}):
    {json.dumps(missing_url_sample)}

    QUERIES ALREADY USED (last 30, do NOT repeat):
    {json.dumps(queries_used[-30:])}

    CONSECUTIVE DRY ROUNDS (no new data): {consecutive_dry}
    {"⚠ We've been finding nothing — you MUST try radically different approaches!" if consecutive_dry >= 2 else ""}

    ═══════════════════════════════════════
    THINK CREATIVELY — consider these angles:
    ═══════════════════════════════════════

    A) TARGETED LOOKUPS for known companies with holes:
       - Search "<company name> founder LinkedIn" to find founders
       - Search "<company name> site:crunchbase.com" to find URLs/data
       - Search "<company name> <university> startup" for verification

    B) DISCOVERY of NEW startups we haven't found yet:
       - Search industry-specific directories
       - Search for recently funded companies
       - Search press releases and demo day coverage
       - Search alumni magazines and newsletters
       - Search for specific departments / labs / programs we haven't covered
       - Search for specific competition winners
       - Search for patents assigned to university-affiliated inventors

    C) CROSS-REFERENCING:
       - Take a known founder name and search for other companies they started
       - Take a known investor and search their full portfolio
       - Look at co-author networks or lab member pages

    D) TEMPORAL COVERAGE:
       - Are we missing startups from specific years?
       - Search for "2020 startups", "2021 startups", etc.

    This system runs PERPETUALLY — there is no "done".  Even if coverage
    looks good, you MUST always generate new actions.  Think about:
      - Re-checking sources that update over time (annual reports, news)
      - Trying completely new query phrasings and angles
      - Searching for very recent startups (current year)
      - Exploring adjacent ecosystems you haven't touched yet
      - Verifying old records that may have changed status

    Return ONLY valid JSON (no markdown fences):
    {{
        "thinking": "Your multi-paragraph reasoning about what's missing and why",
        "actions": [
            {{
                "type": "discovery" or "fill_gaps" or "verify",
                "target": "what we're looking for",
                "queries": ["specific query 1", "specific query 2"],
                "rationale": "why this will help"
            }}
        ]
    }}

    ALWAYS generate 5-15 actions with 2-4 queries each.
    NEVER return an empty actions list.
    Prioritise DISCOVERY of new startups over filling gaps in existing records,
    unless most fields are empty.
    {"⚠ IMPORTANT: " + str(len(gap['missing_founders'])) + " records are missing founders and " + str(len(gap['missing_proof_url'])) + " are missing proof URLs. Dedicate AT LEAST HALF your actions to targeted fill_gaps searches like: \"CompanyName founder CEO\", \"CompanyName site:crunchbase.com\", \"CompanyName site:linkedin.com\". Pick specific companies from the missing lists above." if (len(gap['missing_founders']) > gap['total'] * 0.3 or len(gap['missing_proof_url']) > gap['total'] * 0.3) else ""}
    """)

    raw = call_gemini(strategy_prompt, label="Gemini (Strategy)")
    return _parse_json(raw, fallback={
        "thinking": "Strategy generation failed.",
        "status": "complete",
        "actions": [],
    })


# ═════════════════════════════════════════════════════════════════════════════
#  SEED URL SCRAPING  (prime a run with known directory pages)
# ═════════════════════════════════════════════════════════════════════════════

def scrape_seed_urls(
    driver,
    urls: list[str],
    visited_urls: set,
    prompt: str,
    db: StartupDB,
    page_cache,
) -> tuple[int, int]:
    """
    Scrape a list of seed URLs directly (no Google search) and extract
    records from each. Use this to prime a run with known high-value pages.
    Returns (new_records_count, pages_scraped).
    """
    new_count = 0
    pages_scraped = 0

    for url in urls:
        if url in visited_urls:
            UI.warn(f"Seed URL already visited: {url}")
            continue
        visited_urls.add(url)

        UI.reading(f"[seed] {url}")
        text, status = scrape_page(driver, url, page_cache)

        if status != "ok":
            UI.warn(f"  Could not scrape seed URL ({status}): {url}")
            continue

        records = extract_startups(text, url, prompt, "Seed URL")
        if records:
            for rec in records:
                if db.upsert(rec):
                    new_count += 1
            UI.found(f"  Extracted {len(records)} records "
                     f"({new_count} new so far)")
        else:
            UI.warn("  No relevant records on this seed page")
        pages_scraped += 1
        time.sleep(random.uniform(BETWEEN_PAGES_MIN, BETWEEN_PAGES_MAX))

    return new_count, pages_scraped


# ═════════════════════════════════════════════════════════════════════════════
#  SEARCH EXECUTION  (run a batch of queries, scrape pages, extract records)
# ═════════════════════════════════════════════════════════════════════════════

def execute_searches(
    driver,
    actions: list[dict],
    visited_urls: set,
    prompt: str,
    db: StartupDB,
    page_cache: dict,
) -> tuple[int, int]:
    """
    Execute search actions, scrape results, extract records, upsert into DB.
    Returns (new_records_count, pages_scraped).
    """
    new_count = 0
    pages_scraped = 0
    consecutive_errors = 0

    for action in actions:
        target = action.get("target", action.get("name", "General"))
        queries = action.get("queries", [])
        rationale = action.get("rationale", "")

        if rationale:
            UI.thinking(f"{target}: {rationale}")

        for query in queries:
            UI.search(query)
            urls = google_search(driver, query)

            if not urls:
                UI.warn("No results")
                continue

            UI.found(f"{len(urls)} results")

            for url in urls:
                if url in visited_urls:
                    continue
                visited_urls.add(url)

                UI.reading(url)
                text, status = scrape_page(driver, url, page_cache)

                if status == "ok":
                    records = extract_startups(text, url, prompt, target)
                    if records:
                        for rec in records:
                            is_new = db.upsert(rec)
                            if is_new:
                                new_count += 1
                        UI.found(f"Extracted {len(records)} records "
                                 f"({new_count} new so far)")
                    else:
                        UI.warn("No relevant records on this page")
                    consecutive_errors = 0
                    pages_scraped += 1
                elif status == "empty":
                    consecutive_errors = 0
                else:
                    UI.warn(f"Failed to scrape {urlparse(url).netloc}")
                    consecutive_errors += 1

                if consecutive_errors >= CONSECUTIVE_FAIL_HALT:
                    UI.error("Too many consecutive failures. Stopping round.")
                    return new_count, pages_scraped

                time.sleep(random.uniform(BETWEEN_PAGES_MIN, BETWEEN_PAGES_MAX))

            time.sleep(random.uniform(BETWEEN_SEARCHES_MIN, BETWEEN_SEARCHES_MAX))

    return new_count, pages_scraped


# ═════════════════════════════════════════════════════════════════════════════
#  PARALLEL SEARCH EXECUTION
#  N Selenium workers scrape pages concurrently; Gemini extraction runs serially
#  on the main thread (single browser session, no contention).
# ═════════════════════════════════════════════════════════════════════════════

def execute_searches_parallel(
    actions: list[dict],
    all_visited_urls: set[str],
    prompt: str,
    db: StartupDB,
    page_cache: PageCache,
    headless: bool = False,
    chrome_major: int | None = None,
    num_workers: int = NUM_WORKERS,
) -> tuple[int, int]:
    """
    Parallel variant of execute_searches.
    - NUM_WORKERS Selenium browsers scrape pages concurrently into a queue.
    - Main thread drains the queue, calling Gemini for extraction (serial, safe).
    Returns (new_records_added, pages_scraped).
    """
    if not actions:
        return 0, 0

    n = min(num_workers, len(actions))
    page_queue: Queue = Queue(maxsize=n * 10)   # bounded: back-pressure on fast scrapers
    visited_lock = threading.Lock()
    all_pages_scraped = [0]

    # Distribute actions round-robin across workers
    chunks: list[list[dict]] = [[] for _ in range(n)]
    for i, action in enumerate(actions):
        chunks[i % n].append(action)

    def scrape_worker(worker_id: int, worker_actions: list[dict]) -> None:
        driver = init_driver(headless=headless, chrome_major=chrome_major)
        local_scraped = 0
        try:
            load_cookies(driver)
            for action in worker_actions:
                target = action.get("target", action.get("name", "General"))
                rationale = action.get("rationale", "")
                if rationale:
                    UI.thinking(f"[W{worker_id}] {target}: {rationale}")
                for query in action.get("queries", []):
                    UI.search(f"[W{worker_id}] {query}")
                    urls = google_search(driver, query)
                    if not urls:
                        UI.warn(f"[W{worker_id}] No results")
                        continue
                    UI.found(f"[W{worker_id}] {len(urls)} results")
                    for url in urls:
                        with visited_lock:
                            if url in all_visited_urls:
                                continue
                            all_visited_urls.add(url)
                        UI.reading(url)
                        text, status = scrape_page(driver, url, page_cache)
                        if status == "ok":
                            page_queue.put((text, url, target))
                            local_scraped += 1
                        elif status != "empty":
                            UI.warn(f"[W{worker_id}] Failed: {urlparse(url).netloc}")
                        time.sleep(random.uniform(BETWEEN_PAGES_MIN, BETWEEN_PAGES_MAX))
                    time.sleep(random.uniform(BETWEEN_SEARCHES_MIN, BETWEEN_SEARCHES_MAX))
        except Exception as exc:
            UI.warn(f"Worker {worker_id} crashed: {exc}")
        finally:
            try:
                save_cookies(driver)
                driver.quit()
            except Exception:
                pass
            all_pages_scraped[0] += local_scraped
            page_queue.put(None)   # sentinel: this worker is done

    # Launch workers in background threads
    threads = [
        threading.Thread(target=scrape_worker, args=(i, chunks[i]), daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()

    # Main thread: drain queue and run Gemini extraction (serial)
    new_count = 0
    workers_done = 0
    while workers_done < n:
        try:
            item = page_queue.get(timeout=120)
        except Empty:
            # Timeout — check if all threads are actually dead
            if not any(t.is_alive() for t in threads):
                break
            continue

        if item is None:
            workers_done += 1
            continue

        text, url, target = item
        records = extract_startups(text, url, prompt, target)
        if records:
            for rec in records:
                if db.upsert(rec):
                    new_count += 1
            UI.found(f"Extracted {len(records)} records from {urlparse(url).netloc} "
                     f"(+{new_count} new total)")
        else:
            UI.warn(f"No qualifying records on {urlparse(url).netloc}")

    for t in threads:
        t.join(timeout=10)

    return new_count, all_pages_scraped[0]


# ═════════════════════════════════════════════════════════════════════════════
#  TARGETED GAP-FILLING  (deterministic per-record lookups for missing data)
# ═════════════════════════════════════════════════════════════════════════════

def fill_missing_data(
    driver,
    db: StartupDB,
    visited_urls: set,
    page_cache: dict,
    prompt: str,
    batch_size: int = GAP_FILL_BATCH_SIZE,
) -> int:
    """
    Iterate over records that are missing founders or proof URLs and perform
    targeted, deterministic searches for each one. This bypasses the LLM
    strategy layer — it directly constructs queries from known company names.

    Returns the number of records that were updated (had a gap filled).
    """
    gap = db.gap_report()
    missing_founders = gap.get("missing_founders", [])
    missing_urls = gap.get("missing_proof_url", [])

    # Combine into a deduplicated list, prioritising records missing BOTH
    both_missing = set(missing_founders) & set(missing_urls)
    only_founders = set(missing_founders) - both_missing
    only_urls = set(missing_urls) - both_missing

    # Prioritise: missing-both first, then founders, then URLs
    targets = list(both_missing)[:batch_size]
    remaining = batch_size - len(targets)
    if remaining > 0:
        targets += list(only_founders)[:remaining]
        remaining = batch_size - len(targets)
    if remaining > 0:
        targets += list(only_urls)[:remaining]

    if not targets:
        UI.found("No records need gap-filling — all fields populated!")
        return 0

    UI.phase(f"TARGETED GAP-FILL — {len(targets)} records")
    updated = 0

    for company_name in targets:
        norm = _normalise_name(company_name)
        rec = db.records.get(norm)
        if not rec:
            continue

        needs_founders = (rec.get("founders", "").strip().lower()
                          in ("", "unknown", "n/a"))
        needs_url = not rec.get("proof_url", "").strip() or \
                    rec.get("proof_url", "").strip().lower() in ("unknown", "n/a")

        # Build targeted queries for this specific company
        queries = []
        if needs_founders:
            queries.append(f'"{company_name}" founder CEO')
            queries.append(f'"{company_name}" founded by site:linkedin.com')
            queries.append(f'"{company_name}" site:crunchbase.com')
        if needs_url:
            queries.append(f'"{company_name}" official website')
            if not needs_founders:  # avoid duplicate crunchbase query
                queries.append(f'"{company_name}" site:crunchbase.com')

        UI.action(f"Filling gaps for: {company_name}")
        if needs_founders:
            UI.thinking(f"  Missing: founders")
        if needs_url:
            UI.thinking(f"  Missing: proof URL")

        record_was_updated = False

        for query in queries:
            UI.search(query)
            urls = google_search(driver, query)
            if not urls:
                continue

            for url in urls[:5]:  # limit depth per query
                if url in visited_urls:
                    continue
                visited_urls.add(url)

                UI.reading(url)
                text, status = scrape_page(driver, url, page_cache)
                if status != "ok":
                    continue

                # Ask Gemini to extract ONLY the missing fields for this company
                fill_prompt = textwrap.dedent(f"""\
                I need SPECIFIC information about the company "{company_name}".

                {"I need the FOUNDER NAME(S) — the person(s) who founded this company." if needs_founders else ""}
                {"I need the company's OFFICIAL WEBSITE URL." if needs_url else ""}

                PAGE CONTENT (from {url}):
                {text[:MAX_CONTENT_PER_CALL]}

                Return ONLY valid JSON (no markdown fences):
                {{
                    "company_name": "{company_name}",
                    {"\"founders\": \"comma-separated founder names or empty string if not found\"," if needs_founders else ""}
                    {"\"proof_url\": \"company website URL or empty string if not found\"," if needs_url else ""}
                    "found_useful_info": true or false
                }}
                """)

                try:
                    raw = call_gemini(fill_prompt, label=f"Gemini (Fill: {company_name})")
                    result = _parse_json(raw, fallback={})

                    if not result.get("found_useful_info"):
                        continue

                    # Merge the new data into the existing record
                    new_founders = result.get("founders", "").strip()
                    new_url = result.get("proof_url", "").strip()

                    if needs_founders and new_founders and \
                       new_founders.lower() not in ("unknown", "n/a", ""):
                        rec["founders"] = new_founders
                        needs_founders = False
                        record_was_updated = True
                        UI.found(f"  → Founders: {new_founders}")

                    if needs_url and new_url and \
                       new_url.lower() not in ("unknown", "n/a", "") and \
                       new_url.startswith("http"):
                        rec["proof_url"] = new_url
                        needs_url = False
                        record_was_updated = True
                        UI.found(f"  → Proof URL: {new_url}")

                    # Also update source tracking for verification
                    if new_url or new_founders:
                        all_srcs = set(rec.get("all_sources", []))
                        all_srcs.add(url)
                        rec["all_sources"] = list(all_srcs)
                        if len(all_srcs) >= 2:
                            rec["verified"] = True

                except Exception as e:
                    UI.warn(f"  Fill extraction failed: {e}")

                # If we found everything, stop searching for this company
                if not needs_founders and not needs_url:
                    break

                time.sleep(random.uniform(BETWEEN_PAGES_MIN, BETWEEN_PAGES_MAX))

            # If we found everything, skip remaining queries
            if not needs_founders and not needs_url:
                break

            time.sleep(random.uniform(BETWEEN_SEARCHES_MIN, BETWEEN_SEARCHES_MAX))

        if record_was_updated:
            updated += 1
            # Re-validate so validation_tier reflects current state
            try:
                validate_record(rec)
            except Exception as e:
                log.warning("revalidate after fill failed: %s", e)
            db.records[norm] = rec

    db.save()
    UI.found(f"Gap-fill complete: {updated}/{len(targets)} records updated")
    return updated


# ═════════════════════════════════════════════════════════════════════════════
#  OUTPUT COMPILATION
# ═════════════════════════════════════════════════════════════════════════════

def write_outputs(db: StartupDB, output_dir: str, prompt: str):
    """Write JSON + CSV outputs to output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    # JSON — the full database
    json_path = os.path.join(output_dir, "startups.json")
    db.save()  # saves to db.path

    # Also write a clean JSON array for easy consumption
    clean_json_path = os.path.join(output_dir, "startups_clean.json")
    with open(clean_json_path, "w", encoding="utf-8") as f:
        json.dump(db.all_records(), f, ensure_ascii=False, indent=2)

    # CSV
    csv_path = os.path.join(output_dir, "startups.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(db.to_csv())

    # Gap report
    gap_path = os.path.join(output_dir, "gap_report.json")
    with open(gap_path, "w", encoding="utf-8") as f:
        json.dump(db.gap_report(), f, ensure_ascii=False, indent=2)

    return [clean_json_path, csv_path, gap_path]


# ═════════════════════════════════════════════════════════════════════════════
#  RETROACTIVE VERIFICATION  (validate existing records against affiliation)
# ═════════════════════════════════════════════════════════════════════════════

def _heuristic_verify(db: StartupDB) -> tuple[list[str], list[dict]]:
    """
    First pass: remove records that are obviously non-startups using the
    blocklist and heuristic rules. Returns (removed_names, ambiguous_records).
    """
    to_remove = []
    ambiguous = []

    for key, rec in list(db.records.items()):
        name = rec.get("company_name", "")
        reason = _looks_like_non_startup(rec)
        if reason:
            to_remove.append(name)
            log.info(f"Heuristic removal: {reason}")
        elif _normalise_name(name) in NON_STARTUP_BLOCKLIST:
            to_remove.append(name)
            log.info(f"Blocklist removal: {name}")
        else:
            # Check for signals that this might be a big company, not a startup
            desc = (rec.get("description") or "").lower()
            aff = (rec.get("affiliation_type") or "").lower()

            # Flag records with no affiliation evidence and vague/missing data
            has_evidence = bool(rec.get("affiliation_evidence", "").strip())
            has_founders = (rec.get("founders", "Unknown") not in
                           ("Unknown", "unknown", "", "N/A"))
            has_proof = bool(rec.get("proof_url", "").strip())

            # If we have no affiliation evidence AND no founders AND no proof URL,
            # this is suspect — send to Gemini for review
            if not has_evidence and not has_founders and not has_proof:
                ambiguous.append(rec)

    removed = db.remove_many(to_remove)
    return to_remove, ambiguous


def _gemini_verify_batch(
    records: list[dict],
    prompt: str,
    batch_size: int = 25,
) -> list[str]:
    """
    Second pass: ask Gemini to evaluate a batch of ambiguous records.
    Returns list of company names to REMOVE.
    """
    all_to_remove = []

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        batch_summary = json.dumps(
            [{
                "company_name": r.get("company_name"),
                "founders": r.get("founders", "Unknown"),
                "description": r.get("description", ""),
                "affiliation_type": r.get("affiliation_type", "Unknown"),
                "affiliation_evidence": r.get("affiliation_evidence", ""),
                "source_url": r.get("source_url", ""),
                "proof_url": r.get("proof_url", ""),
            } for r in batch],
            indent=1,
        )

        verify_prompt = textwrap.dedent(f"""\
        You are an AUDITOR reviewing a startup directory for quality.

        RESEARCH GOAL: {prompt}

        The following {len(batch)} records are AMBIGUOUS — they may or may not
        genuinely belong in this directory. Review each one and decide:

        KEEP if:
          - The company was founded by or spun out of the target institution
          - The company licenses technology from the institution
          - The company was created in an institutional incubator/accelerator
          - There is reasonable evidence of genuine affiliation

        REMOVE if:
          - This is a large/established company (Fortune 500, Big Tech, etc.)
            that is NOT a startup, even if an alum works there
          - This is a venture fund, angel group, accelerator PROGRAM, or
            university department — not a startup
          - The "affiliation" is just that an employee once attended the school
          - This looks like a placeholder or generic description, not a real company
          - There is NO credible evidence of genuine institutional affiliation

        RECORDS TO REVIEW:
        {batch_summary}

        OUTPUT FORMAT (NON-NEGOTIABLE):
        Wrap your entire answer in a single ```json fenced code block.
        No prose, no second fence, no Markdown table.

        ```json
        {{
            "decisions": [
                {{
                    "company_name": "Example Corp",
                    "verdict": "keep",
                    "reason": "Brief explanation"
                }}
            ]
        }}
        ```

        Use exactly the field names "company_name", "verdict", "reason".
        verdict must be the literal string "keep" or "remove".
        Be STRICT. When in doubt, REMOVE. We want a high-quality directory.

        ===END_PROMPT===GEMINI_RESPONSE_BELOW===
        """)

        try:
            raw = call_gemini(verify_prompt, label="Gemini (Verify Batch)")
            result = _parse_json(raw, fallback={"decisions": []})
            decisions = result.get("decisions", [])

            for d in decisions:
                if d.get("verdict", "").lower() == "remove":
                    name = d.get("company_name", "")
                    reason = d.get("reason", "no reason given")
                    if name:
                        all_to_remove.append(name)
                        log.info(f"Gemini removal: {name} — {reason}")
                        UI.warn(f"  Remove: {name} — {reason}")
                else:
                    name = d.get("company_name", "")
                    UI.found(f"  Keep: {name}")

        except Exception as e:
            UI.error(f"Gemini verify batch failed: {e}")

    return all_to_remove


def inline_gemini_verify(db: StartupDB, prompt: str) -> int:
    """
    Lightweight quality pass run every INLINE_GEMINI_VERIFY_INTERVAL rounds.
    Samples the most suspicious unverified records (missing founders + URL +
    affiliation evidence) and asks Gemini whether they belong.
    Returns the number of records removed.
    """
    candidates = [
        r for r in db.all_records()
        if not r.get("verified") and not r.get("affiliation_evidence", "").strip()
    ]
    if not candidates:
        return 0

    # Sort by suspicion: most fields missing = highest priority for review
    def suspicion(r: dict) -> int:
        score = 0
        if not (r.get("founders") or "").strip() or r.get("founders") == "Unknown":
            score += 1
        if not (r.get("proof_url") or "").strip():
            score += 1
        if not (r.get("description") or "").strip():
            score += 1
        return score

    candidates.sort(key=suspicion, reverse=True)
    sample = candidates[:INLINE_VERIFY_SAMPLE_SIZE]

    UI.action(f"Inline verify: reviewing {len(sample)} suspicious records …")
    to_remove = _gemini_verify_batch(sample, prompt, batch_size=len(sample))
    if to_remove:
        removed = db.remove_many(to_remove)
        db.save()
        UI.warn(f"Inline verify removed {removed} records")
        return removed
    UI.found("Inline verify: all sampled records passed")
    return 0


def retroactive_verify(
    prompt: str,
    output_dir: str = OUTPUT_DIR,
    use_gemini: bool = True,
    chrome_major: int | None = None,
):
    """
    Run a full retroactive verification pass on the existing database.
    Phase 1: Heuristic blocklist (fast, offline).
    Phase 2: Gemini LLM review of ambiguous records (optional).
    Deletes records that fail verification.
    """
    db_path = os.path.join(output_dir, "startups_db.json")
    db = StartupDB(db_path)

    before_count = db.count()
    UI.banner("RETROACTIVE VERIFICATION")
    print(f"  Database: {before_count} records")

    # ── Phase 1: Heuristic ────────────────────────────────────────────────
    UI.phase("PHASE 1 — HEURISTIC FILTER (blocklist + rules)")
    removed_names, ambiguous = _heuristic_verify(db)

    if removed_names:
        print(f"\n  {UI.RED}Removed {len(removed_names)} records:{UI.RESET}")
        for name in removed_names:
            print(f"    ✗ {name}")
    else:
        print(f"  No records removed by heuristic filter.")

    print(f"  {len(ambiguous)} ambiguous records flagged for Gemini review.")

    # ── Phase 2: Gemini (optional) ────────────────────────────────────────
    gemini_removed = []
    if use_gemini and ambiguous:
        UI.phase("PHASE 2 — GEMINI LLM REVIEW")
        print(f"  Reviewing {len(ambiguous)} ambiguous records …")

        start_gemini(chrome_major=chrome_major)
        gemini_removed = _gemini_verify_batch(ambiguous, prompt)

        if gemini_removed:
            count = db.remove_many(gemini_removed)
            print(f"\n  {UI.RED}Gemini removed {count} records.{UI.RESET}")
        else:
            print(f"  Gemini kept all ambiguous records.")

    # ── Save & report ─────────────────────────────────────────────────────
    db.save()
    after_count = db.count()
    total_removed = before_count - after_count

    write_outputs(db, output_dir, prompt)

    UI.banner("VERIFICATION COMPLETE")
    print(f"  Before:  {before_count} records")
    print(f"  Removed: {total_removed} records")
    print(f"    Heuristic: {len(removed_names)}")
    print(f"    Gemini:    {len(gemini_removed)}")
    print(f"  After:   {after_count} records")
    print()

    # Write a verification log
    log_path = os.path.join(output_dir, "verification_log.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "before_count": before_count,
            "after_count": after_count,
            "heuristic_removed": removed_names,
            "gemini_removed": gemini_removed,
        }, f, ensure_ascii=False, indent=2)

    if use_gemini:
        stop_gemini()

    return total_removed


# ═════════════════════════════════════════════════════════════════════════════
#  VERIFY-ONLY MODE  (no discovery: just plug holes + revalidate every record)
# ═════════════════════════════════════════════════════════════════════════════

def verify_and_fill(
    prompt: str,
    output_dir: str = OUTPUT_DIR,
    headless: bool = True,
    chrome_major: int | None = None,
    max_records: int = 0,
):
    """
    Walk every record, run validate_record(), then for any record that is
    'weak' or 'provisional' (or has missing required fields), do targeted
    Google searches to fill the gaps. Discovers NO new companies.
    """
    db_path = os.path.join(output_dir, "startups_db.json")
    db = StartupDB(db_path)

    if db.count() == 0:
        print("  No records to verify. Run the researcher first.")
        return

    UI.banner("VERIFY & FILL MODE")
    print(f"  {UI.DIM}DB: {db.count()} records{UI.RESET}")

    # ── Pass 1: revalidate every record in place ──────────────────────────
    UI.phase("PHASE 1 — REVALIDATE ALL RECORDS")
    tier_counts = {"high": 0, "provisional": 0, "weak": 0}
    for rec in db.all_records():
        validate_record(rec)
        tier_counts[rec.get("validation_tier", "weak")] = \
            tier_counts.get(rec.get("validation_tier", "weak"), 0) + 1
    db.save()

    print(f"  {UI.GREEN}High:{UI.RESET}        {tier_counts.get('high', 0)}")
    print(f"  {UI.YELLOW}Provisional:{UI.RESET} {tier_counts.get('provisional', 0)}")
    print(f"  {UI.RED}Weak:{UI.RESET}        {tier_counts.get('weak', 0)}")

    # ── Pass 2: gap-fill weak/provisional records ─────────────────────────
    targets = [
        rec for rec in db.all_records()
        if rec.get("validation_tier") in ("weak", "provisional")
    ]
    if max_records and max_records > 0:
        targets = targets[:max_records]

    if not targets:
        UI.found("All records pass at 'high' tier — nothing to fill.")
        return

    UI.phase(f"PHASE 2 — TARGETED FILL ({len(targets)} records)")
    start_gemini(chrome_major=chrome_major)
    try:
        driver = init_driver(headless=headless, chrome_major=chrome_major)
        setup_browser_session(driver, headless)
        page_cache = PageCache(output_dir)
        visited_urls: set[str] = set()
        try:
            updated = fill_missing_data(
                driver, db, visited_urls, page_cache, prompt,
                batch_size=len(targets),
            )
        finally:
            try: driver.quit()
            except Exception: pass

        # ── Pass 3: revalidate again after filling ────────────────────────
        UI.phase("PHASE 3 — REVALIDATE AFTER FILL")
        tier_counts_after = {"high": 0, "provisional": 0, "weak": 0}
        for rec in db.all_records():
            validate_record(rec)
            tier_counts_after[rec.get("validation_tier", "weak")] = \
                tier_counts_after.get(rec.get("validation_tier", "weak"), 0) + 1
        db.save()
        write_outputs(db, output_dir, prompt)

        print(f"  Updated:     {updated} records")
        print(f"  {UI.GREEN}High:{UI.RESET}        {tier_counts.get('high', 0)} → "
              f"{tier_counts_after.get('high', 0)}")
        print(f"  {UI.YELLOW}Provisional:{UI.RESET} {tier_counts.get('provisional', 0)} → "
              f"{tier_counts_after.get('provisional', 0)}")
        print(f"  {UI.RED}Weak:{UI.RESET}        {tier_counts.get('weak', 0)} → "
              f"{tier_counts_after.get('weak', 0)}")
    finally:
        stop_gemini()


# ═════════════════════════════════════════════════════════════════════════════
#  INSPECT MODE  (print gap report without running any searches)
# ═════════════════════════════════════════════════════════════════════════════

def inspect(output_dir: str = OUTPUT_DIR):
    """Load the database, print a gap report, and exit."""
    db_path = os.path.join(output_dir, "startups_db.json")
    db = StartupDB(db_path)
    gap = db.gap_report()

    UI.banner("STARTUP DATABASE INSPECTION")
    print(f"\n  {gap['summary']}\n")

    if gap["total"] == 0:
        print("  No records found. Run the researcher first.")
        return

    complete = gap["complete_count"]
    total = gap["total"]
    pct = complete / total * 100

    print(f"  Completeness: {complete}/{total} records fully filled ({pct:.0f}%)")
    print(f"  Verified (multi-source): "
          f"{total - len(gap['unverified'])}/{total}")

    if gap["missing_founders"]:
        print(f"\n  Missing founders ({len(gap['missing_founders'])}):")
        for name in gap["missing_founders"][:20]:
            print(f"    • {name}")
        if len(gap["missing_founders"]) > 20:
            print(f"    … and {len(gap['missing_founders']) - 20} more")

    if gap["missing_proof_url"]:
        print(f"\n  Missing proof URL ({len(gap['missing_proof_url'])}):")
        for name in gap["missing_proof_url"][:20]:
            print(f"    • {name}")
        if len(gap["missing_proof_url"]) > 20:
            print(f"    … and {len(gap['missing_proof_url']) - 20} more")

    print()


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

def run_scrape_only_pass(url_queue, page_cache, max_urls: int = 20):
    """Level 3 (SCRAPE_ONLY): scrape and cache pages, do not extract.

    Best-effort wiring -- the main round loop does not currently maintain a
    standing url_queue, so this is a no-op when called from the perpetual
    loop. Real wiring will land alongside future queue refactoring.
    """
    import queue as _queue
    if url_queue is None:
        log.info("scrape_only pass: no url_queue available, nothing to do")
        return
    for _ in range(max_urls):
        try:
            url = url_queue.get_nowait()
        except _queue.Empty:
            return
        try:
            text = scrape_page(url)
            if text and page_cache is not None:
                page_cache[url] = text
        except Exception as e:
            log.warning("scrape_only pass: %s -> %s", url, e)


def run_backlog_pass(db, output_dir) -> None:
    """Level 4: zero Gemini, zero Selenium. Local CPU work on the existing DB."""
    log.info("backlog pass starting: %d records", len(db.records))
    updated = 0
    for rec in db.records.values():
        before_tier = rec.get("validation_tier")
        # validate_record mutates the dict in place and updates validation_tier
        validate_record(rec)
        if rec.get("validation_tier") != before_tier:
            updated += 1
    db.save()
    log.info("backlog pass: re-validated %d records, %d tier changes", len(db.records), updated)

    # Recompute gap report
    try:
        report = db.gap_report()
        out_dir = Path(output_dir) if not isinstance(output_dir, Path) else output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gap_report.json").write_text(json.dumps(report, indent=2))
    except Exception as e:
        log.warning("backlog pass: gap_report failed: %s", e)

    # Health report: flag records that look like re-extraction candidates
    try:
        candidates = [r for r in db.records.values()
                      if r.get("validation_tier") == "weak"
                      and r.get("proof_url")]
        out_dir = Path(output_dir) if not isinstance(output_dir, Path) else output_dir
        (out_dir / "health_report.json").write_text(json.dumps({
            "weak_records_with_proof_url": len(candidates),
            "ids": [c.get("company_name", "<unknown>") for c in candidates[:200]],
        }, indent=2))
    except Exception as e:
        log.warning("backlog pass: health_report failed: %s", e)


def run(
    prompt: str = DEFAULT_PROMPT,
    headless: bool = False,
    chrome_major: int | None = None,
    resume: bool = True,
    output_dir: str = OUTPUT_DIR,
    max_rounds: int = MAX_ROUNDS,
    seed_urls: list[str] | None = None,
):
    started_at = datetime.now()
    os.makedirs(output_dir, exist_ok=True)

    # B8: Degradation ladder, observed by call_gemini / scrape_page wrappers.
    ladder = DegradationLadder()
    _LADDER_HOLDER["ladder"] = ladder

    # ── Database ──────────────────────────────────────────────────────────
    db_path = os.path.join(output_dir, "startups_db.json")
    db = StartupDB(db_path)

    # ── Checkpoint ────────────────────────────────────────────────────────
    state = load_checkpoint() if resume else {}

    visited_urls: set[str] = set(state.get("visited_urls", []))
    cache_manifest: set[str] = set(state.get("cache_manifest", []))
    queries_used: list[str] = state.get("queries_used", [])
    round_num: int           = state.get("round", 0)
    plan: dict | None        = state.get("plan")
    page_cache = PageCache(output_dir)  # file-backed; survives restarts
    if cache_manifest:
        on_disk = set(page_cache.list_keys())
        missing = cache_manifest - on_disk
        if missing:
            print(f"  {UI.DIM}Cache manifest: {len(missing)} entries listed in checkpoint are no longer on disk{UI.RESET}")

    # ── Banner ────────────────────────────────────────────────────────────
    UI.banner("STARTUP RESEARCHER")
    print(f"  {UI.DIM}Prompt: {prompt[:100]}{'…' if len(prompt)>100 else ''}{UI.RESET}")
    print(f"  {UI.DIM}DB: {db.count()} records loaded{UI.RESET}")
    print(f"  {UI.DIM}URLs visited: {len(visited_urls)}{UI.RESET}")
    print(f"  {UI.DIM}Page cache: {len(page_cache)} pages on disk{UI.RESET}")
    print(f"  {UI.DIM}Workers: {NUM_WORKERS} parallel Selenium instances{UI.RESET}")
    if round_num > 0:
        print(f"  {UI.DIM}Resuming from round {round_num}{UI.RESET}")

    # ── Start Gemini ──────────────────────────────────────────────────────
    start_gemini(chrome_major=chrome_major)

    try:
        # ══════════════════════════════════════════════════════════════════
        #  PHASE 1 — PLANNING
        # ══════════════════════════════════════════════════════════════════
        if plan is None:
            UI.phase("PHASE 1 — RESEARCH PLANNING")
            UI.thinking("Decomposing the research into systematic search strategies …")
            plan = plan_research(prompt)

            strategies = plan.get("strategies", [])
            print(f"\n  {UI.BOLD}{len(strategies)} search strategies planned:{UI.RESET}")
            for s in strategies:
                p_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                    s.get("priority", "medium"), "⚪")
                print(f"    {p_icon} {s['name']} — {len(s.get('queries', []))} queries")

            state["plan"] = plan
            save_checkpoint(state, page_cache=page_cache)
        else:
            UI.phase("PHASE 1 — PLAN (loaded from checkpoint)")
            for s in plan.get("strategies", []):
                print(f"    • {s['name']}")

        # ── Init browser ──────────────────────────────────────────────────
        UI.phase("INITIALIZING BROWSER")
        driver = init_driver(headless=headless, chrome_major=chrome_major)
        UI.found("Browser ready" + (" (headless)" if headless else " (visible)"))

        # Cookie-aware session setup: load existing cookies or interactive login
        setup_browser_session(driver, headless)

        pages_since_restart = 0

        try:
            # ══════════════════════════════════════════════════════════════
            #  SEED URLS — scrape known directory pages first (if provided)
            # ══════════════════════════════════════════════════════════════
            if seed_urls and round_num == 0:
                UI.phase(f"SEED URLS — {len(seed_urls)} pages")
                seed_new, seed_scraped = scrape_seed_urls(
                    driver, seed_urls, visited_urls, prompt, db, page_cache,
                )
                pages_since_restart += seed_scraped
                UI.found(f"Seed sweep complete: {seed_new} new records "
                         f"(DB total: {db.count()})")
                db.save()

            # ══════════════════════════════════════════════════════════════
            #  ROUND 1 — Initial sweep (execute all planned strategies)
            # ══════════════════════════════════════════════════════════════
            if round_num == 0:
                round_num = 1
                rm = RoundMetrics(round_number=round_num)
                _ROUND_METRICS_HOLDER["rm"] = rm
                UI.phase(f"ROUND {round_num} — INITIAL SWEEP")

                # Convert plan strategies to actions format
                actions = [
                    {
                        "type": "discovery",
                        "target": s["name"],
                        "queries": s.get("queries", []),
                        "rationale": s.get("description", ""),
                    }
                    for s in plan.get("strategies", [])
                ]
                for a in actions:
                    queries_used.extend(a["queries"])

                before = db.count()
                new, scraped = execute_searches_parallel(
                    actions, visited_urls, prompt, db, page_cache,
                    headless=headless, chrome_major=chrome_major,
                )
                pages_since_restart += scraped

                UI.found(f"Round 1 complete: {new} new records "
                         f"(DB total: {db.count()})")
                db.save()

                # Save checkpoint
                state.update({
                    "prompt": prompt, "round": round_num,
                    "visited_urls": list(visited_urls),
                    "queries_used": queries_used,
                    "plan": plan, "complete": False,
                })
                save_checkpoint(state, page_cache=page_cache)

                # Write intermediate outputs
                write_outputs(db, output_dir, prompt)

                # Round metrics summary
                rm.record_db(new_records=new, merged=0, rejected=0)
                print(rm.summary_text())
                import json as _json_for_rm
                _metrics_path = Path(output_dir) / "round_metrics.jsonl"
                _metrics_path.parent.mkdir(parents=True, exist_ok=True)
                with _metrics_path.open("a", encoding="utf-8") as _f:
                    _f.write(_json_for_rm.dumps(rm.to_dict()) + "\n")

            # ══════════════════════════════════════════════════════════════
            #  PERPETUAL LOOP — gap-filling with cooldown
            # ══════════════════════════════════════════════════════════════
            consecutive_dry = 0   # rounds with zero new records
            cooldown_secs = COOLDOWN_BASE_SECS

            while True:
                # Respect max_rounds if set (0 = unlimited)
                if max_rounds > 0 and round_num >= max_rounds:
                    UI.warn(f"Max rounds ({max_rounds}) reached. Stopping.")
                    break

                round_num += 1
                rm = RoundMetrics(round_number=round_num)
                _ROUND_METRICS_HOLDER["rm"] = rm

                # ── B8: Degradation ladder gating ─────────────────────────
                ladder.tick()
                if ladder.level == Level.HARD_STOP:
                    log.error("Degradation ladder reached HARD_STOP. "
                              "Saving state and exiting.")
                    state.update({
                        "prompt": prompt, "round": round_num,
                        "visited_urls": list(visited_urls),
                        "queries_used": queries_used,
                        "plan": plan, "complete": False,
                    })
                    save_checkpoint(state, page_cache=page_cache)
                    break
                if ladder.level == Level.BACKLOG:
                    log.warning("Ladder at BACKLOG level; running local-CPU backlog pass.")
                    try:
                        run_backlog_pass(db, output_dir)
                    except Exception as e:
                        log.exception("backlog pass crashed: %s", e)
                    time.sleep(min(cooldown_secs, COOLDOWN_MAX_SECS))
                    continue
                if ladder.level == Level.SCRAPE_ONLY:
                    log.warning("Ladder at SCRAPE_ONLY; running scrape-only pass.")
                    run_scrape_only_pass(
                        url_queue=None, page_cache=page_cache, max_urls=20,
                    )
                    continue

                # Memory management
                if pages_since_restart > RESTART_EVERY:
                    # Save cookies before quitting
                    try:
                        save_cookies(driver)
                    except Exception:
                        pass
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = init_driver(headless=headless, chrome_major=chrome_major)
                    # Reload cookies into the fresh browser
                    load_cookies(driver)
                    pages_since_restart = 0

                # ── URL expiry: re-allow old URLs for re-checking ─────────
                if round_num % URL_EXPIRY_ROUNDS == 0 and visited_urls:
                    expired_count = len(visited_urls)
                    visited_urls.clear()
                    UI.action(f"URL expiry: cleared {expired_count} visited URLs "
                              f"(allowing re-scrape of updated pages)")
                    # Also trim query history to prevent unbounded growth
                    if len(queries_used) > 500:
                        queries_used[:] = queries_used[-200:]
                        UI.action("Trimmed query history to last 200 entries")

                # ── Show current state ────────────────────────────────────
                gap = db.gap_report()
                UI.phase(f"ROUND {round_num} — GAP ANALYSIS & CREATIVE STRATEGY")
                UI.progress(round_num, gap["total"], gap["complete_count"],
                            len(visited_urls))
                print(f"  {UI.DIM}{gap['summary']}{UI.RESET}")

                # ── Ask Gemini for creative strategy ──────────────────────
                strategy = generate_gap_filling_strategy(
                    prompt, db, queries_used, round_num, consecutive_dry,
                )

                thinking = strategy.get("thinking", "")
                if thinking:
                    print(f"\n  {UI.BOLD}Strategy reasoning:{UI.RESET}")
                    for para in thinking.split("\n"):
                        if para.strip():
                            UI.thinking(para.strip())

                actions = strategy.get("actions", [])

                if not actions:
                    UI.warn("No actions generated. Will retry after cooldown.")
                    consecutive_dry += 1
                else:
                    # Track queries
                    for a in actions:
                        queries_used.extend(a.get("queries", []))

                    # ── Execute the actions ───────────────────────────────
                    UI.phase(f"ROUND {round_num} — EXECUTING {len(actions)} ACTIONS")
                    discovery = [a for a in actions if a.get("type") == "discovery"]
                    fills     = [a for a in actions if a.get("type") == "fill_gaps"]
                    verifies  = [a for a in actions if a.get("type") == "verify"]
                    other     = [a for a in actions if a.get("type") not in
                                 ("discovery", "fill_gaps", "verify")]

                    ordered = discovery + fills + verifies + other

                    new, scraped = execute_searches_parallel(
                        ordered, visited_urls, prompt, db, page_cache,
                        headless=headless, chrome_major=chrome_major,
                    )
                    pages_since_restart += scraped

                    UI.found(f"Round {round_num}: +{new} new records "
                             f"(DB total: {db.count()})")
                    db.save()
                    rm.record_db(new_records=new, merged=0, rejected=0)

                    if new > 0:
                        consecutive_dry = 0
                        cooldown_secs = COOLDOWN_BASE_SECS  # reset cooldown
                    else:
                        consecutive_dry += 1

                # ── Trim queries_used every round (not just on URL expiry) ───
                if len(queries_used) > MAX_QUERIES_HISTORY:
                    queries_used[:] = queries_used[-MAX_QUERIES_HISTORY:]

                # ── Checkpoint ────────────────────────────────────────────
                state.update({
                    "prompt": prompt, "round": round_num,
                    "visited_urls": list(visited_urls),
                    "queries_used": queries_used,
                    "plan": plan, "complete": False,
                })
                save_checkpoint(state, page_cache=page_cache)

                # ── Inline Gemini verify (every N rounds) ─────────────────
                if round_num % INLINE_GEMINI_VERIFY_INTERVAL == 0:
                    UI.phase(f"ROUND {round_num} — INLINE QUALITY VERIFY")
                    removed = inline_gemini_verify(db, prompt)
                    if removed:
                        write_outputs(db, output_dir, prompt)

                # ── Periodic targeted gap-fill (every N rounds) ─────────
                if round_num % GAP_FILL_INTERVAL == 0:
                    gap_before = db.gap_report()
                    missing_before = (len(gap_before.get("missing_founders", []))
                                     + len(gap_before.get("missing_proof_url", [])))
                    if missing_before > 0:
                        UI.phase(f"ROUND {round_num} — TARGETED GAP-FILL PASS")
                        gap_filled = fill_missing_data(
                            driver, db, visited_urls, page_cache, prompt,
                        )
                        if gap_filled > 0:
                            consecutive_dry = 0  # count fills as productive
                            # Re-save cookies periodically (sessions may refresh)
                            try:
                                save_cookies(driver)
                            except Exception:
                                pass

                # ── Periodic heuristic cleanup (every 5 rounds) ───────────
                if round_num % 5 == 0:
                    before_clean = db.count()
                    removed_names, _ = _heuristic_verify(db)
                    if removed_names:
                        db.save()
                        UI.warn(f"Heuristic cleanup: removed "
                                f"{len(removed_names)} non-startup records "
                                f"({before_clean} → {db.count()})")

                # Write outputs every round
                write_outputs(db, output_dir, prompt)

                # ── Round metrics summary ─────────────────────────────────
                try:
                    print(rm.summary_text())
                    import json as _json_for_rm
                    _metrics_path = Path(output_dir) / "round_metrics.jsonl"
                    _metrics_path.parent.mkdir(parents=True, exist_ok=True)
                    with _metrics_path.open("a", encoding="utf-8") as _f:
                        _f.write(_json_for_rm.dumps(rm.to_dict()) + "\n")
                except Exception:
                    log.exception("Failed to write round metrics")

                # ── Cooldown on dry rounds ────────────────────────────────
                if consecutive_dry > 0:
                    sleep_time = min(cooldown_secs, COOLDOWN_MAX_SECS)
                    UI.warn(f"Dry streak: {consecutive_dry} rounds with no new data. "
                            f"Cooling down for {sleep_time/60:.0f} min …")
                    time.sleep(sleep_time)
                    cooldown_secs = min(
                        cooldown_secs * COOLDOWN_BACKOFF, COOLDOWN_MAX_SECS
                    )

        except KeyboardInterrupt:
            UI.warn("Interrupted. Saving progress — re-run with --resume.")
        finally:
            try:
                save_cookies(driver)
            except Exception:
                pass
            try:
                driver.quit()
            except Exception:
                pass

        # ══════════════════════════════════════════════════════════════════
        #  SAVE & REPORT
        # ══════════════════════════════════════════════════════════════════
        state.update({
            "prompt": prompt, "round": round_num,
            "visited_urls": list(visited_urls),
            "queries_used": queries_used,
            "plan": plan, "complete": False,
        })
        save_checkpoint(state, page_cache=page_cache)

        paths = write_outputs(db, output_dir, prompt)
        gap = db.gap_report()

        UI.banner("SESSION ENDED")
        print(f"  Records:       {db.count()}")
        print(f"  Complete:      {gap['complete_count']}")
        print(f"  Verified:      {db.count() - len(gap['unverified'])}")
        print(f"  URLs visited:  {len(visited_urls)}")
        print(f"  Rounds:        {round_num}")
        print(f"  Elapsed:       {datetime.now() - started_at}")
        print(f"\n  Outputs:")
        for p in paths:
            print(f"    → {os.path.abspath(p)}")
        print(f"\n  {UI.DIM}Resume anytime with: python startup_researcher.py --resume{UI.RESET}")
        print(f"{'═' * _TERM_WIDTH}\n")

    finally:
        stop_gemini()


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Structured startup researcher with self-inspection "
                    "and creative gap-filling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python startup_researcher.py
          python startup_researcher.py "Every MIT-affiliated startup"
          python startup_researcher.py --resume
          python startup_researcher.py --inspect
          python startup_researcher.py --verify
          python startup_researcher.py --verify --no-gemini
        """),
    )
    p.add_argument("prompt", nargs="?", default=None,
                   help=f"Research prompt (default: Cornell startups).")
    p.add_argument("--headless", action="store_true", default=False)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    p.add_argument("--chrome-major", type=int, default=None)
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--inspect", action="store_true", default=False,
                   help="Print gap report and exit (no searching).")
    p.add_argument("--verify", action="store_true", default=False,
                   help="Run retroactive verification pass on existing DB. "
                        "Removes non-startups (blocklist + Gemini review).")
    p.add_argument("--verify-only", action="store_true", default=False,
                   help="Skip discovery — just revalidate every record and "
                        "fill gaps via targeted searches. Discovers no new companies.")
    p.add_argument("--no-gemini", action="store_true", default=False,
                   help="With --verify: skip Gemini LLM review, only use "
                        "heuristic blocklist filter.")
    p.add_argument("--seed-urls", type=str, default=None,
                   help="Comma-separated URLs to scrape FIRST (before any "
                        "Google searches). Useful for priming a run with "
                        "known directory pages.")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--max-rounds", type=int, default=0,
                   help="Max rounds (0 = perpetual, default).")
    p.add_argument("--max-records", type=int, default=0,
                   help="With --verify-only: cap number of records to fill (0=all).")
    p.add_argument("--clear-cookies", action="store_true", default=False,
                   help="Delete saved cookies and re-do interactive login.")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.inspect:
        inspect(output_dir=args.output_dir)
        sys.exit(0)

    prompt = args.prompt
    if (args.resume or args.verify) and not prompt:
        ckpt = load_checkpoint()
        prompt = ckpt.get("prompt")
    if not prompt:
        prompt = DEFAULT_PROMPT

    if args.verify:
        retroactive_verify(
            prompt=prompt,
            output_dir=args.output_dir,
            use_gemini=not args.no_gemini,
            chrome_major=args.chrome_major,
        )
        sys.exit(0)

    if args.verify_only:
        verify_and_fill(
            prompt=prompt,
            output_dir=args.output_dir,
            headless=args.headless,
            chrome_major=args.chrome_major,
            max_records=args.max_records,
        )
        sys.exit(0)

    if args.clear_cookies:
        cookie_path = os.path.abspath(COOKIE_FILE)
        if os.path.exists(cookie_path):
            os.remove(cookie_path)
            print(f"  Deleted saved cookies: {cookie_path}")
        else:
            print(f"  No cookie file found at {cookie_path}")

    seed_urls = []
    if args.seed_urls:
        seed_urls = [u.strip() for u in args.seed_urls.split(",") if u.strip()]

    run(
        prompt=prompt,
        headless=args.headless,
        chrome_major=args.chrome_major,
        resume=args.resume,
        output_dir=args.output_dir,
        max_rounds=args.max_rounds,
        seed_urls=seed_urls,
    )
