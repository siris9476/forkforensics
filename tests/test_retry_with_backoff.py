"""retry_with_backoff is the shared resilience primitive both the REST and
GraphQL clients build on. It had zero direct test coverage - a bug here
(swallowing the final exception, retrying something non-retriable forever,
an off-by-one in attempt count) would only ever surface as a flaky hang or
crash in production, never in CI."""

import time

import pytest

from forkforensics.errors import retry_with_backoff


def test_succeeds_immediately_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)

    result = retry_with_backoff(lambda: "ok")

    assert result == "ok"
    assert slept == []


def test_a_non_retriable_status_raises_immediately_without_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    calls = []

    def _fn():
        calls.append(1)
        raise ValueError("permanent failure")

    with pytest.raises(ValueError, match="permanent failure"):
        retry_with_backoff(_fn, status_getter=lambda exc: 400)  # 400 not in RETRIABLE_STATUS

    assert len(calls) == 1  # no retry attempted at all
    assert slept == []


def test_a_retriable_status_retries_exactly_the_configured_number_of_times(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def _fn():
        calls.append(1)
        raise ValueError("503")

    with pytest.raises(ValueError, match="503"):
        retry_with_backoff(_fn, retries=3, status_getter=lambda exc: 503)

    assert len(calls) == 4  # the first attempt + 3 retries, no more


def test_succeeds_on_a_later_attempt_after_retriable_failures(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def _fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("503")
        return "recovered"

    result = retry_with_backoff(_fn, retries=5, status_getter=lambda exc: 503)

    assert result == "recovered"
    assert attempts["n"] == 3


def test_reraises_the_last_exception_not_a_generic_one(monkeypatch):
    """A caller inspecting the exception (e.g. checking response.status_code
    on an HTTPError) must get the REAL final exception, not something
    generic that loses that information."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class _Marked(ValueError):
        pass

    def _fn():
        raise _Marked("final")

    with pytest.raises(_Marked):
        retry_with_backoff(_fn, retries=2, status_getter=lambda exc: 503)


def test_no_status_getter_retries_any_exception(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def _fn():
        calls.append(1)
        raise RuntimeError("anything")

    with pytest.raises(RuntimeError):
        retry_with_backoff(_fn, retries=2)

    assert len(calls) == 3  # retried even with no status_getter at all


def test_delay_grows_and_is_bounded_by_backoff_and_jitter(monkeypatch):
    """Not pinning exact values (jitter is random), just the shape: each
    delay must be at least base_delay * 2**attempt, and strictly increasing
    across attempts on average - a flat or shrinking delay would defeat the
    point of backoff."""
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)  # remove jitter for a clean check

    def _fn():
        raise ValueError("503")

    with pytest.raises(ValueError):
        retry_with_backoff(_fn, retries=3, base_delay=1.0, status_getter=lambda exc: 503)

    assert delays == [1.0, 2.0, 4.0]  # base_delay * 2**attempt, attempt = 0,1,2
