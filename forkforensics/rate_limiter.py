"""GitHub rate limit management (REST and GraphQL share the same
X-RateLimit-* header schema, but separate budgets)."""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class GitHubRateLimiter:
    def __init__(self, min_remaining: int = 5) -> None:
        self.min_remaining = min_remaining
        self.remaining: int | None = None
        self.reset_at: int | None = None

    # Ceiling on any header-driven sleep. GitHub's own resets are at most an
    # hour away; a malformed or hostile header must not park the app for
    # days.
    MAX_WAIT_SECONDS = 3600

    def is_available(self) -> bool:
        """True when this token can serve a request right now.

        `remaining` is only refreshed by actually using the token, so an
        exhausted one keeps its stale small value indefinitely. Without the
        reset_at check, a token that ran out an hour ago (and has had a full
        budget again ever since) was never reconsidered by rotation, and a
        pool of N tokens degraded to "wait on whichever one is current"."""
        if self.remaining is None or self.remaining > self.min_remaining:
            return True
        return self.reset_at is not None and self.reset_at <= time.time()

    def before_request(self) -> None:
        if self.remaining is not None and self.remaining <= self.min_remaining:
            wait = max(0, (self.reset_at or 0) - int(time.time())) + 1
            if wait > self.MAX_WAIT_SECONDS:
                logger.warning("implausible rate-limit reset (%ss away), capping at %ss",
                              wait, self.MAX_WAIT_SECONDS)
                wait = self.MAX_WAIT_SECONDS
            logger.warning("rate limit: %s requests remaining, waiting %ss until reset",
                          self.remaining, wait)
            time.sleep(wait)
            self.remaining = None

    def after_response(self, resp: requests.Response) -> None:
        # Headers are attacker-adjacent input in the general case and simply
        # absent on some error responses: a malformed value must not turn a
        # recoverable rate limit into a crash inside the retry handler.
        self.remaining = _as_int(resp.headers.get("X-RateLimit-Remaining"), self.remaining)
        self.reset_at = _as_int(resp.headers.get("X-RateLimit-Reset"), self.reset_at)

    def handle_secondary_limit(self, resp: requests.Response) -> float:
        """Returns the seconds to wait for a secondary rate limit
        (explicit Retry-After, or a cautious fixed backoff if absent).
        Retry-After is also legally an HTTP-date, which float() cannot
        parse - fall back rather than raising inside the 403 handler."""
        retry_after = _as_float(resp.headers.get("Retry-After"), None)
        if retry_after is None:
            return 30.0
        return min(max(retry_after, 0.0), float(self.MAX_WAIT_SECONDS))


def _as_int(value: str | None, fallback: int | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _as_float(value: str | None, fallback: float | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
