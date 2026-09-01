from forkforensics.cache import CacheManager


def _cache(tmp_path) -> CacheManager:
    return CacheManager(tmp_path / "t.db")


def test_delete_job_removes_only_that_row(tmp_path):
    c = _cache(tmp_path)
    c.create_job("a", "owner", "repo1", "", "", "2024-01-01T00:00:00Z")
    c.create_job("b", "owner", "repo2", "", "", "2024-01-02T00:00:00Z")
    c.delete_job("a")
    remaining = c.list_jobs()
    assert [r["job_id"] for r in remaining] == ["b"]


def test_clear_jobs_removes_everything_including_stuck_queued(tmp_path):
    """History must be clearable even when it contains rows left stuck in
    'queued' (e.g. the app was force-closed during a long-running
    operation, never reaching update_job)."""
    c = _cache(tmp_path)
    c.create_job("a", "owner", "repo1", "", "", "2024-01-01T00:00:00Z")
    c.create_job("b", "(interval)", "2024-01-01..2024-06-01", "2024-01-01", "2024-06-01",
                "2024-01-02T00:00:00Z")
    c.update_job("a", status="done")
    # "b" stays 'queued' on purpose - simulates a job that never finished

    assert len(c.list_jobs()) == 2
    c.clear_jobs()
    assert c.list_jobs() == []
