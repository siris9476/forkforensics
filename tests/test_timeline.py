from forkforensics.models import ForkCoverage, ForkInfo, ProbeResult
from forkforensics.timeline import build_unified_timeline, find_gaps, identify_at_risk


def _fork(name, oldest_date, pushed_at=None):
    """pushed_at defaults to oldest_date - it degenerates to an isolated
    point (no interval), preserving the "single-point observation"
    semantics for tests that don't explicitly reason about a fork with a
    known, distinct start and end."""
    return ForkCoverage(
        fork=ForkInfo(full_name=name, owner="x", repo_name="y", default_branch="main",
                     created_at="2020-01-01T00:00:00Z", pushed_at=pushed_at or oldest_date, depth=1),
        oldest_commit_sha="sha_" + name, oldest_commit_date=oldest_date, commit_count=10,
    )


def _probe(sha, reachable, api_alive=False, raw_alive=False, git_fetch_alive=False):
    return ProbeResult(sha=sha, reachable_from_current_refs=reachable, api_alive=api_alive,
                       raw_alive=raw_alive, git_fetch_alive=git_fetch_alive, checked_at="now")


def test_build_unified_timeline_marks_at_risk():
    coverages = [_fork("fork/a", "2020-01-01T00:00:00Z")]
    probes = [_probe("sha_fork/a", reachable=False, api_alive=True)]
    timeline = build_unified_timeline(coverages, [], probes)
    assert len(timeline) == 1
    assert timeline[0].at_risk is True


def test_build_unified_timeline_not_at_risk_when_reachable():
    coverages = [_fork("fork/a", "2020-01-01T00:00:00Z")]
    probes = [_probe("sha_fork/a", reachable=True, api_alive=True)]
    timeline = build_unified_timeline(coverages, [], probes)
    assert timeline[0].at_risk is False


def test_find_gaps_detects_missing_days():
    coverages = [_fork("fork/a", "2020-01-01"), _fork("fork/b", "2020-01-05")]
    timeline = build_unified_timeline(coverages, [], [])
    gaps = find_gaps(timeline)
    assert ("2020-01-01", "2020-01-05") in gaps


def test_find_gaps_no_gap_for_consecutive_days():
    coverages = [_fork("fork/a", "2020-01-01"), _fork("fork/b", "2020-01-02")]
    timeline = build_unified_timeline(coverages, [], [])
    assert find_gaps(timeline) == []


def test_find_gaps_does_not_flag_a_forks_own_continuous_span():
    """Regression: a fork with a known start and end (pushed_at different
    from the oldest commit) is a continuous chain by construction -
    before the fix, the two isolated points (start, end) were compared as
    if they belonged to disconnected sources, flagging the fork's entire
    internal history as a "gap". Found live on octocat/data."""
    coverages = [_fork("fork/a", "2018-06-15", pushed_at="2025-06-19")]
    timeline = build_unified_timeline(coverages, [], [])
    assert find_gaps(timeline) == []


def test_find_gaps_reports_only_the_real_residual_after_a_forks_own_end():
    """The fork covers 2018-06-15 -> 2025-06-19 (an interval, not two
    disconnected points); a second isolated observation at 2026-03-25
    leaves uncovered only the real residual after the fork's end, not the
    entire history since 2018."""
    coverages = [_fork("fork/a", "2018-06-15", pushed_at="2025-06-19"),
                _fork("fork/b", "2026-03-25", pushed_at="2026-03-25")]
    timeline = build_unified_timeline(coverages, [], [])
    gaps = find_gaps(timeline)
    assert gaps == [("2025-06-19", "2026-03-25")]


def test_find_gaps_merges_overlapping_fork_intervals():
    """Two forks with overlapping intervals together cover a wider span
    with no gap between them, even though neither covers it entirely on
    its own."""
    coverages = [_fork("fork/a", "2018-01-01", pushed_at="2020-01-01"),
                _fork("fork/b", "2019-06-01", pushed_at="2021-01-01")]
    timeline = build_unified_timeline(coverages, [], [])
    assert find_gaps(timeline) == []


def test_identify_at_risk_excludes_reachable():
    probes = [
        _probe("s1", reachable=True, api_alive=True, raw_alive=True),
        _probe("s2", reachable=False, api_alive=True, raw_alive=False),
        _probe("s3", reachable=False, api_alive=False, raw_alive=False),
    ]
    at_risk = identify_at_risk(probes)
    assert [p.sha for p in at_risk] == ["s2"]


def test_identify_at_risk_includes_git_fetch_only_signal():
    """An orphan may no longer respond to the commit/raw API but still be
    recoverable via direct fetch - it must still be flagged."""
    probes = [
        _probe("s1", reachable=False, api_alive=False, raw_alive=False, git_fetch_alive=True),
    ]
    at_risk = identify_at_risk(probes)
    assert [p.sha for p in at_risk] == ["s1"]
