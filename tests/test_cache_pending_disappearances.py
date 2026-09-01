"""list_pending_disappearances filters in SQL rather than taking the most
recent N rows and checking their status in Python - the daily cycle used to
do exactly that (list_disappearances(limit=5000) then filter for pending),
so once total disappearances passed 5000, an older row stuck pending fell
outside that window and could never be picked up again."""

from forkforensics.cache import CacheManager


def _seed(cache, disappearance_id_count, pending_ids):
    """Creates `disappearance_id_count` disappearances, most already
    resolved ('done'), except the ids in `pending_ids` which stay
    'pending'. Detected dates are assigned so the pending ones are among
    the OLDEST - i.e. exactly the ones a recency-limited query would drop
    first."""
    for i in range(1, disappearance_id_count + 1):
        cache.upsert_known_repo(i, f"acme/repo{i}", "acme", "2024-01-01", "2024-01-01")
        did = cache.record_disappearance(
            repo_id=i, full_name=f"acme/repo{i}",
            detected_date=f"2024-01-{(i % 28) + 1:02d}",
            last_known_alive="2023-12-01",
        )
        if did not in pending_ids:
            cache.update_investigation(did, "done", "resolved")


def test_an_old_pending_row_is_not_starved_by_a_recency_limited_query(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    # row 1 (the oldest disappearance_id) stays pending; everything after
    # it (rows 2..50) is already resolved
    _seed(cache, disappearance_id_count=50, pending_ids={1})

    # a small limit simulates "more disappearances exist than the query
    # window" without actually creating 5000+ rows in a test
    pending = cache.list_pending_disappearances(limit=10)

    assert [row["disappearance_id"] for row in pending] == [1]
    cache.close()


def test_pending_rows_are_returned_oldest_first(tmp_path):
    """Oldest first, so a genuinely stuck row takes priority over one just
    added this cycle - not an incidental ordering."""
    cache = CacheManager(tmp_path / "t.db")
    _seed(cache, disappearance_id_count=5, pending_ids={1, 3, 5})

    pending = cache.list_pending_disappearances(limit=10)

    assert [row["disappearance_id"] for row in pending] == [1, 3, 5]
    cache.close()


def test_respects_the_limit(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    _seed(cache, disappearance_id_count=5, pending_ids={1, 2, 3, 4, 5})

    pending = cache.list_pending_disappearances(limit=2)

    assert len(pending) == 2
    assert [row["disappearance_id"] for row in pending] == [1, 2]
    cache.close()


def test_no_pending_rows_returns_empty(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    _seed(cache, disappearance_id_count=3, pending_ids=set())

    assert cache.list_pending_disappearances() == []
    cache.close()
