"""build_report/rescue_commands had zero direct tests: rescue_commands
produces the literal git commands a user is told to copy-paste to recover
a repo, and build_report's shape (coverage_continuous, gaps, at_risk_shas)
is the contract _build_verdict and daily_cycle._summarize both consume.
Nothing previously would have caught a reordered/typo'd command or an
inverted coverage_continuous check."""

from forkforensics.models import ForkCoverage, ForkInfo, ProbeResult, TimelineEntry
from forkforensics.report import build_report, rescue_commands


def _fork(full_name, depth=1):
    owner, _, name = full_name.partition("/")
    return ForkInfo(full_name=full_name, owner=owner, repo_name=name,
                    default_branch="main", created_at="2020-01-01T00:00:00Z",
                    pushed_at="2021-01-01T00:00:00Z", depth=depth)


def test_rescue_commands_are_correct_and_in_the_right_order():
    """These are shown to a user as copy-paste-able git commands - a wrong
    or reordered step here fails silently for anyone who trusts the tool
    and pastes them one by one without reading closely."""
    commands = rescue_commands("acme", "widget", "deadbeef", "https://github.com/me/rescue.git")

    assert commands == [
        "git init rescue-widget",
        "cd rescue-widget",
        "git remote add origin https://github.com/acme/widget.git",
        "git fetch origin deadbeef",
        "git checkout -b rescue/deadbeef FETCH_HEAD",
        "git remote add rescue https://github.com/me/rescue.git",
        "git push rescue rescue/deadbeef",
    ]
    # every step operates on the SAME branch name and the same rescue repo,
    # or a copy-pasted sequence would push the wrong ref
    assert "rescue/deadbeef" in commands[4]
    assert "rescue/deadbeef" in commands[6]


def test_coverage_continuous_is_true_when_there_are_no_gaps():
    cov = ForkCoverage(fork=_fork("acme/widget"), oldest_commit_sha="a" * 40,
                       oldest_commit_date="2018-01-01T00:00:00Z", commit_count=10)
    timeline = [
        TimelineEntry(date="2018-01-01T00:00:00Z", sha="a" * 40, source="fork:acme/widget"),
        TimelineEntry(date="2026-01-01T00:00:00Z", sha="", source="fork-head:acme/widget"),
    ]

    report = build_report("acme", "widget", [cov], timeline, [])

    assert report["coverage_continuous"] is True
    assert report["gaps"] == []


def test_coverage_continuous_is_false_when_a_real_gap_exists():
    """Counter-proof for the test above: a fork that only covers a narrow
    window, with archive activity on both sides it doesn't reach, must
    leave gaps and coverage_continuous=False - inverting this check would
    tell users a repo is fully recovered when it isn't."""
    cov = ForkCoverage(fork=_fork("acme/widget"), oldest_commit_sha="a" * 40,
                       oldest_commit_date="2019-06-01T00:00:00Z", commit_count=10)
    timeline = [
        TimelineEntry(date="2018-01-01T00:00:00Z", sha="b" * 40, source="archive"),
        TimelineEntry(date="2019-06-01T00:00:00Z", sha="a" * 40, source="fork:acme/widget"),
    ]

    report = build_report("acme", "widget", [cov], timeline, [])

    assert report["coverage_continuous"] is False
    assert len(report["gaps"]) == 1
    assert report["gaps"][0] == {"from": "2018-01-01", "to": "2019-06-01"}


def test_at_risk_shas_carry_their_own_rescue_commands():
    probe = ProbeResult(sha="c" * 40, reachable_from_current_refs=False, api_alive=True,
                        raw_alive=True, git_fetch_alive=True, checked_at="now")

    report = build_report("acme", "widget", [], [], [probe])

    assert len(report["at_risk_shas"]) == 1
    entry = report["at_risk_shas"][0]
    assert entry["sha"] == "c" * 40
    assert entry["rescue_commands"][3] == f"git fetch origin {'c' * 40}"


def test_a_reachable_probe_result_is_not_listed_as_at_risk():
    probe = ProbeResult(sha="d" * 40, reachable_from_current_refs=True, api_alive=True,
                        raw_alive=True, git_fetch_alive=True, checked_at="now")

    report = build_report("acme", "widget", [], [], [probe])

    assert report["at_risk_shas"] == []


def test_fork_ranking_preserves_input_order_and_fields():
    """build_report must not silently drop or reorder fork_ranking - it's
    the entire basis of _build_verdict's "best fork" choice."""
    cov1 = ForkCoverage(fork=_fork("a/deep"), oldest_commit_sha="1" * 40,
                        oldest_commit_date="2015-01-01T00:00:00Z", commit_count=100)
    cov2 = ForkCoverage(fork=_fork("b/shallow"), oldest_commit_sha="2" * 40,
                        oldest_commit_date="2020-01-01T00:00:00Z", commit_count=5)

    report = build_report("acme", "widget", [cov1, cov2], [], [])

    assert [f["full_name"] for f in report["fork_ranking"]] == ["a/deep", "b/shallow"]
    assert report["fork_ranking"][0]["oldest_commit_date"] == "2015-01-01T00:00:00Z"


def test_report_includes_owner_repo_and_a_generated_timestamp():
    report = build_report("acme", "widget", [], [], [])

    assert report["owner"] == "acme"
    assert report["repo"] == "widget"
    assert report["generated_at"]  # non-empty, real timestamp
