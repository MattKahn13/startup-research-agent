# degradation.py
from __future__ import annotations
import enum
import time
from collections import deque
from typing import Deque
from metrics import CallOutcome


class Level(enum.IntEnum):
    NORMAL = 1
    DEMOTED = 2          # smaller chunks, minimal schema
    SCRAPE_ONLY = 3      # no gemini
    BACKLOG = 4          # no gemini, no selenium
    HARD_STOP = 5


class DegradationLadder:
    WINDOW = 20
    L1_TO_L2_PARSE_RATE = 0.70
    L2_TO_L3_PARSE_RATE = 0.50
    L3_FROM_PROMPT_ECHO = 5
    L4_SELENIUM_FAIL_RATE = 0.50
    L4_SELENIUM_WINDOW = 20
    L1_PROMOTE_AFTER = 10
    L2_PROMOTE_AFTER = 2
    HARD_STOP_AFTER_S = 60 * 60
    # observe_l2_sample_success/observe_full_prompt_success (the intended
    # step-back-down promotions) are not currently called anywhere in the
    # codebase, so a degraded level otherwise has NO wired-up recovery path
    # short of the full HARD_STOP_AFTER_S. Confirmed live 2026-07-03: a
    # transient Selenium fail-rate burst tripped straight to BACKLOG and the
    # run sat idle (a no-op local revalidation pass every 5 min) for 45+
    # minutes with zero chance of resuming real work. Give it a much earlier
    # chance to reset and retry; a genuinely broken environment simply
    # re-degrades immediately and burns through the reset budget below.
    BACKLOG_RETRY_AFTER_S = 15 * 60
    MAX_RESETS_BEFORE_HARD_STOP = 3
    # How long the ladder must stay healthy at NORMAL before a later
    # degradation is treated as a fresh problem (fresh reset budget) rather
    # than a continuation of the same one.
    SUSTAINED_HEALTHY_S = 15 * 60

    def __init__(self):
        self.level = Level.NORMAL
        self._gemini_window: Deque[CallOutcome] = deque(maxlen=self.WINDOW)
        self._selenium_window: Deque[str] = deque(maxlen=self.L4_SELENIUM_WINDOW)
        self._full_prompt_streak = 0
        self._l2_sample_streak = 0
        self._l3_fetch_streak = 0
        self._degraded_since: float | None = None
        self._reset_count = 0
        self._last_normal_at: float = self._now()

    @staticmethod
    def _now() -> float:
        return time.time()

    def _maybe_mark_degraded(self):
        if self.level > Level.NORMAL and self._degraded_since is None:
            self._degraded_since = self._now()
            if self._now() - self._last_normal_at > self.SUSTAINED_HEALTHY_S:
                self._reset_count = 0
        elif self.level == Level.NORMAL:
            self._degraded_since = None
            self._last_normal_at = self._now()

    def observe_gemini(self, outcome: CallOutcome) -> None:
        self._gemini_window.append(outcome)
        if outcome == CallOutcome.PROMPT_ECHOED:
            recent_echoes = sum(1 for o in list(self._gemini_window)[-self.L3_FROM_PROMPT_ECHO:]
                                if o == CallOutcome.PROMPT_ECHOED)
            if recent_echoes >= self.L3_FROM_PROMPT_ECHO and self.level < Level.SCRAPE_ONLY:
                self.level = Level.SCRAPE_ONLY
                self._maybe_mark_degraded()
                return
        if len(self._gemini_window) >= self.WINDOW:
            parsed = sum(1 for o in self._gemini_window if o == CallOutcome.PARSED)
            rate = parsed / len(self._gemini_window)
            if rate < self.L1_TO_L2_PARSE_RATE and self.level < Level.DEMOTED:
                self.level = Level.DEMOTED
        self._maybe_mark_degraded()

    def observe_selenium(self, outcome: str) -> None:
        self._selenium_window.append(outcome)
        if outcome == "ok" and self.level == Level.SCRAPE_ONLY:
            self._l3_fetch_streak += 1
        else:
            self._l3_fetch_streak = 0 if outcome != "ok" else self._l3_fetch_streak
        if len(self._selenium_window) >= self.L4_SELENIUM_WINDOW:
            fails = sum(1 for o in self._selenium_window if o != "ok")
            rate = fails / len(self._selenium_window)
            if rate > self.L4_SELENIUM_FAIL_RATE and self.level < Level.BACKLOG:
                self.level = Level.BACKLOG
        self._maybe_mark_degraded()

    def observe_full_prompt_success(self) -> None:
        self._full_prompt_streak += 1
        if self.level == Level.DEMOTED and self._full_prompt_streak >= self.L1_PROMOTE_AFTER:
            self.level = Level.NORMAL
            self._full_prompt_streak = 0
            self._maybe_mark_degraded()

    def observe_l2_sample_success(self) -> None:
        self._l2_sample_streak += 1
        if self.level == Level.SCRAPE_ONLY and self._l2_sample_streak >= self.L2_PROMOTE_AFTER:
            self.level = Level.DEMOTED
            self._l2_sample_streak = 0
            self._maybe_mark_degraded()

    def tick(self) -> None:
        """Called once per round; promotes hard stop if degraded too long, or
        resets back to NORMAL well before that so the ladder gets a real
        chance to test whether conditions have actually recovered (see
        BACKLOG_RETRY_AFTER_S docstring above)."""
        if self.level > Level.NORMAL and self._degraded_since is not None:
            degraded_for = self._now() - self._degraded_since
            if degraded_for > self.HARD_STOP_AFTER_S or self._reset_count >= self.MAX_RESETS_BEFORE_HARD_STOP:
                self.level = Level.HARD_STOP
                return
            if degraded_for > self.BACKLOG_RETRY_AFTER_S:
                self.level = Level.NORMAL
                self._degraded_since = None
                self._last_normal_at = self._now()
                self._reset_count += 1
                self._gemini_window.clear()
                self._selenium_window.clear()
