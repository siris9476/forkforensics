"""The watchlist was only ever tested as a boolean flag, so the thing it
actually promises - that a watched repo really comes back from
due_for_check - was never checked. Two real defects lived in that gap."""

import sys
from datetime import date, datetime, timezone

import pytest
from PySide6.QtCore import QCoreApplication

from desktop.workers import AddToWatchlistWorker
from forkforensics.cache import CacheManager


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _fake_client(repo_id=42, full_name="acme/widget"):
    return lambda tokens=None: type(
        "_C", (), {"get_repo": lambda self, o, r: {"id": repo_id, "full_name": full_name}})()


def _today():
    return date.today().isoformat()


def test_a_repo_just_added_to_the_watchlist_is_due_today(monkeypatch, tmp_path):
    """Regression: next_check_due was stored as a full ISO timestamp while
    due_for_check compares against a bare YYYY-MM-DD, so
    "2026-09-02T10:00:00+00:00" <= "2026-09-02" was False and the entry was
    not due on the day it was added."""
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", _fake_client())
    cache = CacheManager(tmp_path / "t.db")

    AddToWatchlistWorker(cache, ["tok"], "acme", "widget").run()

    due = {row["repo_id"] for row in cache.due_for_check(_today(), limit=10)}
    assert 42 in due
    cache.close()


def test_watching_an_already_vanished_repo_puts_it_back_in_the_queue(monkeypatch, tmp_path):
    """Regression: due_for_check filters status IN ('active','renamed') and
    the known_repos upsert never resets status, so adding an
    already-vanished repo produced a watchlist row that was silently never
    rechecked again - even though a private repo CAN be made public again."""
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", _fake_client())
    cache = CacheManager(tmp_path / "t.db")

    cache.upsert_known_repo(42, "acme/widget", "acme", "2024-01-01", "2024-01-01")
    cache.mark_gone(42)
    assert cache.get_known_repo(42)["status"] == "gone_or_private"

    AddToWatchlistWorker(cache, ["tok"], "acme", "widget").run()

    assert cache.count_watchlist() == 1
    assert {row["repo_id"] for row in cache.due_for_check(_today(), limit=10)} == {42}
    cache.close()


def test_reactivate_does_not_disturb_a_healthy_repo(tmp_path):
    """It must only lift the terminal state, never overwrite a 'renamed'
    repo back to 'active' (renamed rows are already in the queue)."""
    cache = CacheManager(tmp_path / "t.db")
    cache.upsert_known_repo(7, "acme/renamed", "acme", "2024-01-01", "2024-01-01")
    cache.mark_renamed(7, "acme/new-name", "2024-01-02", "2024-01-16")

    cache.reactivate_for_checking(7)

    assert cache.get_known_repo(7)["status"] == "renamed"
    cache.close()


def test_watchlist_entry_survives_a_clear_all(monkeypatch, tmp_path):
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", _fake_client())
    cache = CacheManager(tmp_path / "t.db")
    cache.upsert_known_repo(1, "noise/repo", "noise", "2024-01-01", "2024-01-01")
    AddToWatchlistWorker(cache, ["tok"], "acme", "widget").run()

    removed = cache.clear_known_repos()

    assert removed == 1                      # only the passively discovered row
    assert cache.count_watchlist() == 1
    assert cache.get_known_repo(42) is not None
    cache.close()


def test_seen_at_still_records_a_full_timestamp(monkeypatch, tmp_path):
    """Only next_check_due needed to become a date; last_seen_active is
    genuinely a moment in time and must keep its precision."""
    import desktop.workers as w
    monkeypatch.setattr(w, "GitHubClient", _fake_client())
    cache = CacheManager(tmp_path / "t.db")

    before = datetime.now(timezone.utc).isoformat()
    AddToWatchlistWorker(cache, ["tok"], "acme", "widget").run()

    row = cache.get_known_repo(42)
    assert row["last_seen_active"] >= before[:10]
    assert "T" in row["last_seen_active"]
    assert "T" not in row["next_check_due"]
    cache.close()
