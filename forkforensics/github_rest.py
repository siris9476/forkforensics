"""GitHub REST client: forks, refs, commit-by-sha, is_ancestor, and the
"last page" trick to find the oldest reachable commit without having to
page through the entire history."""

from __future__ import annotations

import re
from typing import Iterator

import requests

from .errors import GitHubAPIError, retry_with_backoff
from .rate_limiter import GitHubRateLimiter

API_ROOT = "https://api.github.com"
LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def _parse_link_header(link_header: str | None) -> dict[str, str]:
    if not link_header:
        return {}
    return {rel: url for url, rel in LINK_RE.findall(link_header)}


class GitHubClient:
    def __init__(self, token: str | None = None, tokens: list[str] | None = None,
                session: requests.Session | None = None,
                user_agent: str = "forkforensics/0.1") -> None:
        """tokens (more than one) takes precedence over token (single,
        backward-compatible). Each token has its own HTTP session and its
        own independent rate-limit budget - when the active one is about to
        run out, we switch to the next one with margin to spare instead of
        waiting for the reset, which previously made a single token the
        bottleneck for a large scan (a known limitation stated in the
        README)."""
        token_list = list(tokens) if tokens else [token]
        self._sessions: list[requests.Session] = []
        self._limiters: list[GitHubRateLimiter] = []
        for t in token_list:
            sess = session if (session is not None and len(token_list) == 1) else requests.Session()
            headers = {"Accept": "application/vnd.github+json", "User-Agent": user_agent}
            if t:
                headers["Authorization"] = f"Bearer {t}"
            sess.headers.update(headers)
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
                    # its reset window has passed: the stale count is
                    # meaningless, drop it so before_request() doesn't sleep
                    lim.remaining = None
                self._active = j
                return
        # all tokens are exhausted: stay on the current one, before_request()
        # will wait for the reset just like with a single token

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        def _do() -> requests.Response:
            self._maybe_rotate()
            self.limiter.before_request()
            resp = self.session.get(url, params=params, timeout=30)
            self.limiter.after_response(resp)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                wait = self.limiter.handle_secondary_limit(resp)
                raise _Transient(wait)
            if resp.status_code >= 500:
                raise _Transient(1.0)
            return resp

        def _status(exc: Exception) -> int | None:
            return 503 if isinstance(exc, _Transient) else None

        try:
            return retry_with_backoff(_do, retries=4, status_getter=_status)
        except _Transient as exc:
            raise GitHubAPIError(f"persistent rate limit/5xx on {url}") from exc

    def get_user(self, username: str) -> dict | None:
        """GET /users/{username} - works for both user accounts and
        organizations (both respond with 200 and a 'type' field). None if
        the account no longer exists/has been suspended - used as an
        indirect signal to distinguish "almost certainly deleted" (the
        owner account is gone too) from "deleted or made private,
        indistinguishable" (the owner is still active)."""
        resp = self._get(f"{API_ROOT}/users/{username}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_repo(self, owner: str, repo: str) -> dict | None:
        resp = self._get(f"{API_ROOT}/repos/{owner}/{repo}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_repo_by_id(self, repo_id: int) -> dict | None:
        """Resolves a repo by its immutable ID - if renamed/transferred it
        returns the record with the CURRENT name; None if it's truly gone."""
        resp = self._get(f"{API_ROOT}/repositories/{repo_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def list_forks(self, owner: str, repo: str, per_page: int = 100) -> Iterator[dict]:
        url = f"{API_ROOT}/repos/{owner}/{repo}/forks"
        params = {"per_page": per_page, "sort": "newest"}
        while url:
            resp = self._get(url, params=params)
            resp.raise_for_status()
            yield from resp.json()
            links = _parse_link_header(resp.headers.get("Link"))
            url = links.get("next")
            params = None  # the "next" url already contains the query params

    def get_refs(self, owner: str, repo: str) -> list[dict]:
        out = []
        for kind in ("branches", "tags"):
            url = f"{API_ROOT}/repos/{owner}/{repo}/{kind}"
            params: dict | None = {"per_page": 100}
            while url:
                resp = self._get(url, params=params)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                out.extend(resp.json())
                links = _parse_link_header(resp.headers.get("Link"))
                url = links.get("next")
                params = None
        return out

    def get_commit(self, owner: str, repo: str, sha: str) -> dict | None:
        resp = self._get(f"{API_ROOT}/repos/{owner}/{repo}/commits/{sha}")
        if resp.status_code in (404, 422):
            return None
        resp.raise_for_status()
        return resp.json()

    def is_ancestor(self, owner: str, repo: str, base_sha: str, head_sha: str) -> bool:
        resp = self._get(f"{API_ROOT}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}")
        if resp.status_code != 200:
            return False
        status = resp.json().get("status")
        return status in ("ahead", "identical")

    def oldest_commit_via_last_page(self, owner: str, repo: str,
                                    sha: str | None = None) -> tuple[str, str, int] | None:
        """Trick: GET /commits?per_page=1, read Link: rel="last" -> page N;
        request page N -> that single commit is the oldest reachable one,
        N is roughly the total commit count."""
        params: dict = {"per_page": 1}
        if sha:
            params["sha"] = sha
        resp = self._get(f"{API_ROOT}/repos/{owner}/{repo}/commits", params=params)
        if resp.status_code == 409:
            return None  # empty repo
        if resp.status_code != 200:
            return None
        commits = resp.json()
        links = _parse_link_header(resp.headers.get("Link"))
        last_url = links.get("last")
        if not last_url:
            if not commits:
                return None
            c = commits[0]
            return c["sha"], c["commit"]["committer"]["date"], 1
        m = re.search(r"[?&]page=(\d+)", last_url)
        total = int(m.group(1)) if m else None
        resp2 = self._get(last_url)
        resp2.raise_for_status()
        last_commits = resp2.json()
        if not last_commits:
            return None
        c = last_commits[-1]
        return c["sha"], c["commit"]["committer"]["date"], (total or len(last_commits))

    def raw_url(self, owner: str, repo: str, sha: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"

    def check_raw_alive(self, owner: str, repo: str, sha: str, path: str) -> bool:
        resp = self.session.get(self.raw_url(owner, repo, sha, path), timeout=20)
        return resp.status_code == 200

    def get_rate_limit_all(self) -> list[dict]:
        """Rate limit status for EVERY configured token, not just the
        currently active one - GET /rate_limit does not consume REST budget
        (it's a dedicated endpoint made exactly for this). Deliberately
        bypasses _get()/the rotation mechanism: this is a single state
        read, it doesn't make sense to run it through retry/backoff. Used
        to show the user how much headroom remains on each token, not just
        discover it indirectly when rotation kicks in."""
        results = []
        for sess in self._sessions:
            try:
                resp = sess.get(f"{API_ROOT}/rate_limit", timeout=15)
                resp.raise_for_status()
                core = (resp.json().get("resources") or {}).get("core") or {}
                results.append({"limit": core.get("limit"), "remaining": core.get("remaining"),
                               "reset_at": core.get("reset")})
            except Exception as exc:  # noqa: BLE001
                results.append({"error": str(exc)})
        return results


class _Transient(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__(f"transient, wait={wait}")
        self.wait = wait
