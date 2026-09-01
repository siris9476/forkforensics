"""End-to-end orchestrator: given owner/repo (+ an optional date range for
targeted reconstruction via GH Archive), runs fork scan -> candidate SHA
collection -> survival probe -> report.

Used both by the manual flow (/investigate) and by the automatic trigger
on a new row in `disappearances`.

NOTE (verified on a real vanished repository, real case): a "rescue" fork the user
makes on top of an already-found fork (e.g. a fork of the best fork) is a
fork-of-fork, so at depth 2, not 1 - with max_depth=1 it never shows up in
the ranking even if it exists and is alive. Defaults to max_depth=2 for
this reason: negligible extra cost (most forks don't have further forks of
their own, so the extra scan is almost always an empty API call), concrete
benefit (the most trustworthy fork, the one under the user's direct
control, becomes visible).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .archive_fetch import fetch_range_threaded
from .archive_filter import process_hour_file
from .archive_index import build_hour_list
from .cache import CacheManager
from .fork_scan import compute_coverage, discover_forks, rank_forks
from .github_rest import GitHubClient
from .report import build_report
from .survival_probe import check_alive
from .timeline import build_unified_timeline, to_date

ProgressCB = Callable[[str, str, float], None]  # (phase, detail, pct) -> None


def _noop_progress(phase: str, detail: str, pct: float) -> None:
    pass


def _detect_reset_window(client: GitHubClient, owner: str, repo: str,
                         coverages: list) -> tuple[date, date] | None:
    """If the best fork stops being updated BEFORE the repo's current
    history begins, that's the signal of a reset/force-push (verified live
    on a real case: the best fork stopped months before the repo's
    current history restarted - a gap no fork covers). In that
    case there's no need to ask the user to guess the date: the window to
    scan on GH Archive is the narrow one around the start of the current
    history, where by construction the push that did the reset happened."""
    usable = [c for c in coverages if c.oldest_commit_date and not c.error]
    if not usable:
        return None
    best = usable[0]  # coverages is already ranked by rank_forks
    if not best.fork.pushed_at:
        return None
    try:
        result = client.oldest_commit_via_last_page(owner, repo)
    except Exception:  # noqa: BLE001
        return None
    if result is None:
        return None
    _sha, live_root_date, _count = result
    fork_end = to_date(best.fork.pushed_at)
    live_start = to_date(live_root_date)
    if live_start <= fork_end:
        return None  # no gap: the fork covers past the current start
    return live_start - timedelta(days=1), live_start + timedelta(days=1)


def investigate(client: GitHubClient, cache: CacheManager, owner: str, repo: str,
                raw_dir: Path, date_from: date | None = None, date_to: date | None = None,
                max_depth: int = 2, max_forks: int = 200,
                progress_cb: ProgressCB = _noop_progress, cancel_cb=None) -> dict:
    cancelled = False

    def _cancelled() -> bool:
        return bool(cancel_cb and cancel_cb())

    progress_cb("fork_scan", f"discovering forks of {owner}/{repo}", 0.05)
    forks = discover_forks(client, owner, repo, max_depth=max_depth, max_forks=max_forks)
    coverages = []
    for i, fork in enumerate(forks):
        coverages.append(compute_coverage(client, fork))
        progress_cb("fork_scan", f"{i + 1}/{len(forks)} forks analyzed", 0.05 + 0.35 * (i + 1) / max(len(forks), 1))
        # up to max_forks API calls happen here; without this check Stop did
        # nothing during the phase and the UI sat on "Stopping..." with both
        # buttons disabled until the entire scan finished
        if _cancelled():
            cancelled = True
            progress_cb("fork_scan", "cancelled by the user", 0.4)
            break
    coverages = rank_forks(coverages)

    auto_detected = False
    if not (date_from and date_to):
        window = _detect_reset_window(client, owner, repo, coverages)
        if window:
            date_from, date_to = window
            auto_detected = True

    archive_events = []
    if date_from and date_to:
        if auto_detected:
            progress_cb("archive_scan",
                       f"gap auto-detected around {date_to.isoformat()} "
                       f"(the best fork stops before the current history begins) "
                       f"- targeted GH Archive scan", 0.42)
        progress_cb("archive_scan", "downloading/filtering GH Archive for the given range", 0.45)
        hour_list = build_hour_list(date_from, date_to)
        target = f"{owner}/{repo}"
        for i, gz_path in enumerate(fetch_range_threaded(hour_list, raw_dir, cache, concurrency=4)):
            archive_events.extend(process_hour_file(gz_path, target_full_name=target))
            # GH Archive is permanent and always re-downloadable: there's no
            # point hoarding the raw files (for a wide range these can be
            # many GB) just for the rare case of an identical
            # re-investigation - what's actually needed (candidate SHAs,
            # outcome) stays in SQLite anyway (probe_results, jobs).
            gz_path.unlink(missing_ok=True)
            if i % 10 == 0:
                progress_cb("archive_scan", f"{i} hourly files processed",
                           0.45 + 0.25 * min(i / max(len(hour_list), 1), 1.0))
            if _cancelled():
                cancelled = True
                progress_cb("archive_scan", "cancelled by the user", 0.7)
                break

    progress_cb("probe", "checking survival of candidate SHAs", 0.75)
    refs = client.get_refs(owner, repo)
    candidate_shas = set()
    for cov in coverages:
        if cov.oldest_commit_sha:
            candidate_shas.add(cov.oldest_commit_sha)
    for ev in archive_events:
        if ev.head_sha:
            candidate_shas.add(ev.head_sha)
        # before_sha is the tip of the history BEFORE that push - on a
        # reset/force-push it's exactly the orphan commit that carries the
        # whole cancelled ancestor chain with it (verified live on a real
        # gap: head_sha after a reset points to the NEW history, which is
        # already reachable and adds nothing).
        if ev.before_sha and ev.before_sha != "0" * 40:
            # "0000...0" is GitHub's conventional before_sha for the first
            # push of a new branch (no prior history) - it's not a real
            # SHA, must be discarded or every probe would fail for nothing.
            candidate_shas.add(ev.before_sha)

    probe_results = []
    for i, sha in enumerate(candidate_shas):
        probe_results.append(check_alive(client, cache, owner, repo, sha, refs))
        if i % 20 == 0:
            progress_cb("probe", f"{i}/{len(candidate_shas)} SHAs checked",
                       0.75 + 0.15 * min(i / max(len(candidate_shas), 1), 1.0))
        # one network probe per SHA - the other phase where Stop used to be
        # ignored entirely
        if _cancelled():
            cancelled = True
            progress_cb("probe", "cancelled by the user", 0.9)
            break

    timeline = build_unified_timeline(coverages, archive_events, probe_results)
    progress_cb("report", "building final report", 0.95)
    report = build_report(owner, repo, coverages, timeline, probe_results)
    # A partial run must say so. Without this the caller stored it as
    # status="done", progress=100% and showed "Completed." - a truncated
    # investigation presented as a full one, whose report understates
    # coverage with no indication why.
    report["cancelled"] = cancelled
    progress_cb("done", "cancelled" if cancelled else "completed", 1.0)
    return report
