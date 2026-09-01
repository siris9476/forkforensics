"""Unifies forks/archive/probe into a single timeline, finds coverage gaps
and identifies "at-risk" SHAs (orphaned but still alive RIGHT NOW)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import ForkCoverage, ProbeResult, TimelineEntry


def build_unified_timeline(coverages: list[ForkCoverage],
                          archive_events: list, probe_results: list[ProbeResult]
                          ) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    for cov in coverages:
        if cov.oldest_commit_date:
            entries.append(TimelineEntry(
                date=cov.oldest_commit_date, sha=cov.oldest_commit_sha or "",
                source=f"fork:{cov.fork.full_name}",
            ))
        if cov.fork.pushed_at and (not cov.oldest_commit_date
                                   or to_date(cov.fork.pushed_at) != to_date(cov.oldest_commit_date)):
            # find_gaps() used to sample only ONE point per fork (the
            # oldest commit) - a fork that covers a long period but stops
            # being updated BEFORE another source starts left an uncovered
            # remainder that detection couldn't see (found live on a real
            # case: the fork stalled months before the repo's history
            # restarted, leaving a real gap). pushed_at (the last commit inherited by the
            # fork, already downloaded with the fork list - zero extra API
            # calls) is a second real data point: where its coverage
            # actually ENDS, not just where it begins. Only added if
            # different from the oldest commit, otherwise it would be a
            # duplicate point with no extra information.
            entries.append(TimelineEntry(
                date=cov.fork.pushed_at, sha="",
                source=f"fork-head:{cov.fork.full_name}",
            ))
    for ev in archive_events:
        if ev.head_sha:
            entries.append(TimelineEntry(date=ev.ts, sha=ev.head_sha, source="archive"))

    at_risk_shas = {
        p.sha for p in probe_results
        if not p.reachable_from_current_refs
        and (p.api_alive or p.raw_alive or p.git_fetch_alive)
    }
    for e in entries:
        if e.sha in at_risk_shas:
            e.at_risk = True

    return sorted(entries, key=lambda e: e.date)


def find_gaps(timeline: list[TimelineEntry], granularity: str = "day") -> list[tuple[str, str]]:
    """A fork covers, by construction, EVERY day between its oldest commit
    (source "fork:X") and the last one it inherited (source
    "fork-head:X") - not just those two isolated instants. This function
    used to treat every date as an isolated point: a fork with a known
    "start" and "end" was seen as TWO disconnected observations, and the
    span between them ended up flagged as a gap even when it was actually
    the continuous history of that very same fork (found live on a real
    case - previously patched only at the UI text level, this
    is the structural fix in the data source itself). Now the INTERVALS
    covered by each fork are built, the ones that touch/overlap are
    merged, and the gap is the complement between the merged intervals -
    no longer a naive comparison between points disconnected from their
    source."""
    if len(timeline) < 2:
        return []

    fork_start: dict[str, date] = {}
    fork_end: dict[str, date] = {}
    other_points = []
    for e in timeline:
        if not e.date:
            continue
        d = to_date(e.date)
        if e.source.startswith("fork-head:"):
            fork_end[e.source.split(":", 1)[1]] = d
        elif e.source.startswith("fork:"):
            fork_start[e.source.split(":", 1)[1]] = d
        else:
            other_points.append(d)

    # Iterate the UNION of starts and ends: a fork whose coverage failed to
    # compute still emits a "fork-head" entry (its pushed_at) with no
    # matching "fork:" start. Iterating fork_start alone discarded those
    # points entirely, so a date known to be covered dropped out of the
    # analysis and the span around it got reported as one large gap that
    # did not actually exist.
    intervals = []
    for name in fork_start.keys() | fork_end.keys():
        start = fork_start.get(name)
        end = fork_end.get(name)
        if start is None:
            intervals.append((end, end))
        elif end and end >= start:
            intervals.append((start, end))
        else:
            intervals.append((start, start))

    covered = sorted(intervals + [(d, d) for d in other_points])
    if not covered:
        return []
    merged = [covered[0]]
    for start, end in covered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + timedelta(days=1):
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    gaps = []
    for (_, end_a), (start_b, _) in zip(merged, merged[1:]):
        if (start_b - end_a).days > 1:
            gaps.append((end_a.isoformat(), start_b.isoformat()))
    return gaps


def identify_at_risk(probe_results: list[ProbeResult]) -> list[ProbeResult]:
    return [p for p in probe_results
            if not p.reachable_from_current_refs
            and (p.api_alive or p.raw_alive or p.git_fetch_alive)]


def to_date(iso_str: str):
    """Accepts both plain dates (YYYY-MM-DD) and ISO timestamps (with Z or
    an explicit offset) and extracts just the date. Public (no longer only
    for internal use) because it's also needed by callers comparing
    oldest_commit_date (may have a time component) with gap dates (always
    plain) - comparing the raw, unnormalized strings would treat them as
    different even when they're the same day."""
    normalized = iso_str.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date()
