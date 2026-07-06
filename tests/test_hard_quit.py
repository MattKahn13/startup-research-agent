"""Tests for the browser-process force-kill on driver teardown.

driver.quit() silently fails on Windows (WinError 6) fairly often and leaves the
Chrome/chromedriver processes alive; over a long run they pile up and OOM-crash
the whole thing (observed 2026-07-06 -- MemoryError while the Gemini session was
restarting after a hang). hard_quit() captures the owned PIDs BEFORE quit() (quit
nulls them on the way down) and force-kills any that survive. The taskkill itself
is OS I/O; what's tested here is the PID-extraction and the capture-before-quit
ordering, since that's what silently breaks if uc renames an attribute.
"""
import gemini_tool


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class _FakeService:
    def __init__(self, pid):
        self.process = _FakeProc(pid)


class _FakeDriver:
    """Mimics the uc.Chrome surface hard_quit reads: .browser_pid and
    .service.process.pid. quit() flips a flag and (like the real thing) clears
    the pid attributes, so a naive post-quit read would find nothing."""
    def __init__(self, browser_pid=1111, service_pid=2222):
        self.browser_pid = browser_pid
        self.service = _FakeService(service_pid)
        self.quit_called = False

    def quit(self):
        self.quit_called = True
        self.browser_pid = None
        self.service = None


def test_browser_pids_collects_browser_and_service_pids():
    d = _FakeDriver(browser_pid=1111, service_pid=2222)
    assert gemini_tool.browser_pids(d) == {1111, 2222}


def test_browser_pids_is_forgiving_of_missing_attributes():
    class Bare:  # neither attribute present
        pass
    assert gemini_tool.browser_pids(Bare()) == set()


def test_hard_quit_force_kills_the_pids_captured_before_quit(monkeypatch):
    """The core guarantee: even though quit() nulls the pid attributes, the
    processes captured beforehand are the ones force-killed -- so a quit() that
    silently left them alive still gets them reaped."""
    killed = {}
    monkeypatch.setattr(gemini_tool, "_force_kill_pids",
                        lambda pids: killed.update({"pids": set(pids)}))
    d = _FakeDriver(browser_pid=1111, service_pid=2222)

    gemini_tool.hard_quit(d)

    assert d.quit_called is True
    assert killed["pids"] == {1111, 2222}, (
        "hard_quit must force-kill the PIDs captured BEFORE quit(); "
        f"got {killed.get('pids')}"
    )


def test_hard_quit_still_kills_when_quit_itself_raises(monkeypatch):
    """The WinError-6 case: quit() throws, but the captured processes must
    still be force-killed (that failure is the whole reason they leak)."""
    killed = {}
    monkeypatch.setattr(gemini_tool, "_force_kill_pids",
                        lambda pids: killed.update({"pids": set(pids)}))

    class Throws(_FakeDriver):
        def quit(self):
            raise OSError("[WinError 6] The handle is invalid")

    gemini_tool.hard_quit(Throws(browser_pid=1111, service_pid=2222))
    assert killed["pids"] == {1111, 2222}
