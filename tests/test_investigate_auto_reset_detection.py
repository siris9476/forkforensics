"""If the best fork stops being updated BEFORE the repo's current
history begins, that's the signal of a reset/force-push - the user
should not have to guess a date range to make it surface (verified
live against octocat/data: fork rescuer stalled at 2025-06-19,
current history restarted from 2026-03-26)."""

from datetime import date
from pathlib import Path

import forkforensics.investigate as inv
from forkforensics.models import ArchiveEvent, ForkCoverage, ForkInfo, ProbeResult


def _fork_coverage(pushed_at, oldest_commit_date="2018-06-15T09:46:39Z"):
    fork = ForkInfo(full_name="rescuer/data", owner="rescuer", repo_name="data",
                    default_branch="master", created_at="2018-06-16T00:00:00Z",
                    pushed_at=pushed_at, depth=1)
    return ForkCoverage(fork=fork, oldest_commit_sha="e878af6978", oldest_commit_date=oldest_commit_date,
                        commit_count=1989)


class _FakeClient:
    def __init__(self, live_root):
        self._live_root = live_root

    def get_refs(self, owner, repo):
        return []

    def oldest_commit_via_last_page(self, owner, repo, sha=None):
        return self._live_root


def _setup(monkeypatch, coverage, live_root, probed_events):
    monkeypatch.setattr(inv, "discover_forks", lambda *a, **k: [coverage.fork])
    monkeypatch.setattr(inv, "compute_coverage", lambda client, fork: coverage)
    monkeypatch.setattr(inv, "rank_forks", lambda coverages: coverages)
    monkeypatch.setattr(inv, "check_alive", lambda client, cache, owner, repo, sha, refs:
                        ProbeResult(sha=sha, reachable_from_current_refs=False, api_alive=False,
                                   raw_alive=False, git_fetch_alive=False, checked_at="2026-01-01T00:00:00Z"))

    captured_window = {}

    def _fake_build_hour_list(d1, d2):
        captured_window["from"], captured_window["to"] = d1, d2
        return [(d1.isoformat(), 0)]

    monkeypatch.setattr(inv, "build_hour_list", _fake_build_hour_list)
    monkeypatch.setattr(inv, "fetch_range_threaded",
                        lambda hour_list, raw_dir, cache, concurrency=4: iter([Path("dummy.gz")]))
    monkeypatch.setattr(inv, "process_hour_file", lambda gz_path, target_full_name: probed_events)
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)
    return captured_window


def test_gap_between_fork_end_and_live_history_start_triggers_auto_scan(tmp_path, monkeypatch):
    coverage = _fork_coverage(pushed_at="2025-06-19T12:00:00Z")
    live_root = ("newroot123", "2026-03-26T08:00:00Z", 65)
    captured = _setup(monkeypatch, coverage, live_root, probed_events=[])

    inv.investigate(_FakeClient(live_root), cache=None, owner="octocat", repo="data",
                    raw_dir=tmp_path)  # NO date_from/date_to passed by the user

    assert captured.get("from") == date(2026, 3, 25)
    assert captured.get("to") == date(2026, 3, 27)


def test_no_gap_when_fork_still_covers_past_live_history_start(tmp_path, monkeypatch):
    """The fork was updated AFTER the current history's start - there is
    no gap to fill, the automatic scan must not trigger."""
    coverage = _fork_coverage(pushed_at="2026-04-01T12:00:00Z")
    live_root = ("newroot123", "2026-03-26T08:00:00Z", 65)
    captured = _setup(monkeypatch, coverage, live_root, probed_events=[])

    inv.investigate(_FakeClient(live_root), cache=None, owner="octocat", repo="data",
                    raw_dir=tmp_path)

    assert captured == {}


def test_explicit_date_range_from_user_is_not_overridden(tmp_path, monkeypatch):
    coverage = _fork_coverage(pushed_at="2025-06-19T12:00:00Z")
    live_root = ("newroot123", "2026-03-26T08:00:00Z", 65)
    captured = _setup(monkeypatch, coverage, live_root, probed_events=[])

    inv.investigate(_FakeClient(live_root), cache=None, owner="octocat", repo="data",
                    raw_dir=tmp_path, date_from=date(2020, 1, 1), date_to=date(2020, 1, 2))

    assert captured.get("from") == date(2020, 1, 1)
    assert captured.get("to") == date(2020, 1, 2)


def test_auto_detected_window_surfaces_before_sha_of_the_reset_push(tmp_path, monkeypatch):
    coverage = _fork_coverage(pushed_at="2025-06-19T12:00:00Z")
    live_root = ("newroot123", "2026-03-26T08:00:00Z", 65)
    reset_event = ArchiveEvent(ts="2026-03-26T08:00:00Z", actor="someone", ref="refs/heads/master",
                               before_sha="orphanhead" * 4, head_sha="newroot123")
    _setup(monkeypatch, coverage, live_root, probed_events=[reset_event])

    probed = []
    monkeypatch.setattr(inv, "check_alive", lambda client, cache, owner, repo, sha, refs:
                        probed.append(sha) or ProbeResult(
                            sha=sha, reachable_from_current_refs=False, api_alive=False,
                            raw_alive=False, git_fetch_alive=False, checked_at="2026-01-01T00:00:00Z"))

    inv.investigate(_FakeClient(live_root), cache=None, owner="octocat", repo="data",
                    raw_dir=tmp_path)

    assert "orphanhead" * 4 in probed
