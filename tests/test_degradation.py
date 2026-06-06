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
