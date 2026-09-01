import sqlite3

from forkforensics.cache import CacheManager


def test_reopening_old_schema_adds_missing_columns(tmp_path):
    """A database created with a PREVIOUS schema (without the columns
    added in later migrations) must be able to reopen without errors,
    with the missing columns added automatically."""
    db_path = tmp_path / "old.db"

    # simulates a database created with the original schema, before the
    # git_fetch_alive/owner_status columns
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE probe_results (
            sha TEXT PRIMARY KEY, owner TEXT, repo TEXT,
            reachable INTEGER, api_alive INTEGER, raw_alive INTEGER,
            checked_at TEXT
        );
        CREATE TABLE disappearances (
            disappearance_id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL, full_name TEXT NOT NULL,
            detected_date TEXT NOT NULL, last_known_alive TEXT,
            investigation_status TEXT NOT NULL DEFAULT 'pending',
            recoverable_summary TEXT
        );
    """)
    conn.commit()
    conn.close()

    # reopens with the current code - must not raise exceptions
    cache = CacheManager(db_path)
    disap_id = cache.record_disappearance(
        repo_id=1, full_name="a/b", detected_date="2024-01-01",
        last_known_alive="2023-12-01", owner_status="owner_gone",
    )
    row = cache.get_disappearance(disap_id)
    assert row["owner_status"] == "owner_gone"

    cache.record_probe("sha1", "a", "b", reachable=False, api_alive=True,
                       raw_alive=False, git_fetch_alive=True, checked_at="now",
                       parent_count=0)
    probe = cache.get_probe("sha1", "a", "b")
    assert probe["git_fetch_alive"] == 1
    assert probe["parent_count"] == 0
    cache.close()


def test_old_sha_only_primary_key_is_rebuilt_as_composite(tmp_path):
    """Regression: probe_results used to be keyed by sha ALONE. Forks share
    identical SHAs by construction, so probing the same commit in a fork
    overwrote the verdict recorded for the vanished repo - and "orphan but
    alive" is precisely the field that differs between them. Reopening an
    old database must rebuild the table with a (sha, owner, repo) key,
    preserving the existing rows."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE probe_results (
            sha TEXT PRIMARY KEY, owner TEXT, repo TEXT,
            reachable INTEGER, api_alive INTEGER, raw_alive INTEGER,
            checked_at TEXT
        );
        INSERT INTO probe_results(sha,owner,repo,reachable,api_alive,raw_alive,checked_at)
        VALUES ('deadbeef','victim','data',0,1,1,'then');
    """)
    conn.commit()
    conn.close()

    cache = CacheManager(db_path)
    pk = {r["name"] for r in cache._conn.execute("PRAGMA table_info(probe_results)") if r["pk"]}
    assert pk == {"sha", "owner", "repo"}

    # the pre-existing row survived the rebuild
    assert cache.get_probe("deadbeef", "victim", "data")["api_alive"] == 1

    # and the same sha in a DIFFERENT repo is now a distinct row rather
    # than an overwrite of the one above
    cache.record_probe("deadbeef", "forker", "data", reachable=True, api_alive=True,
                       raw_alive=True, git_fetch_alive=True, checked_at="now")
    assert cache.get_probe("deadbeef", "victim", "data")["reachable"] == 0
    assert cache.get_probe("deadbeef", "forker", "data")["reachable"] == 1
    cache.close()


def test_migration_is_idempotent(tmp_path):
    """Reopening the same database multiple times must not fail (the
    column is already present the second time) AND must leave the schema
    identical - a structural migration that re-ran would silently drop
    rows every time the app starts."""
    db_path = tmp_path / "t.db"
    first = CacheManager(db_path)
    first.record_probe("s", "o", "r", reachable=True, api_alive=True, raw_alive=True,
                       git_fetch_alive=True, checked_at="now")
    cols_before = [tuple(r) for r in first._conn.execute("PRAGMA table_info(probe_results)")]
    first.close()

    second = CacheManager(db_path)  # second opening, must not raise exceptions
    cols_after = [tuple(r) for r in second._conn.execute("PRAGMA table_info(probe_results)")]
    assert cols_after == cols_before
    assert second.get_probe("s", "o", "r") is not None  # row not lost on reopen
    second.close()
