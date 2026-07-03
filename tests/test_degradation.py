# tests/test_degradation.py
from collections import deque
from degradation import DegradationLadder, Level
from metrics import CallOutcome


def feed(ladder, outcomes):
    for o in outcomes:
        ladder.observe_gemini(o)


def test_starts_at_level_1():
    ladder = DegradationLadder()
    assert ladder.level == Level.NORMAL


def test_demotes_to_l2_when_parse_rate_drops():
    ladder = DegradationLadder()
    # 13/20 fail -> 35% pass rate, well below 70%
    feed(ladder, [CallOutcome.PARSED]*7 + [CallOutcome.SCHEMA_INVALID]*13)
    assert ladder.level == Level.DEMOTED


def test_demotes_to_l3_on_consecutive_prompt_echoes():
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.PROMPT_ECHOED]*5)
    assert ladder.level == Level.SCRAPE_ONLY


def test_promotes_back_to_l1_after_10_normal_successes():
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.SCHEMA_INVALID]*20)   # drop to L2
    assert ladder.level == Level.DEMOTED
    for _ in range(10):
        ladder.observe_full_prompt_success()
    assert ladder.level == Level.NORMAL


def test_demotes_to_l4_when_selenium_failure_rate_high():
    ladder = DegradationLadder()
    for _ in range(15): ladder.observe_selenium("captcha")
    for _ in range(5):  ladder.observe_selenium("ok")
    assert ladder.level == Level.BACKLOG


def test_hard_stop_after_60min_in_demoted_modes(monkeypatch):
    ladder = DegradationLadder()
    feed(ladder, [CallOutcome.PROMPT_ECHOED]*5)   # -> L3
    ladder._degraded_since = ladder._now() - 61 * 60  # pretend 61 minutes ago
    ladder.tick()
    assert ladder.level == Level.HARD_STOP


def test_backlog_auto_resets_to_normal_instead_of_sitting_idle_for_an_hour():
    """Found live 2026-07-03: a real overnight run tripped straight to BACKLOG
    from a transient Selenium fail-rate burst, then sat there for 45+ minutes
    doing nothing but a no-op local revalidation pass every 5 min, because
    observe_l2_sample_success/observe_full_prompt_success (the methods that
    would normally step the ladder back down) are never called anywhere in
    the codebase -- BACKLOG has no wired-up recovery path except waiting the
    full HARD_STOP_AFTER_S (60 min) to give up entirely. tick() should give
    a degraded ladder a much earlier chance to reset and try real work again
    (a fresh NORMAL round can immediately re-degrade if the problem is still
    real, thanks to the rolling windows -- this is strictly better than being
    provably stuck for up to an hour)."""
    ladder = DegradationLadder()
    for _ in range(15): ladder.observe_selenium("captcha")
    for _ in range(5):  ladder.observe_selenium("ok")
    assert ladder.level == Level.BACKLOG
    ladder._degraded_since = ladder._now() - (ladder.BACKLOG_RETRY_AFTER_S + 1)
    ladder.tick()
    assert ladder.level == Level.NORMAL


def test_repeated_stuck_cycles_eventually_hard_stop_not_reset_forever():
    """The reset above must not become a way to dodge HARD_STOP forever --
    if the ladder keeps re-degrading to BACKLOG right after every reset
    (a genuinely broken environment, not a transient blip), it must still
    give up, per the locked 'the agent stops itself when extraction is
    clearly broken' principle."""
    ladder = DegradationLadder()
    for cycle in range(ladder.MAX_RESETS_BEFORE_HARD_STOP + 1):
        for _ in range(15): ladder.observe_selenium("captcha")
        for _ in range(5):  ladder.observe_selenium("ok")
        assert ladder.level == Level.BACKLOG
        ladder._degraded_since = ladder._now() - (ladder.BACKLOG_RETRY_AFTER_S + 1)
        ladder.tick()
    assert ladder.level == Level.HARD_STOP


def test_sustained_recovery_clears_reset_budget_for_a_later_unrelated_degrade():
    """A later degradation, arriving well after a sustained healthy period,
    should get its own fresh reset budget rather than inheriting an old
    count from an unrelated blip hours earlier -- otherwise a long healthy
    run gets penalized for a transient problem that was already resolved."""
    ladder = DegradationLadder()
    for _ in range(15): ladder.observe_selenium("captcha")
    for _ in range(5):  ladder.observe_selenium("ok")
    assert ladder.level == Level.BACKLOG
    ladder._degraded_since = ladder._now() - (ladder.BACKLOG_RETRY_AFTER_S + 1)
    ladder.tick()
    assert ladder.level == Level.NORMAL
    assert ladder._reset_count == 1

    # Pretend a long, genuinely healthy stretch passed, then a fresh problem
    # trips BACKLOG again. Prime the window directly (rather than via many
    # observe_selenium calls) so the intermediate "still healthy" calls
    # don't keep refreshing _last_normal_at before the real degrade moment.
    ladder._last_normal_at = ladder._now() - (ladder.SUSTAINED_HEALTHY_S + 1)
    ladder._selenium_window.clear()
    ladder._selenium_window.extend(["captcha"] * 19)
    ladder.observe_selenium("captcha")
    assert ladder.level == Level.BACKLOG
    assert ladder._reset_count == 0, "a fresh degrade after sustained health should get a clean reset budget"
