"""LLM adjudication of the Cornellian-founders dataset: founder vs affiliation.

Marx's review exposed that the pipeline recorded any Cornellian mentioned near a
company as its "founder" -- employees (Amazon/Chandrayee Basu), executives
(Citigroup/Sandy Weill), donors (Atkinson Center), and alumni who merely work
there. The triage showed the real discriminator is the SOURCE, not keywords:
records from curated Cornell-startup directories (bigredai.org/startups,
eship.cornell.edu/cornell-startups, elabstartup.com, tech.cornell.edu) carry thin
evidence ("Name '17") but are trustworthy for founding; records from news pages,
dept pages, Wikipedia, LinkedIn, Instagram, alumni profiles are where affiliation
gets misread as founding.

This adjudicates each record with Gemini, source-aware, into a relationship
verdict; only FOUNDER/CO_FOUNDER survive. Non-destructive: reads the DB, writes
adjudication_results.jsonl (resumable, one line per record) + does NOT modify the
DB. A later apply step builds the cleaned dataset from these verdicts.

Usage:
  python adjudicate_founders.py --smoke     # ~11 known cases, one batch, prints verdicts
  python adjudicate_founders.py             # full run, resumable, writes jsonl
"""
import argparse
import json
import sys
from pathlib import Path

import startup_researcher as sr

OUT = Path("startup_output_overnight")
DB = OUT / "startups_db.json"
TRIAGE = OUT / "triage_report.json"
RESULTS = OUT / "adjudication_results.jsonl"

DIRECTORY_DOMAINS = ("bigredai.org", "eship.cornell.edu/cornell-startups",
                     "elabstartup.com", "tech.cornell.edu")
BATCH = 20

CODE_MAP = {"F": "FOUNDER", "E": "EMPLOYEE", "X": "EXECUTIVE", "I": "INVESTOR",
            "D": "DONOR", "A": "ATTENDEE", "N": "NONCOMPANY", "U": "UNCLEAR"}
KEEP_VERDICTS = {"FOUNDER"}
_TOKEN_RE = None  # set lazily to avoid importing re at module top twice

_PROMPT_HEADER = """You are auditing a dataset that is supposed to contain ONLY companies FOUNDED by a Cornell-affiliated person. It is polluted with people who are merely EMPLOYED by, EXECUTIVES of, DONORS to, INVESTORS in, or ALUMNI who happen to work at a company -- those must be rejected.

For each record decide the Cornellian's relationship to the company, from the evidence and the source:
  FOUNDER      -- founded or co-founded the company
  EMPLOYEE     -- works/worked there in a non-founding role (engineer, analyst, manager, consultant, scientist)
  EXECUTIVE    -- CEO/CTO/chairman/president/board WITHOUT having founded it
  INVESTOR     -- VC/angel/partner at an investment firm
  DONOR        -- funded/endowed/is the namesake (e.g. a named center)
  ATTENDEE     -- merely an alum/student of the institution, no founding
  NONCOMPANY   -- not an external company at all (a university unit, club, publication, government program, foundation, or investment fund)
  UNCLEAR      -- evidence insufficient to tell

SOURCE RULE: these domains are curated directories of Cornell-FOUNDED startups -- a named person from them is a FOUNDER unless the evidence explicitly says otherwise: bigredai.org, eship.cornell.edu/cornell-startups, elabstartup.com, tech.cornell.edu. A news article, department page, Wikipedia article, LinkedIn/Instagram profile, or alumni profile does NOT establish founding by itself.

OUTPUT FORMAT -- CRITICAL: reply with ONE single line, space-separated, one token per record IN ORDER, formatted idx:CODE where CODE is one letter:
F=founder  E=employee  X=executive(non-founder)  I=investor  D=donor  A=attendee-only  N=not-a-company  U=unclear
Example for 3 records: 0:F 1:E 2:N
Output NOTHING else -- no prose, no explanation, no code fence, no newlines. Just the tokens.

RECORDS:
"""


def _domain(url):
    from urllib.parse import urlparse
    return urlparse(url or "").netloc.lower().replace("www.", "")


def _record_line(i, r):
    return json.dumps({
        "idx": i,
        "company": r.get("company_name"),
        "person": r.get("cornellian_founder"),
        "cornell_role": r.get("affiliation_type"),
        "evidence": (r.get("affiliation_evidence") or "")[:220],
        "source": _domain(r.get("proof_url")),
    }, ensure_ascii=False)


def adjudicate_batch(batch):
    """Returns {idx: verdict_string}. Parses compact 'idx:CODE' tokens -- robust
    to the browser-Gemini path truncating long responses (the reason a verbose
    JSON-per-record format failed: a 3s-stable heuristic cut it off mid-stream)."""
    import re
    prompt = _PROMPT_HEADER + "\n".join(_record_line(i, r) for i, r in enumerate(batch))
    raw = sr.call_gemini(prompt, label=f"Adjudicate x{len(batch)}")
    out = {}
    for m in re.finditer(r"(\d+)\s*:\s*([FEXIDANU])\b", raw or ""):
        out[int(m.group(1))] = CODE_MAP[m.group(2)]
    return out


def load_done():
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["company_name"])
            except Exception:
                pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    recs = json.loads(DB.read_text(encoding="utf-8"))["records"]
    triage = {v["company_name"]: v for v in json.loads(TRIAGE.read_text(encoding="utf-8"))["verdicts"]}

    if args.smoke:
        names = ["OpenEvidence", "Hermeus", "Sage", "Varda", "Amazon",
                 "Citigroup", "American Express", "Boston Consulting Group",
                 "Google", "Cisco Systems", "Burger King"]
        by = {(r.get("company_name") or ""): r for r in recs}
        batch = [by[n] for n in names if n in by]
        sr.start_gemini()
        verdicts = adjudicate_batch(batch)
        print(f"\nSMOKE ADJUDICATION ({len(batch)} records):\n")
        for i, r in enumerate(batch):
            verdict = verdicts.get(i, "UNCLEAR")
            keep = "KEEP " if verdict in KEEP_VERDICTS else "drop "
            print(f"  {keep} {r.get('company_name'):26s} {verdict}")
        return

    # full run -- rule-drop the clear rejects, adjudicate the rest, resumable
    done = load_done()
    pending = []
    with RESULTS.open("a", encoding="utf-8") as fh:
        for r in recs:
            nm = r.get("company_name")
            if nm in done:
                continue
            t = triage.get(nm, {})
            if t.get("bucket") in ("reject:cornell-entity", "reject:investment"):
                fh.write(json.dumps({"company_name": nm, "verdict": "NONCOMPANY",
                                     "confidence": "high", "why": t.get("reason"),
                                     "rule_dropped": True}, ensure_ascii=False) + "\n")
                fh.flush()
                continue
            pending.append(r)

        sr.start_gemini()
        total = len(pending)
        print(f"adjudicating {total} records in batches of {BATCH} "
              f"({len(done)} already done)")
        for s in range(0, total, BATCH):
            batch = pending[s:s + BATCH]
            try:
                verdicts = adjudicate_batch(batch)
            except Exception as e:
                sr.log.warning(f"batch {s} failed: {e}; marking UNCLEAR")
                verdicts = {}
            for i, r in enumerate(batch):
                verdict = verdicts.get(i, "UNCLEAR")
                fh.write(json.dumps({"company_name": r.get("company_name"),
                                     "verdict": verdict,
                                     "source": _domain(r.get("proof_url")),
                                     "evidence": (r.get("affiliation_evidence") or "")[:160]},
                                    ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  {min(s+BATCH,total)}/{total} adjudicated")
    print(f"done -> {RESULTS}")


if __name__ == "__main__":
    main()
