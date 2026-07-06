"""Watchdog supervisor for the detached research agent.

WHY THIS EXISTS
---------------
Babysitting the overnight run by waking an LLM every ~30 min to run five
process/log commands and print a table is the worst of both worlds: high cost
per check, low time resolution. Every failure this project hit (three crashes, a
45-min stuck ladder, a 65-min Gemini hang) happened BETWEEN polls and burned
dead wall-clock before anyone noticed. This supervisor inverts that: a cheap
Python process watches CONTINUOUSLY (60s ticks, no browser, just file stats + a
process snapshot) and handles the mechanical 95% itself --

  * clean stop (--max-rounds / SESSION ENDED)  -> rotate log, relaunch
  * real crash (traceback through our own code) -> relaunch under a loop-guard
  * cross-run orphaned Chrome windows            -> sweep (parent-dead roots only)
  * Gemini hang (Still waiting elapsed > thresh) -> flag / escalate
  * total log freeze while alive                 -> flag / escalate

...and escalates to a human ONLY for the genuinely-needs-judgment cases: a novel
crash signature, a crash-loop, a pending CAPTCHA/Cloudflare block. Everything it
sees is written to a compact heartbeat file so a check-in is one Read, not five
commands.

It does NOT relaunch anything through Claude's Bash tool, so the kb-gate never
enters the loop -- relaunch is a plain subprocess.Popen from this process.

IMPORTANT it does NOT fix the within-run driver.quit() Chrome leak (those windows
are parented to the LIVE agent; only the agent knows which of its own drivers are
stale). The orphan sweep here is strictly for leftovers of ALREADY-DEAD runs.

Run detached via launch_supervisor.py. Attaches to the existing run named in
run_detached.pid; it never restarts a healthy process on startup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import launch_detached as L

# ---- config -----------------------------------------------------------------

TICK_S = 60
STALL_FREEZE_S = 15 * 60          # no log bytes at all for this long while alive
GEMINI_HANG_S = 10 * 60           # a single "Still waiting elapsed=N" past this
ORPHAN_SWEEP_EVERY_S = 10 * 60
LOOP_GUARD_WINDOW_S = 15 * 60
LOOP_GUARD_MAX = 3
LOG_TAIL_BYTES = 20_000
# Leaked Chrome (driver.quit() that silently failed) piling up is what OOM-crashed
# the run on 2026-07-06. The source leak is now force-killed at teardown, but keep
# a backstop: warn LOUD if the live run's Chrome count climbs back toward danger,
# so the next occurrence is a caught escalation, not a hard MemoryError.
CHROME_ALERT = 90

HEARTBEAT = L.OUT / "supervisor_status.json"
ESCALATIONS = L.OUT / "supervisor_escalations.jsonl"
SUP_LOG = L.OUT / "supervisor.log"

_CLEAN_MARKERS = ("SESSION ENDED", "Max rounds")
_OUR_CRASH_FRAMES = ("startup_researcher.py", ", in run", ", in <module>")
_ELAPSED_RE = re.compile(r"Still waiting\.\.\. elapsed=(\d+)s")
_DBTOTAL_RE = re.compile(r"DB total:\s*(\d+)")
_CAPTCHA_DETECTED = "CAPTCHA detected"
_CAPTCHA_CLEARED = "CAPTCHA cleared"


# ---- pure decision logic (unit-tested in tests/test_supervisor.py) ----------

def classify_exit(log_tail: str) -> str:
    """Return 'clean', 'crash', or 'unknown' for a stopped process, from the log
    tail. Clean = the agent's own SESSION ENDED / Max rounds banner. Crash = a
    traceback that ran through our own code (not the benign Chrome.__del__ /
    WinError 6 teardown spam, which appears on every exit)."""
    if any(m in log_tail for m in _CLEAN_MARKERS):
        return "clean"
    if "Traceback (most recent call last)" in log_tail:
        if any(f in log_tail for f in _OUR_CRASH_FRAMES):
            return "crash"
    return "unknown"


def should_relaunch(exit_kind, restart_times, now,
                    window_s=LOOP_GUARD_WINDOW_S, max_in_window=LOOP_GUARD_MAX):
    """A clean stop always relaunches (it's just budget exhaustion). A crash /
    unknown exit relaunches only until too many happen inside the window -- past
    that it's a crash-loop, so we stop and escalate instead of thrashing."""
    if exit_kind == "clean":
        return True
    recent = [t for t in restart_times if now - t < window_s]
    return len(recent) < max_in_window


def gemini_hang_seconds(log_tail: str) -> int:
    """Max 'Still waiting... elapsed=Ns' value in the tail, or 0. This is the
    signal for 'alive but wedged on Gemini' -- the log keeps advancing with
    these lines during a hang, so a went-quiet check alone would miss it."""
    vals = [int(m) for m in _ELAPSED_RE.findall(log_tail)]
    return max(vals) if vals else 0


def orphan_chrome_pids(procs) -> set[int]:
    """Given a process snapshot (list of {pid, ppid, name}), return the set of
    chrome.exe PIDs safe to kill: orphaned root windows (a root = parent is not
    chrome; orphaned = that parent PID is absent from the snapshot) plus every
    Chrome descended from them. Live-parented roots and their trees are spared."""
    by_pid = {p["pid"]: p for p in procs}
    live = set(by_pid)
    chrome = [p for p in procs if p["name"] == "chrome.exe"]
    orphan_roots = [
        p for p in chrome
        if by_pid.get(p["ppid"], {}).get("name") != "chrome.exe"
        and p["ppid"] not in live
    ]
    kill: set[int] = set()
    frontier = [p["pid"] for p in orphan_roots]
    kill.update(frontier)
    while frontier:
        nxt = []
        for parent in frontier:
            for c in chrome:
                if c["ppid"] == parent and c["pid"] not in kill:
                    kill.add(c["pid"])
                    nxt.append(c["pid"])
        frontier = nxt
    return kill


def run_chrome_count(procs, root_pid) -> int:
    """Count chrome.exe processes DESCENDED from the watched run (root_pid is an
    ancestor). This is the leak signal that matters -- total chrome.exe is
    polluted by the user's OWN browser (48 of 102 on 2026-07-06 were Matt's, not
    the run's), which made a total-count alarm cry wolf. A modern Chrome spawns
    15-25 processes per instance, so the run legitimately runs dozens; what
    signals a leak is THIS number climbing and staying high over time."""
    by = {p["pid"]: p for p in procs}

    def descends(pid):
        seen = set()
        cur = pid
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur == root_pid:
                return True
            cur = by.get(cur, {}).get("ppid")
        return False

    return sum(1 for p in procs
               if p["name"] == "chrome.exe" and p["pid"] != root_pid
               and descends(p["pid"]))


def chrome_alarm(chrome_n: int, threshold: int = CHROME_ALERT) -> bool:
    """True when the RUN'S OWN Chrome-process count (see run_chrome_count) has
    climbed into leak/OOM-risk territory -- the pileup that crashed the run on
    2026-07-06. Fed the run-scoped count, not total chrome.exe, so the user's
    own browser windows never trip it."""
    return chrome_n >= threshold


def pending_captcha(log_tail: str) -> bool:
    """True if the most recent CAPTCHA event in the tail is a 'detected' with no
    later 'cleared' -- i.e. a block is sitting there waiting for a human."""
    di = log_tail.rfind(_CAPTCHA_DETECTED)
    if di == -1:
        return False
    return log_tail.rfind(_CAPTCHA_CLEARED) < di


def latest_db_total(log_tail: str) -> int | None:
    m = list(_DBTOTAL_RE.finditer(log_tail))
    return int(m[-1].group(1)) if m else None


def read_db_count() -> int | None:
    """Current record count straight from the DB file -- the ground truth,
    more accurate than the log's end-of-round 'DB total' (which lags mid-round
    and scrolls out of the tail window). The agent writes the DB atomically
    (temp + rename), so a concurrent read is safe."""
    try:
        with (L.OUT / "startups_db.json").open("r", encoding="utf-8") as f:
            data = json.load(f)
        recs = data.get("records", data) if isinstance(data, dict) else data
        return len(recs)
    except (OSError, ValueError):
        return None


# ---- I/O glue (thin; not unit-tested) ---------------------------------------

def _now() -> float:
    return time.time()


def sup_log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with SUP_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def read_pid() -> int | None:
    try:
        return int(L.PID.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def read_log_tail(n=LOG_TAIL_BYTES) -> str:
    try:
        with L.LOG.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def snapshot_processes() -> list[dict]:
    """One PowerShell call -> [{pid, ppid, name}]. Used both for liveness and the
    orphan sweep. A failure returns [] (the tick treats that as 'can't tell' and
    skips OS-dependent checks rather than acting on bad data)."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.Name)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                          "name": parts[2].strip()})
        except ValueError:
            continue
    return procs


def kill_pids(pids) -> int:
    killed = 0
    for p in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(p)],
                           capture_output=True, timeout=15)
            killed += 1
        except (subprocess.SubprocessError, OSError):
            pass
    return killed


def escalate(kind: str, detail: str) -> None:
    """Record something a human/LLM should look at. This is the ONLY channel out
    of the mechanical loop -- a check-in reads supervisor_escalations.jsonl."""
    rec = {"ts": int(_now()), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "kind": kind, "detail": detail[:2000]}
    try:
        with ESCALATIONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    sup_log(f"ESCALATE [{kind}] {detail[:200]}")


def write_heartbeat(state: dict) -> None:
    state = {**state, "ts": int(_now()), "iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HEARTBEAT)
    except OSError:
        pass


# ---- the watch loop ---------------------------------------------------------

def supervise() -> None:
    sup_log(f"supervisor up, watching pid-file {L.PID}")
    restart_times: list[float] = []
    last_log_size = -1
    last_advance = _now()
    last_sweep = 0.0
    stall_flagged = False
    hang_flagged = False
    captcha_flagged = False
    chrome_flagged = False

    while True:
        try:
            now = _now()
            procs = snapshot_processes()
            live = {p["pid"] for p in procs}
            pid = read_pid()
            alive = pid is not None and (not procs or pid in live)
            tail = read_log_tail()
            db = read_db_count()
            if db is None:
                db = latest_db_total(tail)

            # --- process gone: classify and (maybe) relaunch ------------------
            if not alive:
                kind = classify_exit(tail)
                if should_relaunch(kind, restart_times, now):
                    if kind != "clean":
                        escalate("crash-relaunch",
                                 f"exit={kind}; relaunching. tail:\n{tail[-1500:]}")
                    else:
                        sup_log("clean stop (max-rounds) -> rotating log + relaunching")
                    new_pid = L.spawn_detached(rotate=True, stamp=int(now))
                    restart_times.append(now)
                    last_log_size = -1
                    last_advance = now
                    stall_flagged = hang_flagged = captcha_flagged = False
                    sup_log(f"relaunched as pid {new_pid} (exit was {kind})")
                else:
                    escalate("crash-loop",
                             f"{LOOP_GUARD_MAX}+ restarts in {LOOP_GUARD_WINDOW_S}s; "
                             f"NOT relaunching. Needs a human. tail:\n{tail[-1500:]}")
                    write_heartbeat({"state": "escalated-crashloop", "pid": pid,
                                     "db_total": db, "restarts": len(restart_times)})
                    time.sleep(TICK_S)
                    continue
                write_heartbeat({"state": "relaunched", "pid": new_pid,
                                 "db_total": db, "restarts": len(restart_times)})
                time.sleep(TICK_S)
                continue

            # --- alive: liveness quality checks -------------------------------
            try:
                size = L.LOG.stat().st_size
            except OSError:
                size = last_log_size
            if size != last_log_size:
                last_log_size = size
                last_advance = now
                stall_flagged = False

            state = "healthy"
            note = ""

            frozen_s = now - last_advance
            if frozen_s >= STALL_FREEZE_S:
                state = "frozen"
                note = f"log unchanged {int(frozen_s)}s while alive"
                if not stall_flagged:
                    escalate("log-freeze", note)
                    stall_flagged = True

            hang = gemini_hang_seconds(tail)
            if hang >= GEMINI_HANG_S:
                state = "gemini-hang"
                note = f"Gemini waiting elapsed={hang}s"
                if not hang_flagged:
                    escalate("gemini-hang",
                             f"{note} -- agent self-recovers ~65min; flag for awareness")
                    hang_flagged = True
            elif hang == 0:
                hang_flagged = False

            if pending_captcha(tail):
                state = "captcha" if state == "healthy" else state
                if not captcha_flagged:
                    escalate("captcha", "pending CAPTCHA/Cloudflare block -- needs Matt")
                    captcha_flagged = True
            else:
                captcha_flagged = False

            # --- periodic cross-run orphan sweep (safe: parent-dead only) -----
            swept = 0
            if procs and now - last_sweep >= ORPHAN_SWEEP_EVERY_S:
                last_sweep = now
                orphans = orphan_chrome_pids(procs)
                if orphans:
                    swept = kill_pids(orphans)
                    sup_log(f"swept {swept} orphaned chrome pids: {sorted(orphans)}")

            chrome_n = sum(1 for p in procs if p["name"] == "chrome.exe")
            run_chrome = run_chrome_count(procs, pid) if procs else 0
            if procs and chrome_alarm(run_chrome):
                if state == "healthy":
                    state = "chrome-high"
                if not chrome_flagged:
                    escalate("chrome-high",
                             f"{run_chrome} chrome.exe descended from the run (of {chrome_n} total) "
                             "-- leak/OOM-risk zone; source leak is force-killed at teardown, "
                             "but this is climbing, investigate for an uncovered driver path")
                    chrome_flagged = True
            elif run_chrome < CHROME_ALERT - 15:
                chrome_flagged = False

            write_heartbeat({
                "state": state, "note": note, "pid": pid, "db_total": db,
                "log_frozen_s": int(frozen_s), "gemini_wait_s": hang,
                "chrome_procs": chrome_n, "run_chrome": run_chrome,
                "restarts": len(restart_times), "last_sweep_killed": swept,
            })
        except Exception as e:  # a watchdog must never die on its own tick
            sup_log(f"tick error (continuing): {type(e).__name__}: {e}")

        time.sleep(TICK_S)


if __name__ == "__main__":
    supervise()
