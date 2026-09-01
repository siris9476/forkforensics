"""BFS over a repository's forks: discovers which forks preserve the deepest
history (useful when the original repo was re-initialized/reset and lost
history that its forks still hold onto)."""

from __future__ import annotations

import logging

from .github_rest import GitHubClient
from .models import ForkCoverage, ForkInfo

logger = logging.getLogger(__name__)


def discover_forks(client: GitHubClient, owner: str, repo: str,
                   max_depth: int = 1, max_forks: int = 200,
                   strategy: str = "recent") -> list[ForkInfo]:
    """BFS: level 0 = original repo (not included in the output), level 1 =
    direct forks. Recurses to level 2+ ONLY if max_depth>=2, and only into
    forks with a pushed_at later than the fork point (a fork never touched
    after the fork cannot have added depth)."""
    root = client.get_repo(owner, repo)
    if root is None:
        raise ValueError(f"{owner}/{repo} not found")

    out: list[ForkInfo] = []
    # a leftover 4th element (the parent's pushed_at) used to be threaded
    # through this queue for the earlier, buggy pushed_at>created_at
    # heuristic; the comment below already documents why it was replaced by
    # forks_count, but the now-unused field itself never got removed
    frontier = [(owner, repo, 0)]
    visited: set[str] = {f"{owner}/{repo}"}

    while frontier:
        cur_owner, cur_repo, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        count_this_level = 0
        for fork in client.list_forks(cur_owner, cur_repo):
            if count_this_level >= max_forks:
                logger.warning("%s/%s: truncated to %d forks (strategy=%s)",
                              cur_owner, cur_repo, max_forks, strategy)
                break
            full_name = fork["full_name"]
            if full_name in visited:
                continue
            visited.add(full_name)
            info = ForkInfo(
                full_name=full_name,
                owner=fork["owner"]["login"],
                repo_name=fork["name"],
                default_branch=fork.get("default_branch", "main"),
                created_at=fork.get("created_at", ""),
                pushed_at=fork.get("pushed_at", ""),
                depth=depth + 1,
            )
            out.append(info)
            count_this_level += 1
            # NOTE (real bug found on a real vanished repository): a fork's pushed_at
            # inherits the timestamp of the LAST COMMIT inherited from the
            # source, which can be EARLIER than created_at (the moment of
            # the fork) - "pushed_at > created_at" does NOT tell you whether
            # the fork was itself forked further by others. The correct
            # signal is forks_count (already present in the list_forks
            # response, which returns the full repo object for each fork).
            if depth + 1 < max_depth and fork.get("forks_count", 0) > 0:
                frontier.append((info.owner, info.repo_name, depth + 1))
        if strategy != "all" and len(out) >= max_forks:
            break

    return out


def compute_coverage(client: GitHubClient, fork: ForkInfo) -> ForkCoverage:
    try:
        result = client.oldest_commit_via_last_page(fork.owner, fork.repo_name)
    except Exception as exc:  # noqa: BLE001
        return ForkCoverage(fork=fork, error=str(exc))
    if result is None:
        return ForkCoverage(fork=fork, error="empty or unreachable repo")
    sha, dt, count = result
    return ForkCoverage(fork=fork, oldest_commit_sha=sha, oldest_commit_date=dt,
                        commit_count=count)


def rank_forks(coverages: list[ForkCoverage]) -> list[ForkCoverage]:
    def key(c: ForkCoverage):
        if c.error or not c.oldest_commit_date:
            return ("9999", 0)  # bottom of the ranking
        return (c.oldest_commit_date, -(c.commit_count or 0))
    return sorted(coverages, key=key)
