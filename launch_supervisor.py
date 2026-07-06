"""Spawn supervisor.py as its own fully-detached background process.

Same Windows DETACHED_PROCESS pattern as launch_detached.py, so the watchdog
survives this launcher (and the Claude session) exiting. The supervisor attaches
to whatever run_detached.pid points at; it does NOT restart a healthy run on
startup. Writes: <out>/supervisor.pid, <out>/supervisor.boot.log (the supervisor
keeps its own supervisor.log + supervisor_status.json once running).

Run: python launch_supervisor.py
"""
import os
import subprocess
import sys
from pathlib import Path

import launch_detached as L

BOOT_LOG = L.OUT / "supervisor.boot.log"
SUP_PID = L.OUT / "supervisor.pid"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

env = dict(os.environ)
env["PYTHONUTF8"] = "1"

L.OUT.mkdir(parents=True, exist_ok=True)
log = open(BOOT_LOG, "a", encoding="utf-8", errors="replace")
proc = subprocess.Popen(
    [sys.executable, "-u", "supervisor.py"],
    cwd=str(L.PROJ),
    env=env,
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    close_fds=True,
    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
)
SUP_PID.write_text(str(proc.pid), encoding="ascii")
print(f"SUPERVISOR_PID={proc.pid}")
