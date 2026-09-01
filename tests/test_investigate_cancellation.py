"""Stop used to be honoured only inside the archive-scan loop, and a
cancelled run was then persisted as status="done" at 100% - a truncated
investigation presented as a complete one, whose report understates
coverage with no indication why."""

from unittest.mock import MagicMock

import forkforensics.investigate as inv
from forkforensics.models import ForkCoverage, ForkInfo, ProbeResult


def _fork(name):
    return ForkInfo(full_name=name, owner=name.split("/")[0], repo_name=name.split("/")[1],
                    created_at="2020-01-01T00:00:00Z", pushed_at="2021-01-01T00:00:00Z",
                    default_branch="main", depth=1)


def _patch_pipeline(monkeypatch, n_forks=5, n_shas=5):
    """Replaces every network-touching step with counted fakes, so the test
    observes only how far the loops actually got."""
    calls = {"coverage": 0, "probe": 0}

    monkeypatch.setattr(inv, "discover_forks",
                        lambda *a, **k: [_fork(f"u{i}/r") for i in range(n_forks)])

    def _coverage(client, fork):
        calls["coverage"] += 1
        return ForkCoverage(fork=fork, oldest_commit_date="2020-01-01T00:00:00Z",
                            oldest_commit_sha=f"sha{calls['coverage']:07d}", commit_count=1)
    monkeypatch.setattr(inv, "compute_coverage", _coverage)
    monkeypatch.setattr(inv, "rank_forks", lambda c: c)
    monkeypatch.setattr(inv, "_detect_reset_window", lambda *a, **k: None)

    def _check_alive(client, cache, owner, repo, sha, refs, **kw):
        calls["probe"] += 1
        return ProbeResult(sha=sha, reachable_from_current_refs=True, api_alive=True,
                           raw_alive=True, git_fetch_alive=True, checked_at="now")
    monkeypatch.setattr(inv, "check_alive", _check_alive)
    return calls


def _client():
    c = MagicMock()
    c.get_refs.return_value = []
    return c


def test_stop_during_the_fork_scan_actually_stops_it(monkeypatch, tmp_path):
    calls = _patch_pipeline(monkeypatch, n_forks=50)

    report = inv.investigate(_client(), MagicMock(), "acme", "widget", tmp_path,
                             cancel_cb=lambda: True)

    assert calls["coverage"] == 1        # stopped after the first fork
    assert report["cancelled"] is True


def test_stop_during_the_probe_phase_actually_stops_it(monkeypatch, tmp_path):
    """The fork loop must complete (so there are candidate SHAs to probe),
    then cancellation kicks in during the probes."""
    calls = _patch_pipeline(monkeypatch, n_forks=3)
    state = {"forks_done": False}

    real_rank = inv.rank_forks
    def _rank(c):
        state["forks_done"] = True
        return real_rank(c)
    monkeypatch.setattr(inv, "rank_forks", _rank)

    report = inv.investigate(_client(), MagicMock(), "acme", "widget", tmp_path,
                             cancel_cb=lambda: state["forks_done"])

    assert calls["coverage"] == 3        # the fork loop finished
    assert calls["probe"] == 1           # the probe loop stopped immediately
    assert report["cancelled"] is True


def test_a_completed_run_is_not_flagged_as_cancelled(monkeypatch, tmp_path):
    """Counter-proof: the flag must not appear on a normal run, or the UI
    would warn about partial data on every single report."""
    calls = _patch_pipeline(monkeypatch, n_forks=3)

    report = inv.investigate(_client(), MagicMock(), "acme", "widget", tmp_path,
                             cancel_cb=lambda: False)

    assert calls["coverage"] == 3
    assert report["cancelled"] is False


def test_no_cancel_cb_at_all_still_completes(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, n_forks=2)
    report = inv.investigate(_client(), MagicMock(), "acme", "widget", tmp_path)
    assert report["cancelled"] is False
