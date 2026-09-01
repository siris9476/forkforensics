"""The central phenomenon of this project: GET /repos/{o}/{r}/commits/{sha}
can respond 200 even for SHAs no longer reachable from any ref - this is
what makes a recoverable commit "alive but orphaned" before GitHub's
garbage collector actually removes it.

Third signal (besides api_alive via REST and raw_alive via
raw.githubusercontent): git_fetch_alive, which checks with the SAME command
used in the final rescue (report.rescue_commands: "git fetch origin
<sha>") - if this succeeds NOW, we know the rescue command will actually
work when the user runs it. Technique independently confirmed by
force-push-scanner (Truffle Security), which uses the same direct fetch to
recover orphan commits from GH Archive.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone

from .cache import CacheManager
from .github_rest import GitHubClient
from .models import ProbeResult

logger = logging.getLogger(__name__)

GIT_FETCH_TIMEOUT = 25  # seconds, per single SHA
_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


def check_reachable(client: GitHubClient, owner: str, repo: str, sha: str,
                    refs: list[dict]) -> bool:
    for ref in refs:
        ref_sha = (ref.get("commit") or {}).get("sha")
        if not ref_sha:
            continue
        if ref_sha == sha or client.is_ancestor(owner, repo, sha, ref_sha):
            return True
    return False


def check_git_fetch_alive(owner: str, repo: str, sha: str,
                          timeout: int = GIT_FETCH_TIMEOUT) -> bool:
    """Direct check via the git protocol, not via the API: tries
    `git fetch origin <sha>` from an EMPTY local repository (no
    pre-existing history, exactly like the "rescue fork" we recommend to
    the user in rescue_commands). If it succeeds, the rescue command will
    actually work. --depth=1 to transfer only the requested object, not
    its whole ancestry.

    Requires the `git` binary on PATH; if missing or the fetch times
    out/fails for any reason, returns False (fail-safe: an error here
    should never crash the investigation, it's just one more signal, not
    the only one)."""
    # the sha comes from third-party GH Archive JSON; git's positional ref
    # slot is option-parsed, so an unvalidated value starting with "--"
    # would be read as a flag rather than a ref
    if not _SHA_RE.match(sha or ""):
        logger.warning("refusing to probe a malformed SHA: %r", sha)
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="forkforensics_probe_") as tmp:
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            subprocess.run(["git", "init", "--quiet", tmp], check=True,
                          timeout=10, capture_output=True, env=env)
            subprocess.run(
                ["git", "-C", tmp, "remote", "add", "origin",
                 f"https://github.com/{owner}/{repo}.git"],
                check=True, timeout=10, capture_output=True, env=env,
            )
            result = subprocess.run(
                ["git", "-C", tmp, "fetch", "--quiet", "--depth=1", "origin", "--", sha],
                capture_output=True, timeout=timeout, env=env,
            )
            return result.returncode == 0
    except FileNotFoundError:
        logger.warning("'git' binary not found on PATH: git_fetch_alive always False")
        return False
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        logger.debug("git fetch failed for %s/%s@%s: %r", owner, repo, sha, exc)
        return False


def check_alive(client: GitHubClient, cache: CacheManager, owner: str, repo: str,
                sha: str, refs: list[dict], force_reprobe: bool = False,
                check_git_fetch: bool = True) -> ProbeResult:
    if not force_reprobe:
        cached = cache.get_probe(sha, owner, repo)
        if cached is not None:
            return ProbeResult(
                sha=sha, reachable_from_current_refs=bool(cached["reachable"]),
                api_alive=bool(cached["api_alive"]), raw_alive=bool(cached["raw_alive"]),
                git_fetch_alive=bool(cached["git_fetch_alive"]),
                checked_at=cached["checked_at"],
                commit_message=cached["commit_message"], commit_author=cached["commit_author"],
                commit_date=cached["commit_date"], files_changed=cached["files_changed"],
                parent_count=cached["parent_count"],
                changed_files=json.loads(cached["changed_files"]) if cached["changed_files"] else None,
            )

    reachable = check_reachable(client, owner, repo, sha, refs)
    commit = client.get_commit(owner, repo, sha)
    api_alive = commit is not None

    raw_alive = False
    commit_message = commit_author = commit_date = None
    files_changed = parent_count = None
    changed_files = None
    if commit is not None:
        files = commit.get("files") or []
        for f in files[:3]:
            path = f.get("filename")
            if path and client.check_raw_alive(owner, repo, sha, path):
                raw_alive = True
                break
        # already downloaded for the api_alive check - no need for an
        # extra call to show what's really in this commit before deciding
        # whether to rescue it. parent_count=0 is a verifiable fact (the
        # commit has no parents in the API response), not a guess:
        # rescuing it in isolation carries no history along with it, by
        # construction. The file names (not just the count) tell you what
        # you're actually about to rescue.
        commit_info = commit.get("commit") or {}
        commit_message = commit_info.get("message")
        commit_author = (commit_info.get("author") or {}).get("name")
        commit_date = (commit_info.get("author") or {}).get("date")
        files_changed = len(files)
        changed_files = [f.get("filename") for f in files[:20] if f.get("filename")]
        parent_count = len(commit.get("parents") or [])

    git_fetch_alive = False
    if check_git_fetch and not reachable:
        # only for orphans: if it's already reachable from a ref, the
        # fetch would trivially succeed and adds no information
        git_fetch_alive = check_git_fetch_alive(owner, repo, sha)

    checked_at = datetime.now(timezone.utc).isoformat()
    cache.record_probe(sha, owner, repo, reachable, api_alive, raw_alive,
                       git_fetch_alive, checked_at, commit_message, commit_author,
                       commit_date, files_changed, parent_count,
                       json.dumps(changed_files) if changed_files is not None else None)
    return ProbeResult(sha=sha, reachable_from_current_refs=reachable,
                       api_alive=api_alive, raw_alive=raw_alive,
                       git_fetch_alive=git_fetch_alive, checked_at=checked_at,
                       commit_message=commit_message, commit_author=commit_author,
                       commit_date=commit_date, files_changed=files_changed,
                       parent_count=parent_count, changed_files=changed_files)
