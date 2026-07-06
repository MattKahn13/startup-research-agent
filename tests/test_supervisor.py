"""Tests for the supervisor watchdog's pure decision logic.

The supervisor (supervisor.py) is a detached Python process that watches the
research agent's PID + log and handles the mechanical failure modes itself
(clean-stop relaunch, crash relaunch under a loop-guard, cross-run orphan-Chrome
sweep, Gemini-hang detection) so a human/LLM is only pulled in for genuinely
novel or judgment-requiring events. The OS glue (Popen, process snapshot) is thin
and I/O-bound; the DECISIONS below are what must be correct, so they're isolated
into pure functions and tested here with injected inputs.
"""
from supervisor import (
    classify_exit,
    should_relaunch,
    gemini_hang_seconds,
    orphan_chrome_pids,
)


# ---- classify_exit: why did the process stop? -------------------------------

def test_clean_stop_is_recognized():
    """The agent hitting its --max-rounds budget prints a SESSION ENDED banner.
    That is a clean, expected stop -- relaunch freely, do not escalate."""
    tail = (
        "  Round 500: 154 Gemini calls, 153 parsed (99%)...\n"
        "  Max rounds (500) reached. Stopping.\n"
        "SESSION ENDED\n"
        "  Records:       1500\n"
    )
    assert classify_exit(tail) == "clean"


def test_real_crash_through_our_own_code_is_a_crash():
    """An uncaught exception that propagated through startup_researcher.py up to
    <module> is a real crash -- distinct from the benign Chrome.__del__ cleanup
    spam that also appears at interpreter teardown."""
    tail = (
        'Traceback (most recent call last):\n'
        '  File "G:\\...\\startup_researcher.py", line 4208, in <module>\n'
        '    run(\n'
        '  File "G:\\...\\startup_researcher.py", line 3954, in run\n'
        '    thinking = strategy.get("thinking", "")\n'
        "AttributeError: 'list' object has no attribute 'get'\n"
    )
    assert classify_exit(tail) == "crash"


def test_benign_del_cleanup_spam_is_not_a_crash():
    """The WinError 6 / Chrome.__del__ tracebacks fire on EVERY interpreter exit
    (clean or not) as uc tears down driver objects. On their own -- no SESSION
    ENDED, no frame in our code -- they are 'unknown', never 'crash', so they
    don't trigger a crash-escalation by themselves."""
    tail = (
        "Exception ignored in: <function Chrome.__del__ at 0x0000015E>\n"
        "Traceback (most recent call last):\n"
        '  File "C:\\...\\undetected_chromedriver\\__init__.py", line 843, in __del__\n'
        "    self.quit()\n"
        "OSError: [WinError 6] The handle is invalid\n"
    )
    assert classify_exit(tail) == "unknown"


# ---- should_relaunch: the crash-loop guard ----------------------------------

def test_clean_stop_always_relaunches_regardless_of_history():
    """A clean stop is just budget exhaustion; restarting it can never be a
    crash-loop, so the loop-guard does not apply to clean stops."""
    many = [100.0, 200.0, 300.0, 400.0]  # 4 restarts already, within window
    assert should_relaunch("clean", many, now=450.0, window_s=900, max_in_window=3) is True


def test_crash_relaunches_until_the_loop_guard_trips():
    now = 1000.0
    # two recent crash-restarts in the window -> a third is still allowed
    assert should_relaunch("crash", [200.0, 600.0], now, window_s=900, max_in_window=3) is True
    # three recent crash-restarts in the window -> stop, escalate instead
    assert should_relaunch("crash", [200.0, 600.0, 800.0], now, window_s=900, max_in_window=3) is False


def test_crash_loop_guard_only_counts_restarts_inside_the_window():
    """Old restarts age out -- a crash hours after a burst is a fresh incident,
    not a continuation of a loop, so it should be allowed to restart again."""
    now = 100000.0
    old = [10.0, 20.0, 30.0]  # far outside a 900s window
    assert should_relaunch("crash", old, now, window_s=900, max_in_window=3) is True


# ---- gemini_hang_seconds: the "alive but wedged on Gemini" signal ------------

def test_gemini_hang_reads_the_max_elapsed_from_waiting_lines():
    """During a Gemini hang the process stays alive and the log KEEPS advancing
    (it emits 'Still waiting... elapsed=Ns' every ~45s), so a plain
    log-went-quiet check misses it. The elapsed counter is the real signal."""
    tail = (
        "  Waiting for Gemini response...\n"
        "2026-07-05 15:57:00 [INFO]   Still waiting... elapsed=44s, textLen=0\n"
        "2026-07-05 15:57:45 [INFO]   Still waiting... elapsed=91s, textLen=0\n"
        "2026-07-05 15:58:30 [INFO]   Still waiting... elapsed=138s, textLen=0\n"
    )
    assert gemini_hang_seconds(tail) == 138


def test_no_waiting_lines_means_no_hang():
    tail = "  Extracted 3 records from crea.cornell.edu (+40 new total)\n"
    assert gemini_hang_seconds(tail) == 0


# ---- orphan_chrome_pids: which Chrome windows are SAFE to kill ---------------

def _p(pid, ppid, name="chrome.exe"):
    return {"pid": pid, "ppid": ppid, "name": name}


def test_orphan_sweep_kills_parent_dead_root_and_its_descendants_only():
    """A 'root' Chrome is one whose parent is not itself Chrome. An ORPHANED
    root is a root whose launching parent PID no longer exists in the snapshot.
    We kill orphaned roots and every Chrome descended from them -- and NOTHING
    parented (even transitively) to a still-alive launcher."""
    procs = [
        # live research process + its legitimate Chrome tree -- MUST be spared
        _p(500, 1, name="python3.13.exe"),
        _p(600, 500),          # root chrome, parent = live python -> spare
        _p(601, 600),          # child of the live tree -> spare
        # a dead run's orphaned Chrome tree -- MUST be killed
        _p(700, 999),          # root chrome, parent 999 is NOT in snapshot -> orphan
        _p(701, 700),          # descendant of the orphan
        _p(702, 701),          # deeper descendant of the orphan
    ]
    kill = orphan_chrome_pids(procs)
    assert kill == {700, 701, 702}
    assert 600 not in kill and 601 not in kill and 500 not in kill


def test_orphan_sweep_is_empty_when_every_root_has_a_live_parent():
    """The within-run driver.quit() leak produces windows whose parent (the
    research process) is STILL ALIVE -- those are NOT parent-dead orphans and
    must not be swept here (only the agent knows which of its own drivers are
    stale). This sweep is strictly for cross-run leftovers."""
    procs = [
        _p(500, 1, name="python3.13.exe"),
        _p(600, 500),
        _p(610, 500),
        _p(620, 500),
    ]
    assert orphan_chrome_pids(procs) == set()
