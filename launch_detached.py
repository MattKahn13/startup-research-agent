"""Spawn the research agent as a fully DETACHED background process on Windows.

subprocess.Popen with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
starts a child that is NOT tied to this launcher's console or process group, so
it survives the parent (and the Claude session) exiting. Python passes argv as a
real list -- no shell quoting hell for the seed URLs or the prompt.

Requires UNATTENDED=1 so the agent never blocks on the Enter prompt (cookies
must already be in browser_cookies.json).

Run: python launch_detached.py
Writes: <out>/run_detached.log, <out>/run_detached.pid
"""
import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(r"G:\My Drive\Cornell\Spring 2026\Agents\startup_research_agent")
OUT = PROJ / "startup_output_overnight"
OUT.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env["UNATTENDED"] = "1"
env["PYTHONUTF8"] = "1"

cmd = [
    sys.executable,
    "-u",  # unbuffered stdout so run_detached.log shows live progress
    "startup_researcher.py",
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

log = open(OUT / "run_detached.log", "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen(
    cmd,
    cwd=str(PROJ),
    env=env,
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    close_fds=True,
    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
)
(OUT / "run_detached.pid").write_text(str(proc.pid), encoding="ascii")
print(f"DETACHED_PID={proc.pid}")
