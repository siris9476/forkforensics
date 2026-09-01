"""Selects repos due for re-verification, checks them in bulk via GraphQL
(100 at a time), updates known_repos and generates new rows in
disappearances for those no longer resolvable."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from .archive_ingest import RECHECK_DELAY_DAYS
from .cache import CacheManager
from .github_graphql import BATCH_SIZE, GitHubGraphQLClient
from .github_rest import GitHubClient

logger = logging.getLogger(__name__)


def _check_owner_status(rest_client: GitHubClient, full_name: str) -> str:
    """Indirect signal (not conclusive: GitHub returns the same 'not
    found' both for deleted repos and for ones made private - that's
    intentional, so as not to confirm the existence of private repos to
    strangers). If the owner account is also gone/suspended, though,
    that's a strong hint of actual deletion, not just a changed
    visibility."""
    owner = full_name.split("/", 1)[0]
    try:
        user = rest_client.get_user(owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner check failed for %s: %r", owner, exc)
        return "unknown"
    return "owner_active" if user is not None else "owner_gone"


def run_verify_cycle(cache: CacheManager, gql: GitHubGraphQLClient,
                     rest_client: GitHubClient | None = None,
                     today: date | None = None, max_repos: int = 5000) -> dict:
    today = today or date.today()
    today_str = today.isoformat()
    due = cache.due_for_check(today_str, limit=max_repos)

    n_alive, n_renamed, n_gone, n_undetermined = 0, 0, 0, 0
    for i in range(0, len(due), BATCH_SIZE):
        batch = due[i:i + BATCH_SIZE]
        repo_ids = [row["repo_id"] for row in batch]
        try:
            results = gql.check_alive_batch(repo_ids)
        except Exception as exc:  # noqa: BLE001
            logger.error("verify batch failed for %d repos: %r", len(repo_ids), exc)
            # every repo in this batch is undetermined, same as an
            # individual per-node error - without counting them here they
            # fell out of the stats entirely: checked > alive+renamed+gone+
            # undetermined, with no indication where the missing repos went
            n_undetermined += len(repo_ids)
            continue

        next_due = (today + timedelta(days=RECHECK_DELAY_DAYS)).isoformat()
        for row in batch:
            repo_id = row["repo_id"]
            if repo_id not in results:
                # status undetermined (per-node GraphQL error): leave the
                # row untouched so it comes back on the next cycle. Marking
                # it gone here is exactly how an API hiccup turns into a
                # fabricated disappearance.
                n_undetermined += 1
                continue
            result = results[repo_id]
            if result is None:
                cache.mark_gone(repo_id)
                owner_status = (_check_owner_status(rest_client, row["full_name"])
                               if rest_client is not None else "unknown")
                cache.record_disappearance(
                    repo_id=repo_id, full_name=row["full_name"],
                    detected_date=today_str,
                    last_known_alive=row["last_verified_alive"] or row["last_seen_active"],
                    owner_status=owner_status,
                )
                n_gone += 1
                logger.info("VANISHED: %s (repo_id=%s, owner_status=%s)",
                           row["full_name"], repo_id, owner_status)
            elif result["full_name"].lower() != row["full_name"].lower():
                cache.mark_renamed(repo_id, result["full_name"], today_str, next_due)
                n_renamed += 1
            else:
                cache.mark_verified_alive(repo_id, result["full_name"], today_str, next_due)
                n_alive += 1

    logger.info("verify cycle %s: %d checked, %d alive, %d renamed, %d vanished, "
               "%d undetermined", today_str, len(due), n_alive, n_renamed, n_gone,
               n_undetermined)
    return {"checked": len(due), "alive": n_alive, "renamed": n_renamed, "gone": n_gone,
            "undetermined": n_undetermined}
