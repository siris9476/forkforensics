"""Bulk verification of repository existence via GraphQL nodes(ids:[...]).

WARNING (stated in the plan): the conversion from the legacy numeric ID
(what GH Archive exposes as repo.id) to the global GraphQL node-id uses the
base64("010:Repository" + id) encoding known to work for "classic"
repositories. It must be validated against a real sample before trusting it
at full volume - see tests/test_github_graphql.py and the note in the
README.
"""

from __future__ import annotations

import base64
import logging

import requests

from .errors import GitHubAPIError, retry_with_backoff
from .rate_limiter import GitHubRateLimiter

GRAPHQL_URL = "https://api.github.com/graphql"
BATCH_SIZE = 100

logger = logging.getLogger(__name__)


def legacy_id_to_node_id(repo_id: int) -> str:
    """Known encoding for GitHub's "classic" Repository nodes (pre-2021).
    NOT guaranteed for all repositories: always verify alive_map against a
    known sample before relying on it at scale (see module docstring)."""
    raw = f"010:Repository{repo_id}"
    return base64.b64encode(raw.encode()).decode()


class GitHubGraphQLClient:
    def __init__(self, token: str | None = None, tokens: list[str] | None = None,
                session: requests.Session | None = None,
                user_agent: str = "forkforensics/0.1") -> None:
        """Same multi-token model as GitHubClient: one session and one
        independent budget per token, rotating before the active one runs
        out. This matters more here than on REST - the nightly bulk
        verification is the single largest consumer of the daily budget,
        and it used to run on tokens[0] alone no matter how many were
        configured."""
        token_list = [t for t in (list(tokens) if tokens else [token]) if t]
        if not token_list:
            raise GitHubAPIError("GraphQL always requires a token (no anonymous access)")
        self._sessions: list[requests.Session] = []
        self._limiters: list[GitHubRateLimiter] = []
        for t in token_list:
            sess = session if (session is not None and len(token_list) == 1) else requests.Session()
            sess.headers.update({
                "Authorization": f"Bearer {t}",
                "User-Agent": user_agent,
            })
            self._sessions.append(sess)
            self._limiters.append(GitHubRateLimiter())
        self._active = 0

    @property
    def session(self) -> requests.Session:
        return self._sessions[self._active]

    @property
    def limiter(self) -> GitHubRateLimiter:
        return self._limiters[self._active]

    def _maybe_rotate(self) -> None:
        if len(self._sessions) == 1:
            return
        if self._limiters[self._active].is_available():
            return
        for offset in range(1, len(self._sessions)):
            j = (self._active + offset) % len(self._sessions)
            lim = self._limiters[j]
            if lim.is_available():
                if lim.remaining is not None and lim.remaining <= lim.min_remaining:
                    lim.remaining = None
                self._active = j
                return

    def _query(self, query: str) -> dict:
        def _do() -> requests.Response:
            self._maybe_rotate()
            self.limiter.before_request()
            resp = self.session.post(GRAPHQL_URL, json={"query": query}, timeout=30)
            self.limiter.after_response(resp)
            if resp.status_code >= 500:
                raise _Transient()
            return resp

        def _status(exc: Exception) -> int | None:
            return 503 if isinstance(exc, _Transient) else None

        resp = retry_with_backoff(_do, retries=4, status_getter=_status)
        resp.raise_for_status()
        body = resp.json()
        # GraphQL signals failure with HTTP 200 + an "errors" array. On a
        # total failure (rate limit, timeout, MAX_NODE_LIMIT_EXCEEDED) the
        # body is {"data": null, "errors": [...]} - "data" IS present, with
        # value None, so testing for the key alone silently accepted the
        # failure and every alias came back missing. The caller then read
        # "missing" as "repository vanished" and fabricated a disappearance
        # for the whole batch. Test on the VALUE, not the key.
        if body.get("errors") and not body.get("data"):
            raise GitHubAPIError(f"GraphQL error: {body['errors']}")
        return body

    def check_alive_batch(self, repo_ids: list[int]) -> dict[int, dict | None]:
        """For each repo_id: dict with {'id','name','owner','full_name'} if
        still resolvable (possibly renamed/transferred), None if it no
        longer resolves (deleted OR made private - indistinguishable).

        A repo_id is ABSENT from the result when its status could not be
        determined (a per-node GraphQL error, or an alias missing from the
        response). Absent must never be read as "vanished": partial
        failures are normal at this batch size, and treating them as
        disappearances fabricates up to 100 of them per failed query."""
        if len(repo_ids) > BATCH_SIZE:
            raise ValueError(f"batch too large: {len(repo_ids)} > {BATCH_SIZE}")

        node_ids = {rid: legacy_id_to_node_id(rid) for rid in repo_ids}
        aliases = "\n".join(
            f'n{rid}: node(id: "{nid}") {{ '
            f'... on Repository {{ databaseId name owner {{ login }} isPrivate }} }}'
            for rid, nid in node_ids.items()
        )
        query = f"query {{\n{aliases}\n}}"
        body = self._query(query)
        data = body.get("data") or {}

        # A partial failure returns data for the aliases that resolved plus
        # an "errors" entry naming the ones that didn't, via its "path".
        # Those are undetermined, not gone.
        errored_aliases = {
            step
            for err in (body.get("errors") or [])
            for step in (err.get("path") or [])
            if isinstance(step, str)
        }

        out: dict[int, dict | None] = {}
        for rid in repo_ids:
            alias = f"n{rid}"
            if alias in errored_aliases or alias not in data:
                continue  # undetermined - leave it out, do NOT infer "gone"
            node = data[alias]
            if not node:
                out[rid] = None
                continue
            out[rid] = {
                "id": node["databaseId"],
                "name": node["name"],
                "owner": node["owner"]["login"],
                "full_name": f"{node['owner']['login']}/{node['name']}",
                "is_private": node.get("isPrivate", False),
            }
        return out


class _Transient(Exception):
    pass
