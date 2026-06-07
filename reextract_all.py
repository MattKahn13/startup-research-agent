# reextract_all.py
"""One-shot re-extraction of every record in startups_db.json against the new schema.

Usage:
    python reextract_all.py [--db startup_output/startups_db.json]
                            [--out startup_output/startups_db_v2.json]
                            [--max N] [--workers 2]

Resume-safe: skips records already present in the output file.

NOTE: scrape_page in startup_researcher.py has signature (driver, url, cache) and
returns (text, status). This script lazily initializes a Selenium driver + PageCache
on first use. Actual execution requires a working Chrome/Selenium environment
(D2/F1 territory). The load-bearing artifact here is the script's structure.

Threading note: Selenium drivers are not thread-safe. With workers>1 we use a
per-thread driver via threading.local. For simplicity and safety, the default
worker count is 2; the original spec allowed higher values but driver creation
is expensive, so prefer low values.
"""
from __future__ import annotations
import argparse
import json
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from schema import StartupRecord
from startup_researcher import (
    scrape_page, _extract_pass1, _extract_pass2,
    _normalise_name, start_gemini, stop_gemini,
)


_thread_local = threading.local()
_driver_lock = threading.Lock()
_gemini_lock = threading.Lock()   # Gemini browser session isn't thread-safe
_shared_cache = None  # PageCache, initialized lazily


def _get_driver():
    """Return a thread-local Selenium driver, creating it on first call."""
    drv = getattr(_thread_local, "driver", None)
    if drv is None:
        from startup_researcher import init_driver
        with _driver_lock:
            drv = init_driver(headless=True)
        _thread_local.driver = drv
    return drv


def _get_cache(out_dir: Path):
    """Return a process-wide PageCache rooted at out_dir."""
    global _shared_cache
    if _shared_cache is None:
        from startup_researcher import PageCache
        _shared_cache = PageCache(out_dir)
    return _shared_cache


def _load_existing(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _failure_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def reextract_one(rec: dict, out_dir: Path) -> tuple[str, str | dict]:
    """Returns (status, payload). status in {ok, fetch_failed, unmatched, schema_failed}."""
    name = rec.get("company_name", "")
    url = rec.get("proof_url") or rec.get("source_url")
    if not url:
        return "fetch_failed", {"company": name, "reason": "no proof_url"}
    try:
        driver = _get_driver()
        cache = _get_cache(out_dir)
        result = scrape_page(driver, url, cache)
        # scrape_page returns (text, status); be defensive
        if isinstance(result, tuple):
            text = result[0]
            fetch_status = result[1] if len(result) > 1 else "ok"
        else:
            text = result
            fetch_status = "ok"
    except Exception as e:
        return "fetch_failed", {"company": name, "url": url, "error": str(e)}
    if not text:
        return "fetch_failed", {"company": name, "url": url, "error": f"empty (status={fetch_status})"}

    # Serialize Gemini browser interactions across worker threads
    with _gemini_lock:
        try:
            pass1 = _extract_pass1(text, url)
        except Exception as e:
            return "schema_failed", {"company": name, "error": f"pass1: {e}"}
        target = _normalise_name(name)
        match = next((r for r in pass1 if _normalise_name(r.company_name) == target), None)
        if match is None:
            return "unmatched", {
                "company": name, "url": url,
                "found": [r.company_name for r in pass1][:10],
            }
        try:
            match = _extract_pass2(match, text)
        except Exception as e:
            return "schema_failed", {"company": name, "error": f"pass2: {e}"}
    return "ok", match.model_dump(mode="json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="startup_output/startups_db.json")
    ap.add_argument("--out", default="startup_output/startups_db_v2.json")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args(argv)

    src = Path(args.db)
    out = Path(args.out)
    if not src.exists():
        print(f"source DB does not exist: {src}", file=sys.stderr)
        return 1
    existing = _load_existing(out)
    src_data = json.loads(src.read_text())
    records = src_data if isinstance(src_data, list) else list(src_data.values())
    if args.max:
        records = records[:args.max]

    fail_dir = out.parent
    todo = [r for r in records if _normalise_name(r.get("company_name", "")) not in existing]
    print(f"backfill: {len(todo)} of {len(records)} records remain to be re-extracted")

    # Start the persistent Gemini browser session ONCE for the whole run.
    # call_gemini() bails with a warning if this isn't done.
    print("starting Gemini session...")
    start_gemini()
    print("Gemini session ready.")

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(reextract_one, r, out.parent): r for r in todo}
        for fut in as_completed(futs):
            try:
                status, payload = fut.result()
            except Exception as e:
                status, payload = "schema_failed", {"error": str(e)}
            rec = futs[fut]
            name = rec.get("company_name", "")
            if status == "ok":
                existing[_normalise_name(name)] = payload
                done += 1
                if done % 25 == 0:
                    _save(out, existing)
                    print(f"  ... {done} re-extracted")
            else:
                _failure_log(fail_dir / f"reextract_{status}.jsonl", payload)
    _save(out, existing)
    print(f"backfill complete: {done} new records in {out}")
    try:
        stop_gemini()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
