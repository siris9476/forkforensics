"""Estimates size/time/disk space of a GH Archive range BEFORE downloading
a single byte. No silent default on the range: the caller must always
pass explicit date_from/date_to."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, timedelta

import requests

HOURLY_URL = "https://data.gharchive.org/{date}-{hour}.json.gz"
EARLIEST_DATE = date(2011, 2, 12)  # actual GH Archive limit


@dataclass
class EstimateResult:
    n_hours: int
    sampled_hours: int
    avg_bytes_per_hour: float
    estimated_total_bytes: int
    estimated_gb: float
    free_disk_bytes: int
    disk_ok: bool


def build_hour_list(date_from: date, date_to: date) -> list[tuple[str, int]]:
    if date_from < EARLIEST_DATE:
        raise ValueError(f"GH Archive starts on {EARLIEST_DATE.isoformat()}")
    if date_to < date_from:
        raise ValueError("date_to precedes date_from")
    out = []
    d = date_from
    while d <= date_to:
        for h in range(24):
            out.append((d.isoformat(), h))
        d += timedelta(days=1)
    return out


def estimate_range(hour_list: list[tuple[str, int]], cache_dir_for_disk_check,
                   sample_size: int = 5,
                   session: requests.Session | None = None) -> EstimateResult:
    """Samples a distributed subset (first, last, intermediate points)
    with HEAD requests to estimate the average size/hour - it doesn't
    assume a fixed size, which varies a lot between 2011 and today."""
    session = session or requests.Session()
    n = len(hour_list)
    if n == 0:
        return EstimateResult(0, 0, 0.0, 0, 0.0, 0, True)

    idx = sorted(set(int(i * (n - 1) / max(sample_size - 1, 1)) for i in range(sample_size)))
    sizes = []
    for i in idx:
        d, h = hour_list[i]
        url = HOURLY_URL.format(date=d, hour=h)
        try:
            resp = session.head(url, timeout=15, allow_redirects=True)
            if resp.status_code == 200:
                cl = resp.headers.get("Content-Length")
                if cl:
                    sizes.append(int(cl))
        except requests.RequestException:
            continue

    avg = sum(sizes) / len(sizes) if sizes else 50_000_000.0  # cautious fallback: 50MB
    total = int(avg * n)
    free = shutil.disk_usage(cache_dir_for_disk_check).free

    return EstimateResult(
        n_hours=n, sampled_hours=len(sizes), avg_bytes_per_hour=avg,
        estimated_total_bytes=total, estimated_gb=total / 1e9,
        free_disk_bytes=free, disk_ok=free > total * 1.2,
    )
