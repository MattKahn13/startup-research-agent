"""Tests for VisitedLog -- crash-safe visited-URL persistence.

The checkpoint saved `visited_urls` only at round-end, but rounds routinely
don't complete before the machine sleeps (~hourly during the 2026-07-06 run),
so every resume loaded an EMPTY visited set and the agent re-did all discovery
even with --resume on. VisitedLog is an append-only, flush-per-add log so every
visited URL survives a mid-round crash/sleep. It must be a drop-in for the plain
`set` the run loop uses: `in`, add, len, iter, clear.
"""
from startup_researcher import VisitedLog


def test_persists_across_reopen(tmp_path):
    """Every add is flushed, so a fresh VisitedLog on the same path reloads the
    full set -- this is the whole point (mid-round death must not lose URLs)."""
    p = tmp_path / "visited.log"
    v = VisitedLog(p)
    v.add("https://a.com")
    v.add("https://b.com")
    # simulate a crash + relaunch: brand-new instance, same file
    v2 = VisitedLog(p)
    assert "https://a.com" in v2
    assert "https://b.com" in v2
    assert len(v2) == 2


def test_dedups_in_memory_and_on_disk(tmp_path):
    p = tmp_path / "visited.log"
    v = VisitedLog(p)
    v.add("https://a.com")
    v.add("https://a.com")
    v.add("https://a.com")
    assert len(v) == 1
    # reload proves the file wasn't triple-written
    assert len(VisitedLog(p)) == 1


def test_supports_the_set_interface_the_run_loop_uses(tmp_path):
    p = tmp_path / "visited.log"
    v = VisitedLog(p)
    for u in ["u1", "u2", "u3"]:
        v.add(u)
    assert "u2" in v           # __contains__ (the skip-if-visited check)
    assert "nope" not in v
    assert len(v) == 3         # __len__ (banner + expiry count)
    assert set(iter(v)) == {"u1", "u2", "u3"}   # __iter__ (list() for checkpoint)
    assert set(list(v)) == {"u1", "u2", "u3"}


def test_clear_empties_and_truncates_so_resume_does_not_reload_cleared_urls(tmp_path):
    """The run clears visited_urls every URL_EXPIRY_ROUNDS to re-check stale
    pages. clear() must also truncate the log, else the next resume would
    reload exactly the URLs the expiry just intentionally forgot."""
    p = tmp_path / "visited.log"
    v = VisitedLog(p)
    v.add("https://a.com")
    v.add("https://b.com")
    v.clear()
    assert len(v) == 0
    assert "https://a.com" not in v
    # a post-clear add still persists, and reload sees ONLY the post-clear url
    v.add("https://c.com")
    v2 = VisitedLog(p)
    assert set(iter(v2)) == {"https://c.com"}


def test_empty_and_blank_lines_ignored_on_reload(tmp_path):
    p = tmp_path / "visited.log"
    p.write_text("https://a.com\n\n   \nhttps://b.com\n", encoding="utf-8")
    v = VisitedLog(p)
    assert len(v) == 2
    assert "https://a.com" in v and "https://b.com" in v
