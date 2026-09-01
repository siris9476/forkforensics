from datetime import date

import pytest

from forkforensics.archive_index import EARLIEST_DATE, build_hour_list, estimate_range


def test_build_hour_list_one_day_has_24_hours():
    hours = build_hour_list(date(2023, 1, 1), date(2023, 1, 1))
    assert len(hours) == 24
    assert hours[0] == ("2023-01-01", 0)
    assert hours[-1] == ("2023-01-01", 23)


def test_build_hour_list_multi_day():
    hours = build_hour_list(date(2023, 1, 1), date(2023, 1, 2))
    assert len(hours) == 48


def test_build_hour_list_rejects_before_earliest():
    with pytest.raises(ValueError):
        build_hour_list(date(2000, 1, 1), date(2000, 1, 2))


def test_build_hour_list_rejects_inverted_range():
    with pytest.raises(ValueError):
        build_hour_list(date(2023, 1, 2), date(2023, 1, 1))


def test_estimate_range_no_network(tmp_path, monkeypatch):
    """Verifies only the estimation math, without a real network."""
    import requests

    class FakeResp:
        status_code = 200
        headers = {"Content-Length": "100000000"}  # 100 MB

    class FakeSession:
        def head(self, *a, **k):
            return FakeResp()

    hours = build_hour_list(date(2023, 1, 1), date(2023, 1, 1))
    result = estimate_range(hours, tmp_path, sample_size=3, session=FakeSession())
    assert result.n_hours == 24
    assert result.estimated_total_bytes == 24 * 100_000_000

    # disk_ok depends on the ACTUAL free space of the machine running the
    # test (it's not guaranteed to always be more than 2.4GB*1.2 - a CI
    # environment with a small disk made this flaky) - we verify the real
    # invariant (consistency with the free space measured NOW), not a
    # fixed value.
    import shutil
    free = shutil.disk_usage(tmp_path).free
    assert result.disk_ok == (free > result.estimated_total_bytes * 1.2)
