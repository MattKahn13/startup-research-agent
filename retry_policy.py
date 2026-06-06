# retry_policy.py
from __future__ import annotations
import random
import time
from typing import Callable, TypeVar


T = TypeVar("T")


class Retryable(Exception):
    """Transient failure -- safe to retry."""


class Fatal(Exception):
    """Permanent failure -- don't retry."""


def classify_http_status(status: int) -> str:
    if status == 200:
        return "ok"
    if status == 429:
        return "long_backoff"
    if 500 <= status < 600:
        return "retryable"
    if 400 <= status < 500:
        return "fatal"
    return "retryable"


def retry(fn: Callable[[], T],
          attempts: int = 3,
          base_delay: float = 2.0,
          max_delay: float = 60.0) -> T:
    """Bounded retry with exponential backoff and +/-50% jitter.

    Re-raises Fatal immediately. Re-raises the last Retryable after `attempts` tries.
    """
    last_exc: Exception | None = None
    for n in range(attempts):
        try:
            return fn()
        except Fatal:
            raise
        except Retryable as e:
            last_exc = e
            if n == attempts - 1:
                raise
            wait = min(base_delay * (2 ** n), max_delay)
            wait = wait * (0.5 + random.random() * 0.5)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def long_backoff_sleep(attempt: int) -> None:
    """For 429: 60s, then 120s, then 240s with jitter."""
    wait = 60 * (2 ** attempt) * (0.5 + random.random() * 0.5)
    time.sleep(min(wait, 600))
