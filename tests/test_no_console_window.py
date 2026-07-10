"""Regression guard: when the agent/supervisor run detached (no console of their
own), every child console program (powershell, taskkill, reg) MUST be spawned with
CREATE_NO_WINDOW, or Windows pops a fresh console window for each call -- the
supervisor's per-tick powershell snapshot would flash one every 60 seconds.

These tests assert the creation flag is passed. They pass on any OS (the flag is
0 off-Windows, and CREATE_NO_WINDOW on Windows).
"""
import subprocess
import gemini_tool
import supervisor


def _capture(monkeypatch, module, attr="run"):
    seen = {}

    class _Res:
        stdout = ""
        def decode(self, *a, **k):
            return ""

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return _Res()

    monkeypatch.setattr(getattr(module, "subprocess"), attr, fake)
    return seen


def test_supervisor_snapshot_uses_no_window(monkeypatch):
    seen = _capture(monkeypatch, supervisor, "run")
    supervisor.snapshot_processes()
    assert seen.get("creationflags") == supervisor._NO_WINDOW


def test_supervisor_kill_uses_no_window(monkeypatch):
    seen = _capture(monkeypatch, supervisor, "run")
    supervisor.kill_pids([4242])
    assert seen.get("creationflags") == supervisor._NO_WINDOW


def test_gemini_force_kill_uses_no_window(monkeypatch):
    seen = _capture(monkeypatch, gemini_tool, "run")
    gemini_tool._force_kill_pids([4242])
    assert seen.get("creationflags") == gemini_tool._NO_WINDOW


def test_gemini_chrome_detect_uses_no_window(monkeypatch):
    seen = _capture(monkeypatch, gemini_tool, "check_output")
    gemini_tool._detect_chrome_major()
    assert seen.get("creationflags") == gemini_tool._NO_WINDOW


def test_no_window_is_create_no_window_constant():
    # 0 off-Windows; the real CREATE_NO_WINDOW (0x08000000) on Windows.
    assert gemini_tool._NO_WINDOW == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert supervisor._NO_WINDOW == getattr(subprocess, "CREATE_NO_WINDOW", 0)
