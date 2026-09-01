"""SQLite persistence: known repos, disappearances, GH Archive/probe cache,
investigation jobs. A single connection per process, thread-safe via
check_same_thread=False + an explicit lock on writes (SQLite serializes
anyway, but this avoids interleaving of multiple statements)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS known_repos (
    repo_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    owner TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen_active TEXT NOT NULL,
    last_verified_alive TEXT,
    next_check_due TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_known_repos_next_check ON known_repos(next_check_due);
CREATE INDEX IF NOT EXISTS idx_known_repos_full_name ON known_repos(full_name);
CREATE INDEX IF NOT EXISTS idx_known_repos_status ON known_repos(status);
CREATE INDEX IF NOT EXISTS idx_known_repos_last_seen_active ON known_repos(last_seen_active);

CREATE TABLE IF NOT EXISTS disappearances (
    disappearance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    full_name TEXT NOT NULL,
    detected_date TEXT NOT NULL,
    last_known_alive TEXT,
    investigation_status TEXT NOT NULL DEFAULT 'pending',
    recoverable_summary TEXT,
    owner_status TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_disappearances_date ON disappearances(detected_date);

CREATE TABLE IF NOT EXISTS archive_hours (
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    status TEXT NOT NULL,          -- downloaded | processed | error
    size_bytes INTEGER,
    event_count INTEGER,
    processed_at TEXT,
    PRIMARY KEY (date, hour)
);

-- Keyed by (sha, owner, repo), NOT by sha alone: forks share identical
-- SHAs by construction, which is the very premise of this tool. With sha
-- as the sole key, a probe of the same commit in a fork overwrote (and was
-- served in place of) the probe in the vanished repo - and "orphan but
-- still alive" is exactly the verdict that differs between the two.
CREATE TABLE IF NOT EXISTS probe_results (
    sha TEXT NOT NULL,
    owner TEXT NOT NULL, repo TEXT NOT NULL,
    reachable INTEGER, api_alive INTEGER, raw_alive INTEGER,
    git_fetch_alive INTEGER DEFAULT 0,
    checked_at TEXT,
    PRIMARY KEY (sha, owner, repo)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    owner TEXT, repo TEXT, date_from TEXT, date_to TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    current_phase TEXT, phase_detail TEXT, progress_pct REAL DEFAULT 0.0,
    error TEXT,
    result_json TEXT,
    created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# Additive migrations: (table, column, DDL definition) for columns
# introduced AFTER the first version of the schema. "CREATE TABLE IF NOT
# EXISTS" does not add columns to a table that already exists from a
# database created with an earlier schema - without this, reopening an old
# database.db with a newer version of the code would fail with
# "no column named X" instead of upgrading itself automatically.
_MIGRATIONS = [
    ("probe_results", "git_fetch_alive", "INTEGER DEFAULT 0"),
    ("disappearances", "owner_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("probe_results", "commit_message", "TEXT"),
    ("probe_results", "commit_author", "TEXT"),
    ("probe_results", "commit_date", "TEXT"),
    ("probe_results", "files_changed", "INTEGER"),
    ("probe_results", "parent_count", "INTEGER"),
    ("probe_results", "changed_files", "TEXT"),
    ("known_repos", "watched", "INTEGER NOT NULL DEFAULT 0"),
]


def _restrict_permissions(db_path: Path) -> None:
    """This database holds the GitHub token in plaintext (settings table).
    SQLite creates it 0644 by default, so on a shared Linux/macOS box any
    local account could read it. Windows relies on the per-user ACL of
    %APPDATA% instead and has no meaningful chmod, so it's skipped there.
    Best-effort: a filesystem that doesn't support it must not stop the app
    from starting."""
    if os.name == "nt":
        return
    try:
        os.chmod(db_path, 0o600)
        os.chmod(db_path.parent, 0o700)
    except OSError as exc:  # noqa: BLE001 - never fatal
        logger.warning("could not restrict permissions on %s: %r", db_path, exc)


class CacheManager:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        _restrict_permissions(db_path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._run_migrations()
            self._migrate_probe_results_key()

    def _run_migrations(self) -> None:
        for table, column, ddl in _MIGRATIONS:
            existing = {row["name"] for row in
                       self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._conn.commit()

    def _migrate_probe_results_key(self) -> None:
        """Structural migration: probe_results used to be keyed by sha
        alone. SQLite cannot ALTER a primary key, so the table is rebuilt
        in place. Runs only once - afterwards the pk set already matches
        and this is a cheap PRAGMA check. Rows lacking owner/repo are
        dropped: they cannot be attributed to a repository, and
        probe_results is a cache that refills itself on the next probe."""
        cols = list(self._conn.execute("PRAGMA table_info(probe_results)"))
        pk_cols = {row["name"] for row in cols if row["pk"]}
        if pk_cols == {"sha", "owner", "repo"}:
            return
        names = [row["name"] for row in cols]
        col_list = ", ".join(names)
        defs = ", ".join(
            f"{row['name']} TEXT NOT NULL" if row["name"] in ("sha", "owner", "repo")
            else f"{row['name']} {row['type'] or 'TEXT'}"
            for row in cols
        )
        self._conn.executescript(
            f"CREATE TABLE probe_results_new ({defs}, PRIMARY KEY (sha, owner, repo));"
            f"INSERT OR REPLACE INTO probe_results_new ({col_list}) "
            f"SELECT {col_list} FROM probe_results "
            f"WHERE owner IS NOT NULL AND repo IS NOT NULL;"
            "DROP TABLE probe_results;"
            "ALTER TABLE probe_results_new RENAME TO probe_results;"
        )
        self._conn.commit()
        logger.info("migrated probe_results to a (sha, owner, repo) primary key")

    # ------------------------------------------------------------ archive_hours

    def is_hour_processed(self, date: str, hour: int) -> bool:
        row = self._conn.execute(
            "SELECT status FROM archive_hours WHERE date=? AND hour=?", (date, hour),
        ).fetchone()
        return row is not None and row["status"] == "processed"

    def mark_hour_downloaded(self, date: str, hour: int, size_bytes: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO archive_hours(date,hour,status,size_bytes) VALUES (?,?,?,?) "
                "ON CONFLICT(date,hour) DO UPDATE SET status='downloaded', size_bytes=excluded.size_bytes",
                (date, hour, "downloaded", size_bytes),
            )
            self._conn.commit()

    def mark_hour_processed(self, date: str, hour: int, event_count: int, processed_at: str) -> None:
        # Must be an upsert, not a bare UPDATE: fetch_hour() returns early
        # (without calling mark_hour_downloaded) when the .json.gz is
        # already on disk, so for those hours no archive_hours row exists
        # and an UPDATE silently matched nothing. is_hour_processed() then
        # never returned True and every run re-parsed the same ~30k-row
        # hours from scratch - the whole resume mechanism was dead.
        with self._lock:
            self._conn.execute(
                "INSERT INTO archive_hours(date,hour,status,event_count,processed_at) "
                "VALUES (?,?,'processed',?,?) "
                "ON CONFLICT(date,hour) DO UPDATE SET status='processed', "
                "event_count=excluded.event_count, processed_at=excluded.processed_at",
                (date, hour, event_count, processed_at),
            )
            self._conn.commit()

    # ------------------------------------------------------------ known_repos

    _UPSERT_KNOWN_REPO_SQL = """
        INSERT INTO known_repos(repo_id, full_name, owner, first_seen,
               last_seen_active, next_check_due, status)
           VALUES (?,?,?,?,?,?, 'active')
           ON CONFLICT(repo_id) DO UPDATE SET
               full_name=excluded.full_name,
               last_seen_active=excluded.last_seen_active,
               next_check_due=CASE WHEN known_repos.status='active'
                   THEN excluded.next_check_due ELSE known_repos.next_check_due END"""

    def upsert_known_repo(self, repo_id: int, full_name: str, owner: str,
                          seen_at: str, next_check_due: str) -> None:
        with self._lock:
            self._conn.execute(
                self._UPSERT_KNOWN_REPO_SQL,
                (repo_id, full_name, owner, seen_at, seen_at, next_check_due),
            )
            self._conn.commit()

    def upsert_known_repos_bulk(self, rows: list[tuple[int, str, str, str, str]]) -> None:
        """Like upsert_known_repo but for many rows in a SINGLE transaction.
        One hour of GH Archive can contain tens of thousands of repos seen
        (~32,000 observed live) - a commit (disk fsync) for every single
        row would turn that number into minutes instead of seconds."""
        if not rows:
            return
        params = [(repo_id, full_name, owner, seen_at, seen_at, next_check_due)
                 for repo_id, full_name, owner, seen_at, next_check_due in rows]
        with self._lock:
            self._conn.executemany(self._UPSERT_KNOWN_REPO_SQL, params)
            self._conn.commit()

    def due_for_check(self, today: str, limit: int) -> list[sqlite3.Row]:
        # 'renamed' stays in the re-verification queue: a renamed repo can
        # still disappear later, it is not a terminal state
        return self._conn.execute(
            "SELECT * FROM known_repos WHERE status IN ('active','renamed') "
            "AND next_check_due<=? ORDER BY next_check_due LIMIT ?",
            (today, limit),
        ).fetchall()

    def mark_verified_alive(self, repo_id: int, full_name: str, verified_at: str,
                            next_check_due: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE known_repos SET status='active', full_name=?, "
                "last_verified_alive=?, next_check_due=? WHERE repo_id=?",
                (full_name, verified_at, next_check_due, repo_id),
            )
            self._conn.commit()

    def mark_renamed(self, repo_id: int, new_full_name: str, verified_at: str,
                     next_check_due: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE known_repos SET status='renamed', full_name=?, "
                "last_verified_alive=?, next_check_due=? WHERE repo_id=?",
                (new_full_name, verified_at, next_check_due, repo_id),
            )
            self._conn.commit()

    def mark_gone(self, repo_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE known_repos SET status='gone_or_private' WHERE repo_id=?",
                (repo_id,),
            )
            self._conn.commit()

    def get_known_repo(self, repo_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM known_repos WHERE repo_id=?", (repo_id,),
        ).fetchone()

    def count_known_repos_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM known_repos GROUP BY status",
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def list_known_repos(self, status: str | None = None, search: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        query = "SELECT * FROM known_repos WHERE 1=1"
        params: list = []
        if status:
            query += " AND status=?"
            params.append(status)
        if search:
            query += " AND full_name LIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY last_seen_active DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._conn.execute(query, params).fetchall()

    def count_known_repos(self, status: str | None = None, search: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM known_repos WHERE 1=1"
        params: list = []
        if status:
            query += " AND status=?"
            params.append(status)
        if search:
            query += " AND full_name LIKE ?"
            params.append(f"%{search}%")
        return self._conn.execute(query, params).fetchone()["n"]

    def clear_known_repos(self) -> int:
        """Clears the PASSIVELY DISCOVERED monitored population - useful
        after a test on a wide GH Archive range (verified live: a single
        28-day range left 5.7 million rows, almost all repos seen only
        once and never relevant again). Not destructive for normal use:
        the daily cycle repopulates the table on its own on every future
        ingest, for free, from GH Archive. Preserves repos explicitly
        added to the watchlist (watched=1) - the user chose those on
        purpose, wiping everything would lose them without the user
        noticing. Returns the number of rows deleted."""
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM known_repos WHERE watched=0").fetchone()["n"]
            self._conn.execute("DELETE FROM known_repos WHERE watched=0")
            self._conn.commit()
            return n

    # ------------------------------------------------------------ watchlist

    def set_watched(self, repo_id: int, watched: bool) -> None:
        """The watchlist is just a flag on known_repos, not a parallel
        table: a repo with watched=1 automatically enters the same
        re-verification cycle (due_for_check/run_verify_cycle) as any
        other known repo - no extra logic is needed in the daily cycle,
        and it keeps being rechecked forever even if it never reappears
        in a GH Archive event again (unlike passive discovery, which
        depends on reappearing in a public event)."""
        with self._lock:
            self._conn.execute(
                "UPDATE known_repos SET watched=? WHERE repo_id=?", (1 if watched else 0, repo_id),
            )
            self._conn.commit()

    def reactivate_for_checking(self, repo_id: int) -> None:
        """Puts a repo back into the re-verification queue.

        due_for_check only selects status IN ('active','renamed'), and the
        known_repos upsert deliberately never resets status - so a repo
        marked 'gone_or_private' stays excluded forever. That is right for
        passive discovery, but wrong when the user explicitly asks to watch
        a repository that has already vanished (a repo CAN come back: a
        private one can be made public again)."""
        with self._lock:
            self._conn.execute(
                "UPDATE known_repos SET status='active' WHERE repo_id=? "
                "AND status='gone_or_private'", (repo_id,),
            )
            self._conn.commit()

    def list_watchlist(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM known_repos WHERE watched=1 ORDER BY full_name LIMIT ?", (limit,),
        ).fetchall()

    def count_watchlist(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM known_repos WHERE watched=1").fetchone()["n"]

    # ------------------------------------------------------------ disappearances

    def record_disappearance(self, repo_id: int, full_name: str, detected_date: str,
                             last_known_alive: str | None,
                             owner_status: str = "unknown") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO disappearances(repo_id, full_name, detected_date, "
                "last_known_alive, investigation_status, owner_status) "
                "VALUES (?,?,?,?,'pending',?)",
                (repo_id, full_name, detected_date, last_known_alive, owner_status),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_investigation(self, disappearance_id: int, status: str,
                             summary: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE disappearances SET investigation_status=?, recoverable_summary=? "
                "WHERE disappearance_id=?",
                (status, summary, disappearance_id),
            )
            self._conn.commit()

    def list_disappearances(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM disappearances ORDER BY detected_date DESC, disappearance_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def list_pending_disappearances(self, limit: int = 5000) -> list[sqlite3.Row]:
        """Filtered in SQL, not by taking the most-recent N and checking
        their status in Python: the daily cycle used to do
        `[r for r in list_disappearances(limit=5000) if r.status=='pending']`,
        which only ever looked at the 5000 most recently DETECTED rows. Once
        total disappearances passed 5000, an old one stuck pending (from a
        cancelled run or an API halt) fell outside that window and could
        never be picked up again - a silent starvation with no error and no
        log line. Oldest first, so a genuinely stuck row gets priority over
        one just added this cycle."""
        return self._conn.execute(
            "SELECT * FROM disappearances WHERE investigation_status='pending' "
            "ORDER BY disappearance_id ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def get_disappearance(self, disappearance_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM disappearances WHERE disappearance_id=?", (disappearance_id,),
        ).fetchone()

    # ------------------------------------------------------------ probe_results

    def record_probe(self, sha: str, owner: str, repo: str, reachable: bool,
                     api_alive: bool, raw_alive: bool, git_fetch_alive: bool,
                     checked_at: str, commit_message: str | None = None,
                     commit_author: str | None = None, commit_date: str | None = None,
                     files_changed: int | None = None, parent_count: int | None = None,
                     changed_files: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO probe_results(sha,owner,repo,reachable,api_alive,raw_alive,"
                "git_fetch_alive,checked_at,commit_message,commit_author,commit_date,"
                "files_changed,parent_count,changed_files) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sha,owner,repo) DO UPDATE SET "
                "reachable=excluded.reachable, api_alive=excluded.api_alive, "
                "raw_alive=excluded.raw_alive, git_fetch_alive=excluded.git_fetch_alive, "
                "checked_at=excluded.checked_at, commit_message=excluded.commit_message, "
                "commit_author=excluded.commit_author, commit_date=excluded.commit_date, "
                "files_changed=excluded.files_changed, parent_count=excluded.parent_count, "
                "changed_files=excluded.changed_files",
                (sha, owner, repo, int(reachable), int(api_alive), int(raw_alive),
                 int(git_fetch_alive), checked_at, commit_message, commit_author,
                 commit_date, files_changed, parent_count, changed_files),
            )
            self._conn.commit()

    def get_probe(self, sha: str, owner: str, repo: str) -> sqlite3.Row | None:
        """owner/repo are part of the key on purpose - the same SHA lives in
        every fork of a repository, and its survival verdict is not the
        same in each of them."""
        return self._conn.execute(
            "SELECT * FROM probe_results WHERE sha=? AND owner=? AND repo=?",
            (sha, owner, repo),
        ).fetchone()

    # ------------------------------------------------------------ jobs

    def create_job(self, job_id: str, owner: str, repo: str, date_from: str,
                   date_to: str, created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id,owner,repo,date_from,date_to,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (job_id, owner, repo, date_from, date_to, created_at, created_at),
            )
            self._conn.commit()

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE job_id=?",
                (*fields.values(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def list_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            self._conn.commit()

    def clear_jobs(self) -> None:
        """Clears the entire history, including any rows left stuck in
        'queued'/'running' (e.g. after the app was force-closed during a
        long-running operation)."""
        with self._lock:
            self._conn.execute("DELETE FROM jobs")
            self._conn.commit()

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
