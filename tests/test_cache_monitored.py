from pathlib import Path

from forkforensics.cache import CacheManager


def _cache(tmp_path) -> CacheManager:
    return CacheManager(tmp_path / "t.db")


def test_upsert_and_count_by_status(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "c/d", "c", "2024-01-01", "2024-01-15")
    counts = c.count_known_repos_by_status()
    assert counts == {"active": 2}


def test_mark_renamed_sets_status_renamed(tmp_path):
    """Regression: mark_renamed never set status='renamed'."""
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "old/name", "old", "2024-01-01", "2024-01-15")
    c.mark_renamed(1, "new/name", "2024-02-01", "2024-02-15")
    row = c.get_known_repo(1)
    assert row["status"] == "renamed"
    assert row["full_name"] == "new/name"


def test_mark_gone_sets_status(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.mark_gone(1)
    assert c.get_known_repo(1)["status"] == "gone_or_private"


def test_due_for_check_includes_renamed_not_just_active(tmp_path):
    """Regression: a renamed repo must stay in the re-verification queue
    (it can still disappear afterward), not drop out of it."""
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-02")
    c.mark_renamed(1, "a/c", "2024-01-01", "2024-01-02")
    c.upsert_known_repo(2, "x/y", "x", "2024-01-01", "2024-01-02")
    c.mark_gone(2)
    due = c.due_for_check("2024-01-03", limit=10)
    ids = {r["repo_id"] for r in due}
    assert 1 in ids   # renamed: must stay in the queue
    assert 2 not in ids  # gone: drops out of the queue


def test_list_known_repos_filters_by_status(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "c/d", "c", "2024-01-02", "2024-01-16")
    c.mark_gone(2)
    active = c.list_known_repos(status="active")
    assert len(active) == 1
    assert active[0]["full_name"] == "a/b"


def test_list_known_repos_search(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "other/thing", "other", "2024-01-01", "2024-01-15")
    found = c.list_known_repos(search="acme")
    assert len(found) == 1
    assert found[0]["full_name"] == "acme/widget"


def test_list_known_repos_ordered_by_last_seen_desc(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "old/repo", "old", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "new/repo", "new", "2024-06-01", "2024-06-15")
    rows = c.list_known_repos()
    assert rows[0]["full_name"] == "new/repo"


def test_count_known_repos_respects_filters(tmp_path):
    c = _cache(tmp_path)
    for i in range(5):
        c.upsert_known_repo(i, f"owner{i}/repo", f"owner{i}", "2024-01-01", "2024-01-15")
    assert c.count_known_repos() == 5
    assert c.count_known_repos(search="owner1") == 1


def test_upsert_known_repos_bulk_matches_individual_upsert(tmp_path):
    """Performance regression: upsert_known_repo committed on every
    row (disk fsync) - one hour of GH Archive with tens of thousands of
    repos (~32,000 observed live) would turn into minutes instead of
    seconds. upsert_known_repos_bulk does the same work in a single
    transaction; it must produce identical results, not just be faster."""
    c = _cache(tmp_path)
    rows = [(i, f"owner{i}/repo", f"owner{i}", "2024-01-01", "2024-01-15") for i in range(50)]
    c.upsert_known_repos_bulk(rows)
    assert c.count_known_repos() == 50
    assert c.get_known_repo(7)["full_name"] == "owner7/repo"
    assert c.get_known_repo(7)["status"] == "active"


def test_upsert_known_repos_bulk_preserves_non_active_status(tmp_path):
    """Same semantics as upsert_known_repo: a repo already marked as
    disappeared must not go back to "active" just because it reappears in
    an ingest - explicit re-verification (verify_cycle) remains the only
    way to do that."""
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.mark_gone(1)
    c.upsert_known_repos_bulk([(1, "a/b", "a", "2024-02-01", "2024-02-15")])
    row = c.get_known_repo(1)
    assert row["status"] == "gone_or_private"
    assert row["next_check_due"] == "2024-01-15"  # unchanged, per the CASE WHEN


def test_upsert_known_repos_bulk_empty_list_is_noop(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repos_bulk([])
    assert c.count_known_repos() == 0


def test_clear_known_repos_empties_the_table_and_returns_the_count(tmp_path):
    """Cleanup mechanism for the leftovers of a test on a wide GH Archive
    range (verified live: 5.7 million rows from a single 28-day range) -
    no more need to hand-tinker with the SQLite client."""
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "c/d", "c", "2024-01-01", "2024-01-15")
    c.mark_gone(2)

    n = c.clear_known_repos()

    assert n == 2
    assert c.count_known_repos() == 0
    assert c.count_known_repos_by_status() == {}


def test_clear_known_repos_on_empty_table_returns_zero(tmp_path):
    c = _cache(tmp_path)
    assert c.clear_known_repos() == 0


def test_clear_known_repos_preserves_watched_rows(tmp_path):
    """Regression: 'Clear all' must not silently drop repos that the user
    explicitly added to the watchlist - only the passively discovered
    leftovers go away."""
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "c/d", "c", "2024-01-01", "2024-01-15")
    c.set_watched(2, True)

    n = c.clear_known_repos()

    assert n == 1
    assert c.count_known_repos() == 1
    assert c.get_known_repo(2)["full_name"] == "c/d"


def test_set_watched_and_list_watchlist(tmp_path):
    c = _cache(tmp_path)
    c.upsert_known_repo(1, "a/b", "a", "2024-01-01", "2024-01-15")
    c.upsert_known_repo(2, "c/d", "c", "2024-01-01", "2024-01-15")

    assert c.count_watchlist() == 0
    c.set_watched(1, True)
    assert c.count_watchlist() == 1
    assert [r["full_name"] for r in c.list_watchlist()] == ["a/b"]

    c.set_watched(1, False)
    assert c.count_watchlist() == 0
