from unittest.mock import MagicMock, patch

from forkforensics.archive_fetch import fetch_range_threaded
from forkforensics.cache import CacheManager


def _mock_response(status_code=200, chunks=(b"hello ", b"world")):
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_content.return_value = iter(chunks)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_downloads_when_file_absent(tmp_path):
    """The file name is deterministic (date+hour): existence on disk is
    the source of truth, no separate flag in the database to keep in
    sync (which moreover doesn't track the file name - only
    date/hour/status)."""
    cache = CacheManager(tmp_path / "t.db")
    raw_dir = tmp_path / "raw"

    resp = _mock_response()
    with patch("forkforensics.archive_fetch.requests.get", return_value=resp) as mock_get:
        results = list(fetch_range_threaded([("2024-06-01", 5)], raw_dir, cache, concurrency=1))

    mock_get.assert_called_once()
    assert len(results) == 1
    assert results[0].read_bytes() == b"hello world"


def test_skips_redownload_when_file_already_present(tmp_path):
    """Regression: both the date-range ingest and the investigation of a
    specific repo delete the .json.gz after processing it - an old check
    based only on the 'downloaded' flag in the database stayed true even
    with the file absent, returning a ghost path (downstream crash)
    instead of re-downloading it. Here we verify the normal case (file
    genuinely present): no network request."""
    cache = CacheManager(tmp_path / "t.db")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    dest = raw_dir / "2024-06-01-5.json.gz"
    dest.write_bytes(b"already here")

    with patch("forkforensics.archive_fetch.requests.get") as mock_get:
        results = list(fetch_range_threaded([("2024-06-01", 5)], raw_dir, cache, concurrency=1))

    mock_get.assert_not_called()
    assert results == [dest]
