import asyncio
from datetime import date

import forkforensics.daily_cycle as dc
from forkforensics.cache import CacheManager
from forkforensics.errors import GitHubAPIError


def _cache_with_pending(tmp_path, n: int) -> CacheManager:
    cache = CacheManager(tmp_path / "t.db")
    for i in range(n):
        cache.upsert_known_repo(i, f"owner{i}/repo", f"owner{i}", "2024-01-01", "2024-01-02")
        cache.record_disappearance(repo_id=i, full_name=f"owner{i}/repo",
                                   detected_date="2024-01-03", last_known_alive="2024-01-01")
    return cache


def _run(cache, monkeypatch, investigate_side_effect):
    monkeypatch.setattr(dc, "ingest_day", _noop_ingest_day)
    monkeypatch.setattr(dc, "run_verify_cycle",
                        lambda *a, **k: {"checked": 0, "alive": 0, "renamed": 0, "gone": 0})
    monkeypatch.setattr(dc, "investigate", investigate_side_effect)
    return asyncio.run(dc.run_daily_cycle(
        cache, tmp_path_raw, "fake-token",
        ingest_date=date(2024, 1, 1), ingest_date_to=date(2024, 1, 1),
    ))


async def _noop_ingest_day(*a, **k):
    return 0


tmp_path_raw = None  # set by each test via tmp_path


def test_stops_and_reports_on_persistent_api_error(tmp_path, monkeypatch):
    """Regression: a persistent API error (rate limit exhausted) must
    not be treated as the failure of the single repo currently being
    processed - it must halt the batch, put the row back to 'pending'
    (it will be resumed) instead of marking it 'error' forever, and say
    so clearly in the result instead of continuing to hammer the API on
    all the remaining ones."""
    global tmp_path_raw
    tmp_path_raw = tmp_path / "raw"
    cache = _cache_with_pending(tmp_path, 5)

    calls = []

    def fake_investigate(rest_client, cache_, owner, repo, raw_dir):
        calls.append(f"{owner}/{repo}")
        if len(calls) == 2:
            raise GitHubAPIError("persistent rate limit/5xx on https://api.github.com/x")
        return {"fork_ranking": [], "at_risk_shas": []}

    result = _run(cache, monkeypatch, fake_investigate)

    assert len(calls) == 2  # stopped at the second one, did not try the third/fourth/fifth
    assert result["api_error"]
    assert result["auto_investigated"] == 1  # only the first one succeeded
    assert result["investigate_remaining"] == 4

    rows = {r["full_name"]: r["investigation_status"] for r in cache.list_disappearances(limit=10)}
    assert rows[calls[1]] == "pending"  # the one hit by the API error goes back to the queue, not 'error'


def test_per_repo_failure_does_not_halt_the_batch(tmp_path, monkeypatch):
    """A normal failure (e.g. repo not found, ValueError) affects only
    that repo - the batch must continue with the following ones."""
    global tmp_path_raw
    tmp_path_raw = tmp_path / "raw"
    cache = _cache_with_pending(tmp_path, 3)

    def fake_investigate(rest_client, cache_, owner, repo, raw_dir):
        if repo == "repo" and owner == "owner1":
            raise ValueError(f"{owner}/{repo} not found")
        return {"fork_ranking": [], "at_risk_shas": []}

    result = _run(cache, monkeypatch, fake_investigate)

    assert result["auto_investigated"] == 3  # all processed, no interruption
    assert "api_error" not in result
    rows = {r["full_name"]: r["investigation_status"] for r in cache.list_disappearances(limit=10)}
    assert rows["owner1/repo"] == "error"
    assert rows["owner0/repo"] == "done"
    assert rows["owner2/repo"] == "done"


def test_processes_more_than_fifty_pending(tmp_path, monkeypatch):
    """Regression: the fixed limit of 50 prevented processing a larger
    batch of disappearances in a single cycle (e.g. discovered by a
    search over a wide date range)."""
    global tmp_path_raw
    tmp_path_raw = tmp_path / "raw"
    cache = _cache_with_pending(tmp_path, 60)

    result = _run(cache, monkeypatch, lambda *a, **k: {"fork_ranking": [], "at_risk_shas": []})

    assert result["auto_investigated"] == 60


def test_accepts_a_list_of_tokens_for_rotation(tmp_path, monkeypatch):
    """token can also be a list of multiple tokens (rotation on
    GitHubClient) instead of a single string - normalized internally,
    backward-compatible with the single token from before."""
    global tmp_path_raw
    tmp_path_raw = tmp_path / "raw"
    monkeypatch.setattr(dc, "ingest_day", _noop_ingest_day)
    monkeypatch.setattr(dc, "run_verify_cycle",
                        lambda *a, **k: {"checked": 0, "alive": 0, "renamed": 0, "gone": 0})
    monkeypatch.setattr(dc, "investigate", lambda *a, **k: {"fork_ranking": [], "at_risk_shas": []})
    cache = _cache_with_pending(tmp_path, 2)

    result = asyncio.run(dc.run_daily_cycle(
        cache, tmp_path_raw, ["tok-a", "tok-b"],
        ingest_date=date(2024, 1, 1), ingest_date_to=date(2024, 1, 1),
    ))

    assert result["auto_investigated"] == 2
