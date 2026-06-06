import pytest
from retry_policy import retry, Retryable, Fatal, classify_http_status


def test_retries_on_retryable_then_succeeds():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Retryable("transient")
        return "ok"
    assert retry(flaky, attempts=3, base_delay=0.001) == "ok"
    assert calls["n"] == 3


def test_does_not_retry_on_fatal():
    def explode():
        raise Fatal("4xx")
    with pytest.raises(Fatal):
        retry(explode, attempts=3, base_delay=0.001)


def test_gives_up_after_max_attempts():
    def always():
        raise Retryable("never")
    with pytest.raises(Retryable):
        retry(always, attempts=2, base_delay=0.001)


def test_classify_http():
    assert classify_http_status(500) == "retryable"
    assert classify_http_status(503) == "retryable"
    assert classify_http_status(429) == "long_backoff"
    assert classify_http_status(403) == "fatal"
    assert classify_http_status(404) == "fatal"
    assert classify_http_status(200) == "ok"
