import shutil
from datetime import date
from pathlib import Path

from forkforensics.archive_ingest import ingest_day
from forkforensics.cache import CacheManager

FIXTURES = Path(__file__).parent / "fixtures"


async def _fake_fetch_range(hour_list, raw_dir, cache, concurrency=4):
    date_str, hour = hour_list[0]
    dest = raw_dir / f"{date_str}-{hour}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "sample_hour_2023.json.gz", dest)
    yield dest


def test_ingest_day_records_known_repos_with_correct_owner(tmp_path, monkeypatch):
    """Regression: iter_all_repo_sightings returns (repo_id, full_name)
    - confirmed by test_archive_filter.py, which indexes the result by
    repo_id - but ingest_day unpacked them in reverse (`for full_name,
    repo_id in sightings`), so full_name.split('/') failed with
    AttributeError because full_name was actually an int (the repo_id).
    Never caught before because ingest_day had no test of its own, only
    iter_all_repo_sightings in isolation."""
    import forkforensics.archive_ingest as ai
    monkeypatch.setattr(ai, "fetch_range", _fake_fetch_range)
    monkeypatch.setattr(ai, "build_hour_list", lambda d1, d2: [(d1.isoformat(), 12)])

    cache = CacheManager(tmp_path / "t.db")
    n_seen = ai.asyncio.run(
        ingest_day(date(2024, 6, 1), tmp_path / "raw", cache, keep_raw=False))

    assert n_seen == 2
    row = cache.get_known_repo(42)
    assert row["full_name"] == "acme/widget"
    assert row["owner"] == "acme"
    other = cache.get_known_repo(99)
    assert other["full_name"] == "other/repo"
    assert other["owner"] == "other"


def test_ingest_day_deletes_raw_file_when_not_keeping(tmp_path, monkeypatch):
    import forkforensics.archive_ingest as ai
    monkeypatch.setattr(ai, "fetch_range", _fake_fetch_range)
    monkeypatch.setattr(ai, "build_hour_list", lambda d1, d2: [(d1.isoformat(), 12)])

    cache = CacheManager(tmp_path / "t.db")
    raw_dir = tmp_path / "raw"
    ai.asyncio.run(ingest_day(date(2024, 6, 1), raw_dir, cache, keep_raw=False))

    assert not (raw_dir / "2024-06-01-12.json.gz").exists()
