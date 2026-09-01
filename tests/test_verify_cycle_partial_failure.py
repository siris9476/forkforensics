"""The single worst failure this tool can have is reading an API hiccup as
a repository disappearance: a batch is 100 repos wide, and every fabricated
disappearance then launches an automatic recovery investigation. These
tests pin the behaviour that prevents it."""

from unittest.mock import MagicMock

import pytest

from forkforensics.cache import CacheManager
from forkforensics.errors import GitHubAPIError
from forkforensics.github_graphql import GitHubGraphQLClient
from forkforensics.verify_cycle import run_verify_cycle


def _client_returning(body):
    """A GraphQL client whose HTTP layer returns `body`, so the real
    _query()/check_alive_batch() logic runs against it."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    resp.headers = {}
    session = MagicMock()
    session.post.return_value = resp
    return GitHubGraphQLClient(token="t", session=session)


def test_total_graphql_failure_raises_instead_of_reporting_everything_gone():
    """GitHub signals a rate limit / timeout with HTTP 200 and
    {"data": null, "errors": [...]}. The old guard tested `"data" not in
    body`, but the key IS present (value null), so the failure was accepted
    silently and every alias came back missing."""
    client = _client_returning({
        "data": None,
        "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
    })
    with pytest.raises(GitHubAPIError):
        client.check_alive_batch([1, 2, 3])


def test_per_node_error_leaves_that_repo_undetermined_not_gone():
    """A partial failure returns data for the aliases that resolved plus an
    errors entry naming the ones that didn't. The failed alias must be
    ABSENT from the result - absent means "unknown", and only an explicit
    null means "vanished"."""
    client = _client_returning({
        "data": {
            "n1": {"databaseId": 1, "name": "widget", "owner": {"login": "acme"},
                   "isPrivate": False},
            "n2": None,
        },
        "errors": [{"type": "SERVICE_UNAVAILABLE", "path": ["n3"]}],
    })
    out = client.check_alive_batch([1, 2, 3])

    assert out[1]["full_name"] == "acme/widget"  # resolved
    assert out[2] is None                        # explicitly null -> vanished
    assert 3 not in out                          # errored -> undetermined


def test_undetermined_repos_are_not_marked_gone_by_the_verify_cycle(tmp_path):
    """End-to-end guard: the repo whose alias errored must keep its
    'active' status and produce no disappearance row, while the one
    explicitly reported as null is recorded normally."""
    cache = CacheManager(tmp_path / "t.db")
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-01")
    cache.upsert_known_repo(2, "acme/ghost", "acme", "2024-01-01", "2024-01-01")
    cache.upsert_known_repo(3, "acme/unknown", "acme", "2024-01-01", "2024-01-01")

    gql = MagicMock()
    gql.check_alive_batch.return_value = {
        1: {"id": 1, "name": "widget", "owner": "acme",
            "full_name": "acme/widget", "is_private": False},
        2: None,
        # 3 deliberately absent: status could not be determined
    }

    stats = run_verify_cycle(cache, gql, rest_client=None)

    assert stats["alive"] == 1
    assert stats["gone"] == 1
    assert stats["undetermined"] == 1

    vanished = {row["full_name"] for row in cache.list_disappearances()}
    assert vanished == {"acme/ghost"}          # NOT acme/unknown
    assert cache.get_known_repo(3)["status"] == "active"  # untouched, retried next cycle
    cache.close()


def test_a_whole_batch_call_raising_counts_its_repos_as_undetermined(tmp_path):
    """Regression: when check_alive_batch itself raises (not a per-node
    error, the whole call), every repo in that batch used to fall out of
    the stats dict entirely - `checked` could exceed
    alive+renamed+gone+undetermined with no record of where the rest went.
    None of them may be marked gone on the strength of a failed call."""
    cache = CacheManager(tmp_path / "t.db")
    cache.upsert_known_repo(1, "acme/widget", "acme", "2024-01-01", "2024-01-01")
    cache.upsert_known_repo(2, "acme/gizmo", "acme", "2024-01-01", "2024-01-01")

    gql = MagicMock()
    gql.check_alive_batch.side_effect = GitHubAPIError("rate limit exceeded")

    stats = run_verify_cycle(cache, gql, rest_client=None)

    assert stats["checked"] == 2
    assert stats["undetermined"] == 2
    assert stats["alive"] == stats["renamed"] == stats["gone"] == 0
    assert cache.list_disappearances() == []
    assert cache.get_known_repo(1)["status"] == "active"
    assert cache.get_known_repo(2)["status"] == "active"
    cache.close()
