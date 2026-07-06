"""Spawn the research agent as a fully DETACHED background process on Windows.

subprocess.Popen with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
starts a child that is NOT tied to this launcher's console or process group, so
it survives the parent (and the Claude session) exiting. Python passes argv as a
real list -- no shell quoting hell for the seed URLs or the prompt.

Requires UNATTENDED=1 so the agent never blocks on the Enter prompt (cookies
must already be in browser_cookies.json).

Run: python launch_detached.py        (spawns, prints DETACHED_PID=<pid>)
Importable: `from launch_detached import spawn_detached, PROJ, OUT, LOG, PID`
-- supervisor.py reuses spawn_detached() so there is ONE definition of how the
agent is launched (no argv drift between the manual launcher and the watchdog).
Writes: <out>/run_detached.log, <out>/run_detached.pid
"""
import os
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path(r"G:\My Drive\Cornell\Spring 2026\Agents\startup_research_agent")
OUT = PROJ / "startup_output_overnight"
LOG = OUT / "run_detached.log"
PID = OUT / "run_detached.pid"

# The single canonical launch command. Anything that (re)starts the agent -- the
# manual CLI entrypoint below, or supervisor.py's auto-relaunch -- goes through
# spawn_detached() so this list is the ONLY place the argv lives.
RESEARCH_ARGV = [
    sys.executable,
    "-u",  # unbuffered stdout so run_detached.log shows live progress
    "startup_researcher.py",
    "--resume",  # CRITICAL: reload visited_urls / queries_used / plan / round from
                 # startup_checkpoint.json so a crash/sleep relaunch CONTINUES instead
                 # of re-planning + re-extracting already-visited pages. Without it,
                 # every restart re-did discovery from scratch (the DB dedups records
                 # and the page cache blocks re-downloads, but Gemini re-extraction was
                 # wasted). This is one continuous task, so always-resume is correct.
    "--max-rounds", "500",
    "--output-dir", "startup_output_overnight",
    "--seed-urls",
    "https://eship.cornell.edu/cornell-startups/high-profile-startups/,"
    "https://bigredai.org/startups",
    "Find every company where at least one founder is a Cornellian. "
    "Prioritize source pages that state the founder name and Cornell "
    "affiliation in the same passage.",
]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
_CREATION_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _rotate_log(stamp: int | None = None) -> Path | None:
    """Preserve the current log before a relaunch truncates it. Returns the
    rotated path, or None if there was no log to rotate. (Every relaunch this
    project has done manually ended up hand-copying the log to a .crash-* /
    .stuck-* sibling; this automates exactly that so continuity survives.)"""
    if not LOG.exists() or LOG.stat().st_size == 0:
        return None
    stamp = stamp if stamp is not None else int(time.time())
    rotated = LOG.with_suffix(LOG.suffix + f".{stamp}")
    try:
        LOG.replace(rotated)
        return rotated
    except OSError:
        return None


def spawn_detached(rotate: bool = False, stamp: int | None = None) -> int:
    """Spawn the research agent fully detached. Returns the child PID and writes
    it to <out>/run_detached.pid. If rotate=True, the existing log is moved
    aside first (for relaunches) rather than clobbered."""
    OUT.mkdir(parents=True, exist_ok=True)
    if rotate:
        _rotate_log(stamp)

    env = dict(os.environ)
    env["UNATTENDED"] = "1"
    env["PYTHONUTF8"] = "1"

    log = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        RESEARCH_ARGV,
        cwd=str(PROJ),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_CREATION_FLAGS,
    )
    PID.write_text(str(proc.pid), encoding="ascii")
    return proc.pid


if __name__ == "__main__":
    pid = spawn_detached()
    print(f"DETACHED_PID={pid}")
