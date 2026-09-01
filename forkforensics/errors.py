"""Typed exceptions and retry helper shared by all networking modules."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class GitHubAPIError(Exception):
    """Non-transient error from the GitHub REST/GraphQL API."""


class RateLimitExceeded(Exception):
    """Rate limit exhausted and not recoverable within the allowed wait."""


class ArchiveDownloadError(Exception):
    """Error downloading/parsing a GH Archive hourly file."""


def retry_with_backoff(
    fn: Callable[[], T],
    retries: int = 4,
    base_delay: float = 1.0,
    retriable_status: set[int] = RETRIABLE_STATUS,
    status_getter: Callable[[Exception], int | None] | None = None,
) -> T:
    """Runs fn() with exponential backoff + jitter on transient errors.

    status_getter extracts the HTTP status from an exception (e.g. requests
    HTTPError.response.status_code) to decide whether retrying makes sense;
    if None, it retries on any exception up to `retries`.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we want to catch everything here
            last_exc = exc
            status = status_getter(exc) if status_getter else None
            if status is not None and status not in retriable_status:
                raise
            if attempt == retries:
                break
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            logger.warning(
                "retry %s/%s after error %r, waiting %.1fs",
                attempt + 1, retries, last_exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
