"""The automatic daily cycle: ingest -> verify -> automatic investigation
on every new disappearance. Callable both from the internal scheduler
(APScheduler inside the desktop process) and from a standalone entry
point."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from .archive_ingest import ingest_day
from .cache import CacheManager
from .errors import GitHubAPIError
from .github_graphql import GitHubGraphQLClient
from .github_rest import GitHubClient
from .investigate import investigate
from .verify_cycle import run_verify_cycle

logger = logging.getLogger(__name__)


async def run_daily_cycle(cache: CacheManager, raw_dir: Path, token: str | list[str] | None,
                          ingest_date: date | None = None, ingest_date_to: date | None = None,
                          progress_cb=None, cancel_cb=None) -> dict:
    """ingest_date defaults to YESTERDAY (today isn't complete yet on GH
    Archive). If ingest_date_to is given, ingests every day from
    ingest_date to ingest_date_to (inclusive) before moving on to verify
    and investigate - same cycle, just over more days: this is how a
    date-range-only search (with no specific repo) discovers repos never
    seen before and immediately puts them in the verification queue.
    cancel_cb() -> bool: checked between one hour and the next (ingest_day)
    and between one day and the next - a wide range can mean thousands of
    downloads, it must be interruptible without closing the app."""
    # token can be a single string (backward-compatible) or a list of
    # multiple tokens to rotate through - normalized right away into an
    # internal list.
    tokens = [token] if isinstance(token, str) else (list(token) if token else [])
    if not tokens:
        raise RuntimeError("a GitHub token is required for the daily cycle "
                          "(GraphQL has no anonymous access) - set it up in Settings")

    date_from = ingest_date or (date.today() - timedelta(days=1))
    date_to = ingest_date_to or date_from
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    n_days = (date_to - date_from).days + 1
    logger.info("cycle: ingest %s -> %s (%d days)", date_from.isoformat(), date_to.isoformat(), n_days)

    n_seen = 0
    cancelled = False
    day = date_from
    for day_i in range(n_days):
        if cancel_cb and cancel_cb():
            cancelled = True
            break

        def _hour_progress(i: int, total: int, detail: str, _day_i=day_i, _day=day) -> None:
            if progress_cb:
                frac_within_day = i / max(total, 1)
                progress_cb("ingest",
                           f"day {_day_i + 1}/{n_days} ({_day.isoformat()}) — {detail}",
                           0.05 + 0.55 * (_day_i + frac_within_day) / n_days)

        n_seen += await ingest_day(day, raw_dir, cache, concurrency=4, keep_raw=False,
                                   progress_cb=_hour_progress, cancel_cb=cancel_cb)
        if cancel_cb and cancel_cb():
            cancelled = True
            break
        day += timedelta(days=1)

    if cancelled:
        result = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                 "repos_seen": n_seen, "checked": 0, "alive": 0, "renamed": 0, "gone": 0,
                 "auto_investigated": 0, "cancelled": True}
        logger.info("cycle cancelled by the user: %s", json.dumps(result))
        if progress_cb:
            progress_cb("cancelled", "cancelled by the user", 1.0)
        return result

    if progress_cb:
        progress_cb("verify", "rechecking repos due for verification...", 0.65)
    rest_client = GitHubClient(tokens=tokens)
    gql = GitHubGraphQLClient(tokens=tokens)
    verify_stats = run_verify_cycle(cache, gql, rest_client=rest_client, today=date.today())

    pending = cache.list_pending_disappearances(limit=5000)
    investigated = 0
    api_halt: str | None = None
    for i, row in enumerate(pending):
        if cancel_cb and cancel_cb():
            break
        if progress_cb:
            progress_cb("investigate", f"{row['full_name']} ({i + 1}/{len(pending)})",
                       0.85 + 0.15 * i / max(len(pending), 1))
        owner, repo = row["full_name"].split("/", 1)
        cache.update_investigation(row["disappearance_id"], "running", None)
        try:
            report = investigate(rest_client, cache, owner, repo, raw_dir)
            summary = _summarize(report)
            cache.update_investigation(row["disappearance_id"], "done", summary)
        except GitHubAPIError as exc:
            # this isn't a failure of THIS repo (e.g. not found, normal for
            # a tool that investigates vanished repos) - it's the GitHub
            # API itself no longer responding reliably (persistent rate
            # limit, 5xx). Continuing would keep hammering the API for no
            # reason on all the remaining ones: stop and say so clearly,
            # instead of silently marking them as errored one by one.
            cache.update_investigation(row["disappearance_id"], "pending", None)
            logger.error("stopped due to a persistent API error on %s: %s", row["full_name"], exc)
            api_halt = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("automatic investigation failed for %s", row["full_name"])
            cache.update_investigation(row["disappearance_id"], "error", str(exc))
        investigated += 1

    result = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
             "repos_seen": n_seen, **verify_stats, "auto_investigated": investigated,
             "investigate_remaining": len(pending) - investigated}
    if api_halt:
        result["api_error"] = api_halt
    logger.info("cycle completed: %s", json.dumps(result))
    if progress_cb:
        progress_cb("api_error" if api_halt else "done",
                   f"stopped: {api_halt}" if api_halt else "completed", 1.0)
    return result


def _summarize(report: dict) -> str:
    ranking = report.get("fork_ranking", [])
    at_risk = report.get("at_risk_shas", [])
    if ranking and ranking[0].get("oldest_commit_date"):
        best = ranking[0]
        return (f"history preserved in fork {best['full_name']} up to "
               f"{best['oldest_commit_date']}; {len(at_risk)} at-risk SHAs found")
    if at_risk:
        return f"no useful fork, but {len(at_risk)} orphan SHAs still alive found"
    return "no recovery path found"
