"""Streaming download of GH Archive hourly files - never all in RAM,
atomic write (.part then rename), resume via CacheManager."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
import requests

from .archive_index import HOURLY_URL
from .cache import CacheManager

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB


async def fetch_hour(session: aiohttp.ClientSession, date: str, hour: int,
                     raw_dir: Path, cache: CacheManager, retries: int = 4) -> Path | None:
    dest = raw_dir / f"{date}-{hour}.json.gz"
    if dest.exists():
        # The name is deterministic (date+hour) and the write is atomic
        # (.part then rename): if the file is there, it's a complete and
        # valid download, period - no need to also query the "downloaded"
        # flag in the database, which moreover may have stayed true even
        # after the file was deleted (both ingest flows do that after
        # processing it).
        return dest

    url = HOURLY_URL.format(date=date, hour=hour)
    part = dest.with_suffix(dest.suffix + ".part")
    raw_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 404:
                    logger.info("%s-%s: 404 (not yet published or out of range)", date, hour)
                    return None
                resp.raise_for_status()
                size = 0
                with open(part, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        f.write(chunk)
                        size += len(chunk)
                part.rename(dest)
                cache.mark_hour_downloaded(date, hour, size)
                return dest
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("error downloading %s-%s (attempt %d/%d): %r",
                          date, hour, attempt + 1, retries, exc)
            await asyncio.sleep(2 ** attempt)
    logger.error("%s-%s: failed after %d attempts", date, hour, retries)
    return None


async def fetch_range(hour_list: list[tuple[str, int]], raw_dir: Path,
                      cache: CacheManager, concurrency: int = 4):
    """Async generator: yields Paths as they complete, so the filter
    can start right away without waiting for the whole range."""
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(session: aiohttp.ClientSession, date_: str, hour: int):
        async with sem:
            return await fetch_hour(session, date_, hour, raw_dir, cache)

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(_bounded(session, d, h)) for d, h in hour_list]
        for task in asyncio.as_completed(tasks):
            path = await task
            if path is not None:
                yield path


def fetch_range_threaded(hour_list: list[tuple[str, int]], raw_dir: Path,
                         cache: CacheManager, concurrency: int = 4):
    """Synchronous fallback (requests + ThreadPoolExecutor) for when you
    don't want to depend on aiohttp - same interface (Path iterator)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(date_: str, hour: int) -> Path | None:
        dest = raw_dir / f"{date_}-{hour}.json.gz"
        if dest.exists():
            return dest
        url = HOURLY_URL.format(date=date_, hour=hour)
        part = dest.with_suffix(dest.suffix + ".part")
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(url, stream=True, timeout=120) as resp:
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                size = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        f.write(chunk)
                        size += len(chunk)
                part.rename(dest)
                cache.mark_hour_downloaded(date_, hour, size)
                return dest
        except requests.RequestException as exc:
            logger.error("error downloading %s-%s: %r", date_, hour, exc)
            return None

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one, d, h) for d, h in hour_list]
        for fut in as_completed(futures):
            path = fut.result()
            if path is not None:
                yield path
