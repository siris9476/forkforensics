"""Daily ingest: downloads the GH Archive hourly files for the day that
just ended, extracts EVERY repo seen in ANY public event (not just push)
and registers it in known_repos with a re-verification due date."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path

from .archive_fetch import fetch_range
from .archive_filter import iter_all_repo_sightings
from .archive_index import build_hour_list
from .cache import CacheManager

logger = logging.getLogger(__name__)

RECHECK_DELAY_DAYS = 14  # re-verify a repo N days after the last activity seen


def _next_check_due(seen_date: date) -> str:
    return (seen_date + timedelta(days=RECHECK_DELAY_DAYS)).isoformat()


async def ingest_day(target_date: date, raw_dir: Path, cache: CacheManager,
                     concurrency: int = 4, keep_raw: bool = False,
                     progress_cb=None, cancel_cb=None) -> int:
    """Ingests the 24 hours of a single day. Returns the number of repos
    seen (new or already known, updated). progress_cb(i, total, detail)
    reports progress hour by hour - a single day can already take a
    non-negligible amount of time (24 downloads), this isn't a superfluous
    detail. cancel_cb() -> bool: if it returns True, stops after the
    current hour instead of continuing with the remaining ones."""
    hour_list = build_hour_list(target_date, target_date)
    total_hours = len(hour_list)
    n_repos_seen = 0
    n_done = 0
    seen_at = target_date.isoformat()
    next_due = _next_check_due(target_date)

    async for gz_path in fetch_range(hour_list, raw_dir, cache, concurrency=concurrency):
        n_done += 1
        date_str, hour_str = gz_path.stem.replace(".json", "").rsplit("-", 1)
        if progress_cb:
            progress_cb(n_done, total_hours, f"hour {hour_str} ({n_done}/{total_hours})")
        if not cache.is_hour_processed(date_str, int(hour_str)):
            try:
                sightings = await asyncio.to_thread(iter_all_repo_sightings, gz_path)
            except Exception as exc:  # noqa: BLE001
                logger.error("error processing %s: %r", gz_path, exc)
                sightings = None
            if sightings is not None:
                # one upsert at a time with a commit each was the real
                # bottleneck: a single hour can contain tens of thousands
                # of repos (~32,000 observed live), at ~7ms/row for the
                # individual commit that's minutes instead of seconds.
                rows = [(repo_id, full_name, full_name.split("/", 1)[0], seen_at, next_due)
                       for repo_id, full_name in sightings]
                await asyncio.to_thread(cache.upsert_known_repos_bulk, rows)
                n_repos_seen += len(sightings)
                cache.mark_hour_processed(date_str, int(hour_str), len(sightings), seen_at)
        if not keep_raw:
            gz_path.unlink(missing_ok=True)
        if cancel_cb and cancel_cb():
            logger.info("ingest %s: stopped by user at %d/%d hours",
                       target_date.isoformat(), n_done, total_hours)
            break

    logger.info("ingest %s: %d repo observations", target_date.isoformat(), n_repos_seen)
    return n_repos_seen
