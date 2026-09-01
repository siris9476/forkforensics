"""The Qt workers (QThread) in desktop/workers.py are thin wrappers around
functions already tested in forkforensics/ - but they had zero coverage: a
bug in the signal signature, in how the result gets packaged, or in
exception handling wouldn't have surfaced until someone actually clicked a
button in the app. Here .run() is called directly (not .start()) to
execute the worker's body synchronously, on the test's own thread - Qt
signals still work with a direct connection, all that's needed is a
QCoreApplication instance."""

import sys

import pytest
from PySide6.QtCore import QCoreApplication

from desktop.workers import (AddToWatchlistWorker, CloneForkWorker, DailyCycleWorker,
                             InvestigateWorker, MonitoredRefreshWorker, RateLimitWorker,
                             RecheckShaWorker, RescueWorker)
from forkforensics.cache import CacheManager
from forkforensics.models import ProbeResult
from forkforensics.rescue import CloneResult, RescueError, RescueResult


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _capture(signal):
    results = []
    signal.connect(lambda *a: results.append(a[0] if len(a) == 1 else a))
    return results


def test_investigate_worker_emits_finished_ok_with_the_report(monkeypatch, tmp_path):
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: object())
    monkeypatch.setattr(w, "investigate", lambda *a, **k: {"fork_ranking": [], "at_risk_shas": []})

    worker = InvestigateWorker(cache=None, tokens=["tok"], owner="acme", repo="widget",
                               raw_dir=tmp_path, date_from=None, date_to=None)
    ok = _capture(worker.finished_ok)
    failed = _capture(worker.failed)

    worker.run()

    assert ok == [{"fork_ranking": [], "at_risk_shas": []}]
    assert failed == []


def test_investigate_worker_emits_failed_on_exception(monkeypatch, tmp_path):
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: object())

    def _boom(*a, **k):
        raise ValueError("acme/widget not found")
    monkeypatch.setattr(w, "investigate", _boom)

    worker = InvestigateWorker(cache=None, tokens=["tok"], owner="acme", repo="widget",
                               raw_dir=tmp_path, date_from=None, date_to=None)
    ok = _capture(worker.finished_ok)
    failed = _capture(worker.failed)

    worker.run()

    assert ok == []
    assert "acme/widget not found" in failed[0]


def test_daily_cycle_worker_emits_finished_ok_with_stats(monkeypatch, tmp_path):
    import desktop.workers as w

    async def _fake_run_daily_cycle(*a, **k):
        return {"repos_seen": 10, "auto_investigated": 2}
    monkeypatch.setattr(w, "run_daily_cycle", _fake_run_daily_cycle)

    worker = DailyCycleWorker(cache=None, raw_dir=tmp_path, tokens=["tok"])
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok == [{"repos_seen": 10, "auto_investigated": 2}]


def test_daily_cycle_worker_emits_failed_on_missing_token(monkeypatch, tmp_path):
    import desktop.workers as w

    async def _fake_run_daily_cycle(*a, **k):
        raise RuntimeError("a GitHub token is required")
    monkeypatch.setattr(w, "run_daily_cycle", _fake_run_daily_cycle)

    worker = DailyCycleWorker(cache=None, raw_dir=tmp_path, tokens=None)
    failed = _capture(worker.failed)

    worker.run()

    assert "a GitHub token is required" in failed[0]


def test_rescue_worker_emits_finished_ok_with_rescue_result(monkeypatch, tmp_path):
    import desktop.workers as w
    fake_result = RescueResult(local_path=tmp_path / "x", branch="rescue/abc123")
    monkeypatch.setattr(w, "rescue_sha_locally", lambda *a, **k: fake_result)

    worker = RescueWorker("acme", "widget", "abc123", tmp_path)
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok == [fake_result]


def test_rescue_worker_emits_failed_with_rescue_error_message(monkeypatch, tmp_path):
    import desktop.workers as w
    monkeypatch.setattr(w, "rescue_sha_locally",
                        lambda *a, **k: (_ for _ in ()).throw(RescueError("folder already exists")))

    worker = RescueWorker("acme", "widget", "abc123", tmp_path)
    failed = _capture(worker.failed)

    worker.run()

    assert failed == ["folder already exists"]


def test_clone_fork_worker_emits_finished_ok_with_clone_result(monkeypatch, tmp_path):
    import desktop.workers as w
    fake_result = CloneResult(local_path=tmp_path / "x")
    monkeypatch.setattr(w, "clone_fork_locally", lambda *a, **k: fake_result)

    worker = CloneForkWorker("rescuer/data", tmp_path)
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok == [fake_result]


def test_clone_fork_worker_forwards_progress_events(monkeypatch, tmp_path):
    """Regression: the worker must pass the real progress_cb through to
    clone_fork_locally and re-emit it as a Qt signal - not just ignore
    it."""
    import desktop.workers as w

    def _fake_clone(fork_full_name, dest_root, progress_cb=None):
        progress_cb("Receiving objects: 50% (500/1000)", 0.5)
        progress_cb("Receiving objects: 100% (1000/1000), done.", 1.0)
        return CloneResult(local_path=dest_root / "x")
    monkeypatch.setattr(w, "clone_fork_locally", _fake_clone)

    worker = CloneForkWorker("rescuer/data", tmp_path)
    events = []
    worker.progress.connect(lambda line, pct: events.append((line, pct)))

    worker.run()

    assert events[0][1] == 0.5
    assert events[1][1] == 1.0


def test_add_to_watchlist_worker_resolves_and_marks_watched(monkeypatch, tmp_path):
    import desktop.workers as w
    cache = CacheManager(tmp_path / "t.db")
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: type(
        "_C", (), {"get_repo": lambda self, owner, repo: {"id": 42, "full_name": "acme/widget"}})())

    worker = AddToWatchlistWorker(cache, ["tok"], "acme", "widget")
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok == [{"repo_id": 42, "full_name": "acme/widget"}]
    assert cache.count_watchlist() == 1
    assert cache.get_known_repo(42)["full_name"] == "acme/widget"


def test_add_to_watchlist_worker_emits_failed_when_repo_not_found(monkeypatch, tmp_path):
    import desktop.workers as w
    cache = CacheManager(tmp_path / "t.db")
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: type(
        "_C", (), {"get_repo": lambda self, owner, repo: None})())

    worker = AddToWatchlistWorker(cache, ["tok"], "acme", "ghost")
    failed = _capture(worker.failed)

    worker.run()

    assert "not found" in failed[0]
    assert cache.count_watchlist() == 0


def test_monitored_refresh_worker_emits_finished_ok_with_counts_and_rows(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    cache.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")

    worker = MonitoredRefreshWorker(cache, status=None, search=None, limit=50, offset=0)
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok[0]["total"] == 1
    assert ok[0]["counts"] == {"active": 1}
    assert ok[0]["rows"][0]["full_name"] == "a/b"


def test_monitored_refresh_worker_emits_failed_on_exception(monkeypatch, tmp_path):
    cache = CacheManager(tmp_path / "t.db")

    def _boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(cache, "count_known_repos_by_status", _boom)

    worker = MonitoredRefreshWorker(cache, status=None, search=None, limit=50, offset=0)
    failed = _capture(worker.failed)

    worker.run()

    assert "database is locked" in failed[0]


def test_rate_limit_worker_emits_finished_ok_with_per_token_status(monkeypatch, tmp_path):
    import desktop.workers as w
    fake_results = [{"limit": 5000, "remaining": 4821, "reset_at": 1234567},
                    {"limit": 5000, "remaining": 12, "reset_at": 7654321}]
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: type(
        "_C", (), {"get_rate_limit_all": lambda self: fake_results})())

    worker = RateLimitWorker(["tok-a", "tok-b"])
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok == [fake_results]


def test_rate_limit_worker_emits_failed_on_exception(monkeypatch):
    import desktop.workers as w

    def _boom(tokens=None):
        raise RuntimeError("network unreachable")
    monkeypatch.setattr(w, "GitHubClient", _boom)

    worker = RateLimitWorker(["tok-a"])
    failed = _capture(worker.failed)

    worker.run()

    assert "network unreachable" in failed[0]


def test_recheck_sha_worker_emits_finished_ok_with_probe_result_as_dict(monkeypatch, tmp_path):
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: type(
        "_C", (), {"get_refs": lambda self, o, r: []})())
    fake_probe = ProbeResult(sha="abc123", reachable_from_current_refs=False, api_alive=True,
                             raw_alive=True, git_fetch_alive=True, checked_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(w, "check_alive", lambda *a, **k: fake_probe)

    worker = RecheckShaWorker(cache=None, tokens=["tok"], owner="acme", repo="widget", sha="abc123")
    ok = _capture(worker.finished_ok)

    worker.run()

    assert ok[0]["sha"] == "abc123"
    assert ok[0]["api_alive"] is True


def test_recheck_sha_worker_forces_a_fresh_reprobe_not_the_cached_result(monkeypatch, tmp_path):
    """Regression: without force_reprobe=True, check_alive would return the
    result already in the cache instead of checking NOW - a "recheck"
    button that always returns the same old data would be pointless."""
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", lambda tokens=None: type(
        "_C", (), {"get_refs": lambda self, o, r: []})())

    captured_kwargs = {}
    def _fake_check_alive(client, cache, owner, repo, sha, refs, **kwargs):
        captured_kwargs.update(kwargs)
        return ProbeResult(sha=sha, reachable_from_current_refs=False, api_alive=True,
                           raw_alive=True, git_fetch_alive=True, checked_at="now")
    monkeypatch.setattr(w, "check_alive", _fake_check_alive)

    worker = RecheckShaWorker(cache=None, tokens=["tok"], owner="acme", repo="widget", sha="abc123")
    worker.run()

    assert captured_kwargs.get("force_reprobe") is True
