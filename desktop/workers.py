"""QThread wrappers around functions already existing in forkforensics/
(never rewritten) - keep the UI responsive during network/subprocess
work. Same principle as app/jobs.py used to be (progress_cb -> status
update), here the mechanism is a Qt signal instead of a SQLite write
consumed via HTTP polling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from forkforensics.cache import CacheManager
from forkforensics.daily_cycle import run_daily_cycle
from forkforensics.github_rest import GitHubClient
from forkforensics.investigate import investigate
from forkforensics.rescue import RescueError, clone_fork_locally, rescue_sha_locally
from forkforensics.survival_probe import check_alive

logger = logging.getLogger(__name__)


def user_message(exc: Exception) -> str:
    """A message fit for a dialog, with the full traceback sent to the log
    instead.

    These strings used to be f"{exc}\\n{traceback.format_exc()}", which
    landed verbatim in QMessageBox (unbounded height, unactionable) and was
    persisted into the jobs table. Callers also did splitlines()[0] on
    them, so an exception whose str() is empty rendered as a blank error.
    The type name is included as a fallback for exactly that case."""
    logger.exception("worker failed")
    text = str(exc).strip()
    return text or f"{type(exc).__name__} (see the log for details)"


class InvestigateWorker(QThread):
    progress = Signal(str, str, float)  # phase, detail, pct
    finished_ok = Signal(dict)          # report
    failed = Signal(str)                # error message

    def __init__(self, cache: CacheManager, tokens: list[str] | None, owner: str, repo: str,
                raw_dir: Path, date_from: date | None, date_to: date | None, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.tokens = tokens
        self.owner = owner
        self.repo = repo
        self.raw_dir = raw_dir
        self.date_from = date_from
        self.date_to = date_to

    def run(self) -> None:
        try:
            client = GitHubClient(tokens=self.tokens)

            def _cb(phase: str, detail: str, pct: float) -> None:
                self.progress.emit(phase, detail, pct)

            report = investigate(
                client, self.cache, self.owner, self.repo, self.raw_dir,
                self.date_from, self.date_to, 2, 200, _cb,
                cancel_cb=self.isInterruptionRequested,
            )
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class DailyCycleWorker(QThread):
    progress = Signal(str, str, float)  # phase, detail, pct
    finished_ok = Signal(dict)          # cycle statistics
    failed = Signal(str)

    def __init__(self, cache: CacheManager, raw_dir: Path, tokens: list[str] | None,
                date_from: date | None = None, date_to: date | None = None, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.raw_dir = raw_dir
        self.tokens = tokens
        self.date_from = date_from
        self.date_to = date_to

    def run(self) -> None:
        try:
            def _cb(phase: str, detail: str, pct: float) -> None:
                self.progress.emit(phase, detail, pct)

            # every QThread needs its own asyncio event loop -
            # run_daily_cycle() is async (uses aiohttp for parallel
            # downloads). isInterruptionRequested is already a native
            # QThread method: requestInterruption() from the UI thread
            # makes it return True here.
            stats = asyncio.run(run_daily_cycle(
                self.cache, self.raw_dir, self.tokens,
                ingest_date=self.date_from, ingest_date_to=self.date_to, progress_cb=_cb,
                cancel_cb=self.isInterruptionRequested,
            ))
            self.finished_ok.emit(stats)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class AddToWatchlistWorker(QThread):
    """Resolves owner/repo via REST (to get repo_id, needed for the same
    bulk GraphQL verification used for everything else) and adds it to the
    watchlist - a flag on known_repos, not a separate table: from that
    moment it enters on its own into the same re-verification cycle as any
    other known repo, and keeps being rechecked forever even if it never
    shows up again in a GH Archive event."""
    finished_ok = Signal(dict)  # {"repo_id":..., "full_name":...}
    failed = Signal(str)

    def __init__(self, cache: CacheManager, tokens: list[str] | None, owner: str, repo: str,
                parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.tokens = tokens
        self.owner = owner
        self.repo = repo

    def run(self) -> None:
        try:
            client = GitHubClient(tokens=self.tokens)
            repo_data = client.get_repo(self.owner, self.repo)
            if repo_data is None:
                self.failed.emit(
                    f"{self.owner}/{self.repo} not found (deleted, private, or never existed)")
                return
            repo_id = repo_data["id"]
            full_name = repo_data["full_name"]
            now = datetime.now(timezone.utc)
            # next_check_due is compared against a bare YYYY-MM-DD in
            # due_for_check, so it must be a DATE. Storing a full ISO
            # timestamp made the string comparison
            # ("2026-09-02T10:00:00+00:00" <= "2026-09-02") false, so a repo
            # just added to the watchlist was never due on the day it was
            # added. Today's date makes it due immediately, which is what
            # the user asked for by adding it.
            self.cache.upsert_known_repo(
                repo_id, full_name, self.owner,
                seen_at=now.isoformat(), next_check_due=now.date().isoformat(),
            )
            self.cache.set_watched(repo_id, True)
            # a repo added while already vanished would otherwise keep its
            # terminal status and be filtered out of due_for_check forever
            self.cache.reactivate_for_checking(repo_id)
            self.finished_ok.emit({"repo_id": repo_id, "full_name": full_name})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class MonitoredRefreshWorker(QThread):
    """The count/list queries on known_repos have always run on the UI
    thread (the table used to be small) - but known_repos can grow to
    millions of rows (e.g. a test over a wide GH Archive range), and a
    COUNT/GROUP BY or an ORDER BY at that scale takes several seconds: on
    tabs/threads specifically built to keep the UI from blocking during
    network/subprocess work, this query was the last one still
    synchronous, with the very same symptom ("not responding") the other
    workers exist to avoid."""
    finished_ok = Signal(dict)  # {"counts": ..., "total": ..., "rows": [...]}
    failed = Signal(str)

    def __init__(self, cache: CacheManager, status: str | None, search: str | None,
                limit: int, offset: int, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.status = status
        self.search = search
        self.limit = limit
        self.offset = offset

    def run(self) -> None:
        try:
            counts = self.cache.count_known_repos_by_status()
            total = self.cache.count_known_repos(status=self.status, search=self.search)
            rows = self.cache.list_known_repos(status=self.status, search=self.search,
                                               limit=self.limit, offset=self.offset)
            watchlist_count = self.cache.count_watchlist()
            self.finished_ok.emit({"counts": counts, "total": total,
                                   "watchlist_count": watchlist_count,
                                   "rows": [dict(r) for r in rows]})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class RecheckShaWorker(QThread):
    """Rechecks the survival of a SINGLE at-risk SHA (check_alive), without
    redoing a fork-scan + archive scan the way a full investigation would
    require - useful to know "is it still alive NOW?" without the waste of
    rerunning everything from scratch."""
    finished_ok = Signal(dict)  # ProbeResult as a dict (dataclasses.asdict)
    failed = Signal(str)

    def __init__(self, cache: CacheManager, tokens: list[str] | None, owner: str, repo: str,
                sha: str, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.tokens = tokens
        self.owner = owner
        self.repo = repo
        self.sha = sha

    def run(self) -> None:
        try:
            client = GitHubClient(tokens=self.tokens)
            refs = client.get_refs(self.owner, self.repo)
            # force_reprobe=True: without it, check_alive would return the
            # result already in the cache (the one shown in the report)
            # instead of checking NOW - pointless for a "recheck" button.
            result = check_alive(client, self.cache, self.owner, self.repo, self.sha, refs,
                                 force_reprobe=True)
            self.finished_ok.emit(asdict(result))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class RateLimitWorker(QThread):
    """Checks the remaining REST budget of EVERY configured token (GET
    /rate_limit, doesn't consume budget) - without this, the only way to
    know a token was close to its limit was to actually see rotation kick
    in, or wait for all of them to run out."""
    finished_ok = Signal(list)  # list[dict], one per token
    failed = Signal(str)

    def __init__(self, tokens: list[str] | None, parent=None) -> None:
        super().__init__(parent)
        self.tokens = tokens

    def run(self) -> None:
        try:
            client = GitHubClient(tokens=self.tokens)
            self.finished_ok.emit(client.get_rate_limit_all())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class RescueWorker(QThread):
    """Actually runs the init/remote/fetch/checkout sequence - nothing to
    copy by hand, but stays pure logic already written in forkforensics/,
    just wrapped here so it doesn't block the UI during git/network
    work."""
    finished_ok = Signal(object)  # RescueResult
    failed = Signal(str)

    def __init__(self, owner: str, repo: str, sha: str, dest_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.owner = owner
        self.repo = repo
        self.sha = sha
        self.dest_root = dest_root

    def run(self) -> None:
        try:
            result = rescue_sha_locally(self.owner, self.repo, self.sha, self.dest_root)
            self.finished_ok.emit(result)
        except RescueError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))


class CloneForkWorker(QThread):
    """Like RescueWorker but for an entire fork (git clone) instead of a
    single SHA - unlike the isolated commit, it brings along the whole
    history chain built on top of it."""
    finished_ok = Signal(object)  # CloneResult
    failed = Signal(str)
    progress = Signal(str, object)  # (raw git line, 0-1 percentage or None)

    def __init__(self, fork_full_name: str, dest_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.fork_full_name = fork_full_name
        self.dest_root = dest_root

    def run(self) -> None:
        try:
            def _cb(line: str, pct: float | None) -> None:
                self.progress.emit(line, pct)

            result = clone_fork_locally(self.fork_full_name, self.dest_root, progress_cb=_cb)
            self.finished_ok.emit(result)
        except RescueError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_message(exc))
