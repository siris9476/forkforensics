from unittest.mock import MagicMock

from forkforensics.fork_scan import compute_coverage, discover_forks, rank_forks
from forkforensics.models import ForkCoverage, ForkInfo


def test_discover_forks_builds_fork_info_from_api():
    client = MagicMock()
    client.get_repo.return_value = {"pushed_at": "2020-01-01T00:00:00Z"}
    client.list_forks.return_value = [
        {"full_name": "u1/r", "owner": {"login": "u1"}, "name": "r",
         "default_branch": "main", "created_at": "2019-01-01T00:00:00Z",
         "pushed_at": "2019-06-01T00:00:00Z"},
        {"full_name": "u2/r", "owner": {"login": "u2"}, "name": "r",
         "default_branch": "main", "created_at": "2019-02-01T00:00:00Z",
         "pushed_at": "2019-02-01T00:00:00Z"},
    ]
    forks = discover_forks(client, "orig", "r", max_depth=1, max_forks=200)
    assert len(forks) == 2
    assert {f.full_name for f in forks} == {"u1/r", "u2/r"}
    assert all(f.depth == 1 for f in forks)


def test_discover_forks_respects_max_forks_truncation():
    client = MagicMock()
    client.get_repo.return_value = {"pushed_at": "2020-01-01T00:00:00Z"}
    client.list_forks.return_value = [
        {"full_name": f"u{i}/r", "owner": {"login": f"u{i}"}, "name": "r",
         "default_branch": "main", "created_at": "2019-01-01T00:00:00Z",
         "pushed_at": "2019-01-01T00:00:00Z"}
        for i in range(10)
    ]
    forks = discover_forks(client, "orig", "r", max_depth=1, max_forks=3)
    assert len(forks) == 3


def test_compute_coverage_uses_last_page_trick():
    client = MagicMock()
    client.oldest_commit_via_last_page.return_value = ("abc123", "2018-01-01T00:00:00Z", 42)
    fork = ForkInfo(full_name="u/r", owner="u", repo_name="r", default_branch="main",
                    created_at="", pushed_at="", depth=1)
    cov = compute_coverage(client, fork)
    assert cov.oldest_commit_sha == "abc123"
    assert cov.commit_count == 42
    assert cov.error is None


def test_compute_coverage_handles_empty_repo():
    client = MagicMock()
    client.oldest_commit_via_last_page.return_value = None
    fork = ForkInfo(full_name="u/r", owner="u", repo_name="r", default_branch="main",
                    created_at="", pushed_at="", depth=1)
    cov = compute_coverage(client, fork)
    assert cov.error is not None
    assert cov.oldest_commit_sha is None


def test_rank_forks_orders_by_oldest_first():
    fork_a = ForkInfo(full_name="a/r", owner="a", repo_name="r", default_branch="main",
                      created_at="", pushed_at="", depth=1)
    fork_b = ForkInfo(full_name="b/r", owner="b", repo_name="r", default_branch="main",
                      created_at="", pushed_at="", depth=1)
    covs = [
        ForkCoverage(fork=fork_a, oldest_commit_date="2020-01-01", commit_count=5),
        ForkCoverage(fork=fork_b, oldest_commit_date="2015-01-01", commit_count=3),
    ]
    ranked = rank_forks(covs)
    assert ranked[0].fork.full_name == "b/r"  # 2015 before 2020


def test_discover_forks_recurses_into_depth2_when_forks_count_positive():
    """Regression for the real bug found on octocat/data: a fork never
    touched after creation (pushed_at < created_at, since pushed_at
    inherits the timestamp of the source's last commit) can still have a
    fork-of-fork - recursion must be based on forks_count, not on comparing
    the dates."""
    client = MagicMock()
    client.get_repo.return_value = {"pushed_at": "2020-01-01T00:00:00Z"}

    def list_forks_side_effect(owner, repo):
        if (owner, repo) == ("orig", "r"):
            return [{"full_name": "dormant/r", "owner": {"login": "dormant"}, "name": "r",
                     "default_branch": "main",
                     "created_at": "2025-06-20T13:08:11Z",   # AFTER pushed_at
                     "pushed_at": "2025-06-19T13:36:34Z",    # BEFORE created_at
                     "forks_count": 1}]
        if (owner, repo) == ("dormant", "r"):
            return [{"full_name": "rescue/r", "owner": {"login": "rescue"}, "name": "r",
                     "default_branch": "main", "created_at": "2025-06-21T00:00:00Z",
                     "pushed_at": "2025-06-21T00:00:00Z", "forks_count": 0}]
        return []

    client.list_forks.side_effect = list_forks_side_effect
    forks = discover_forks(client, "orig", "r", max_depth=2, max_forks=200)
    full_names = {f.full_name for f in forks}
    assert "dormant/r" in full_names
    assert "rescue/r" in full_names, "fork-of-fork was not found despite forks_count=1"
    rescue = next(f for f in forks if f.full_name == "rescue/r")
    assert rescue.depth == 2


def test_discover_forks_does_not_recurse_when_forks_count_zero():
    client = MagicMock()
    client.get_repo.return_value = {"pushed_at": "2020-01-01T00:00:00Z"}

    def list_forks_side_effect(owner, repo):
        if (owner, repo) == ("orig", "r"):
            return [{"full_name": "leaf/r", "owner": {"login": "leaf"}, "name": "r",
                     "default_branch": "main", "created_at": "2020-01-01T00:00:00Z",
                     "pushed_at": "2020-06-01T00:00:00Z", "forks_count": 0}]
        raise AssertionError("should not scan the forks of a repo with forks_count=0")

    client.list_forks.side_effect = list_forks_side_effect
    forks = discover_forks(client, "orig", "r", max_depth=2, max_forks=200)
    assert len(forks) == 1


def test_rank_forks_puts_errors_last():
    fork_a = ForkInfo(full_name="a/r", owner="a", repo_name="r", default_branch="main",
                      created_at="", pushed_at="", depth=1)
    fork_b = ForkInfo(full_name="b/r", owner="b", repo_name="r", default_branch="main",
                      created_at="", pushed_at="", depth=1)
    covs = [
        ForkCoverage(fork=fork_a, error="empty"),
        ForkCoverage(fork=fork_b, oldest_commit_date="2020-01-01", commit_count=5),
    ]
    ranked = rank_forks(covs)
    assert ranked[0].fork.full_name == "b/r"
    assert ranked[-1].fork.full_name == "a/r"
