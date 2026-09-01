"""GitHubRateLimiter had no test file at all, yet a bug here either hangs
the app on a sleep or hammers GitHub during a penalty window."""

import time
from unittest.mock import MagicMock

from forkforensics.github_graphql import GitHubGraphQLClient
from forkforensics.github_rest import GitHubClient
from forkforensics.rate_limiter import GitHubRateLimiter


def _resp(headers):
    r = MagicMock()
    r.headers = headers
    return r


def test_after_response_reads_the_headers():
    lim = GitHubRateLimiter()
    lim.after_response(_resp({"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1700000000"}))
    assert lim.remaining == 42
    assert lim.reset_at == 1700000000


def test_after_response_keeps_previous_values_when_headers_are_absent():
    """Not every response carries the rate-limit headers; a missing header
    must not wipe what we already knew."""
    lim = GitHubRateLimiter()
    lim.after_response(_resp({"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": "999"}))
    lim.after_response(_resp({}))
    assert lim.remaining == 10
    assert lim.reset_at == 999


def test_after_response_survives_malformed_headers():
    """A non-numeric header used to raise ValueError from inside the retry
    handler, converting a recoverable rate limit into a crash."""
    lim = GitHubRateLimiter()
    lim.after_response(_resp({"X-RateLimit-Remaining": "n/a", "X-RateLimit-Reset": ""}))
    assert lim.remaining is None
    assert lim.reset_at is None


def test_before_request_does_not_sleep_while_budget_remains(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    lim = GitHubRateLimiter()
    lim.remaining = 100
    lim.before_request()
    assert slept == []


def test_before_request_waits_until_reset_then_clears_the_stale_count(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    lim = GitHubRateLimiter()
    lim.remaining = 0
    lim.reset_at = int(time.time()) + 30

    lim.before_request()

    assert slept and 25 <= slept[0] <= 35
    assert lim.remaining is None  # forces a re-read from the next response


def test_before_request_caps_an_implausible_reset(monkeypatch):
    """A malformed/hostile reset header must not park the app for days."""
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    lim = GitHubRateLimiter()
    lim.remaining = 0
    lim.reset_at = int(time.time()) + 10_000_000

    lim.before_request()

    assert slept == [GitHubRateLimiter.MAX_WAIT_SECONDS]


def test_handle_secondary_limit_prefers_retry_after_and_falls_back_safely():
    lim = GitHubRateLimiter()
    assert lim.handle_secondary_limit(_resp({"Retry-After": "60"})) == 60.0
    assert lim.handle_secondary_limit(_resp({})) == 30.0
    # Retry-After is legally an HTTP-date, which float() cannot parse
    assert lim.handle_secondary_limit(
        _resp({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == 30.0


def test_is_available_reconsiders_a_token_whose_reset_window_has_passed():
    """Regression: `remaining` is only refreshed by USING a token, so an
    exhausted one keeps its stale count forever. Rotation looked only at
    that count, so a token that got its full budget back an hour ago was
    never reconsidered and a pool of N degraded to a pool of one."""
    lim = GitHubRateLimiter()
    lim.remaining = 0

    lim.reset_at = int(time.time()) + 600
    assert lim.is_available() is False       # genuinely exhausted

    lim.reset_at = int(time.time()) - 600
    assert lim.is_available() is True        # window passed, budget is back


def _client_with_two_tokens(cls):
    client = cls(tokens=["a", "b"])
    return client


def test_rest_rotation_moves_to_a_token_whose_reset_has_passed():
    client = _client_with_two_tokens(GitHubClient)
    client._limiters[0].remaining = 0
    client._limiters[0].reset_at = int(time.time()) + 600   # still exhausted
    client._limiters[1].remaining = 0
    client._limiters[1].reset_at = int(time.time()) - 600   # recovered

    client._maybe_rotate()

    assert client._active == 1
    # the stale count is dropped so before_request() doesn't sleep on it
    assert client._limiters[1].remaining is None


def test_graphql_client_rotates_across_tokens_too():
    """The README advertises multi-token rotation; the nightly bulk
    verification runs on GraphQL and used to use tokens[0] only."""
    client = _client_with_two_tokens(GitHubGraphQLClient)
    assert len(client._sessions) == 2

    client._limiters[0].remaining = 0
    client._limiters[0].reset_at = int(time.time()) + 600
    client._maybe_rotate()

    assert client._active == 1
