"""Recover real founders wrongly parked in UNCLEAR by re-reading the CACHED SOURCE
PAGE (not the thin snippet). Reuses the cached pages already on disk, so no
re-download. Resumable: appends to unclear_recovery.jsonl.
Run: python recover_unclear.py"""
import json
from pathlib import Path
import startup_researcher as sr

OUT = Path("startup_output_overnight")
RESULTS = OUT / "adjudication_results.jsonl"
RECOVERY = OUT / "unclear_recovery.jsonl"
DB = OUT / "startups_db.json"


def build_recovery_prompt(company, person, page_text):
    return (f"From the SOURCE PAGE below, did {person} FOUND (or co-found) {company}? "
            f"Answer exactly one token: FOUNDER if the page says they founded/co-founded it, "
            f"or NOT if the page shows any other relationship (employee, executive, donor, "
            f"attendee) or does not establish founding.\n\n"
            f"COMPANY: {company}\nPERSON: {person}\n\nSOURCE PAGE (truncated):\n{page_text[:6000]}")


def _unclear_records():
    verdicts = {json.loads(l)["company_name"]: json.loads(l)
                for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()}
    recs = json.loads(DB.read_text(encoding="utf-8"))["records"]
    return [r for r in recs if verdicts.get(r.get("company_name"), {}).get("verdict") == "UNCLEAR"]


def main():
    done = set()
    if RECOVERY.exists():
        done = {json.loads(l)["company_name"] for l in RECOVERY.read_text(encoding="utf-8").splitlines() if l.strip()}
    cache = sr.PageCache(str(OUT))
    todo = [r for r in _unclear_records() if r.get("company_name") not in done]
    sr.start_gemini()
    with RECOVERY.open("a", encoding="utf-8") as fh:
        for r in todo:
            page = cache.get(r.get("proof_url") or "") or (r.get("affiliation_evidence") or "")
            prompt = build_recovery_prompt(r.get("company_name"), r.get("cornellian_founder"), page)
            raw = sr.call_gemini(prompt, label="Recover")
            verdict = "FOUNDER" if "FOUNDER" in (raw or "").upper()[:20] else "NOT"
            fh.write(json.dumps({"company_name": r.get("company_name"), "verdict": verdict},
                                ensure_ascii=False) + "\n")
            fh.flush()
    print(f"recovered over {len(todo)} UNCLEAR records -> {RECOVERY}")


if __name__ == "__main__":
    main()
