from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from forkforensics.cache import CacheManager
from forkforensics.verify_cycle import _check_owner_status, run_verify_cycle


def _cache(tmp_path) -> CacheManager:
    return CacheManager(tmp_path / "t.db")


def test_check_owner_status_active():
    rest = MagicMock()
    rest.get_user.return_value = {"type": "User"}
    assert _check_owner_status(rest, "someuser/somerepo") == "owner_active"
    rest.get_user.assert_called_once_with("someuser")


def test_check_owner_status_gone():
    rest = MagicMock()
    rest.get_user.return_value = None
    assert _check_owner_status(rest, "someuser/somerepo") == "owner_gone"


def test_check_owner_status_unknown_on_error():
    rest = MagicMock()
    rest.get_user.side_effect = RuntimeError("boom")
    assert _check_owner_status(rest, "someuser/somerepo") == "unknown"


def test_run_verify_cycle_records_owner_gone_when_no_rest_client_is_unknown(tmp_path):
    """Without rest_client (backward compatibility), owner_status stays 'unknown'."""
    cache = _cache(tmp_path)
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-02")
    gql = MagicMock()
    gql.check_alive_batch.return_value = {1: None}

    stats = run_verify_cycle(cache, gql, rest_client=None, today=date(2024, 1, 3))
    assert stats["gone"] == 1
    rows = cache.list_disappearances()
    assert rows[0]["owner_status"] == "unknown"


def test_run_verify_cycle_records_owner_gone_status(tmp_path):
    cache = _cache(tmp_path)
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-02")
    gql = MagicMock()
    gql.check_alive_batch.return_value = {1: None}
    rest = MagicMock()
    rest.get_user.return_value = None  # the account is gone too

    stats = run_verify_cycle(cache, gql, rest_client=rest, today=date(2024, 1, 3))
    assert stats["gone"] == 1
    rows = cache.list_disappearances()
    assert rows[0]["owner_status"] == "owner_gone"


def test_run_verify_cycle_records_owner_active_status(tmp_path):
    cache = _cache(tmp_path)
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-02")
    gql = MagicMock()
    gql.check_alive_batch.return_value = {1: None}
    rest = MagicMock()
    rest.get_user.return_value = {"type": "User"}  # account still active

    run_verify_cycle(cache, gql, rest_client=rest, today=date(2024, 1, 3))
    rows = cache.list_disappearances()
    assert rows[0]["owner_status"] == "owner_active"


def test_run_verify_cycle_no_owner_check_for_alive_or_renamed_repos(tmp_path):
    """The owner check only makes sense for disappearances, it shouldn't
    be wasted on repos that are still alive or merely renamed."""
    cache = _cache(tmp_path)
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-02")
    cache.upsert_known_repo(2, "acme/other", "acme", "2024-01-01", "2024-01-02")
    gql = MagicMock()
    gql.check_alive_batch.return_value = {
        1: {"full_name": "acme/widget"},          # still alive, same name
        2: {"full_name": "acme/other-renamed"},   # renamed
    }
    rest = MagicMock()

    run_verify_cycle(cache, gql, rest_client=rest, today=date(2024, 1, 3))
    rest.get_user.assert_not_called()
