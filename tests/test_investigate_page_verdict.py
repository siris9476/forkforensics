"""_build_verdict extracts the few fields that actually matter (which
fork/SHA to use to rebuild everything) as dynamic label:value rows - not
fixed text, which is why the return value is structured (status, variant,
rows, best_fork) instead of a ready-made sentence."""

from desktop.pages.investigate_page import _build_verdict


def _fork(full_name, oldest_date, commit_count=10, error=None, oldest_commit_sha=None,
         pushed_at=None):
    return {"full_name": full_name, "oldest_commit_date": oldest_date,
           "commit_count": commit_count, "error": error,
           "oldest_commit_sha": oldest_commit_sha, "pushed_at": pushed_at}


def _rows_dict(rows):
    return dict(rows)


def test_best_fork_with_continuous_coverage_is_marked_complete():
    report = {
        "fork_ranking": [_fork("rescuer/data", "2019-01-01")],
        "coverage_continuous": True,
        "gaps": [],
        "at_risk_shas": [],
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "recovered"
    assert status == "Full coverage"
    assert best["full_name"] == "rescuer/data"
    assert _rows_dict(rows)["Best fork"].startswith("rescuer/data")
    assert _rows_dict(rows)["Coverage"] == "continuous"


def test_best_fork_with_gaps_and_independent_at_risk_sha_says_it_may_extend():
    """The gap is BEFORE the fork's start (2017 vs a fork starting in
    2019) - the fork can't cover it by construction, so it must be shown
    as a real gap, not as a detection artifact."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2019-01-01", oldest_commit_sha="root123")],
        "coverage_continuous": False,
        "gaps": [{"from": "2017-01-01", "to": "2017-02-01"}],
        "at_risk_shas": [{"sha": "abc123"}],  # different from the fork's oldest commit
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "ambiguous"
    assert best["full_name"] == "rescuer/data"
    assert "extend coverage" in _rows_dict(rows)["At-risk SHA"]
    assert _rows_dict(rows)["Coverage"] == "1 real gap: 2017-01-01 → 2017-02-01"


def test_gap_bridged_by_fork_is_not_shown_as_missing_content():
    """Regression: a gap that starts at the same point (or after) the best
    fork's history begins is NOT missing content - a git repository is a
    continuous chain by construction, the "gap" is just the absence of a
    second independent reference point in between. Showing the raw dates
    (e.g. "2018 -> 2026") makes it look like an entire period is lost when
    the fork actually covers it in full. Verified live on
    octocat/data."""
    # oldest_commit_date with a time (the API's real format), gaps[].from
    # without one - the regression was precisely in comparing the two
    # strings as they were ("2018-06-15" < "2018-06-15T09:46:39Z" fails
    # the comparison even when the dates are actually the same day)
    report = {
        "fork_ranking": [_fork("rescuer/data", "2018-06-15T09:46:39Z",
                              oldest_commit_sha="e878af6978")],
        "coverage_continuous": False,
        "gaps": [{"from": "2018-06-15", "to": "2026-03-26"}],
        "at_risk_shas": [],
    }
    status, variant, rows, best = _build_verdict(report)
    coverage = _rows_dict(rows)["Coverage"]
    assert "2018-06-15 → 2026-03-26" not in coverage
    assert "no content missing" in coverage


def test_at_risk_sha_matching_fork_root_without_parent_count_is_only_a_hint():
    """Without parent_count (probes from before that check existed) it
    falls back to the weakest clue - matches a fork's oldest point - and
    it must be stated clearly that this is NOT confirmed, only likely."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2018-06-15", oldest_commit_sha="e878af6978")],
        "coverage_continuous": False,
        "gaps": [{"from": "2018-06-15", "to": "2026-03-26"}],
        "at_risk_shas": [{"sha": "e878af6978"}],  # same sha as the fork, parent_count unknown
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "recovered"
    assert best["full_name"] == "rescuer/data"
    assert "not confirmed" in _rows_dict(rows)["At-risk SHA"]


def test_at_risk_sha_confirmed_root_commit_via_parent_count():
    """Regression: parent_count=0 is a verifiable fact (the API response's
    "parents" field), not a hypothesis - a commit with no parents doesn't
    carry any history with it if recovered in isolation, by construction.
    Verified live on octocat/data: recovering that SHA alone gave a
    single commit (README+LICENSE, 0 parents), not the fork's other 1988
    commits."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2018-06-15", oldest_commit_sha="e878af6978")],
        "coverage_continuous": False,
        "gaps": [{"from": "2018-06-15", "to": "2026-03-26"}],
        "at_risk_shas": [{"sha": "e878af6978", "parent_count": 0}],
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "recovered"
    assert best["full_name"] == "rescuer/data"
    assert "confirmed root commit (0 parents)" in _rows_dict(rows)["At-risk SHA"]


def test_best_fork_with_unfillable_gap_is_honest_about_the_limit():
    """Gap BEFORE the fork's start (2017 vs a fork from 2019) - can't be
    covered by construction, must be shown as real."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2019-01-01")],
        "coverage_continuous": False,
        "gaps": [{"from": "2017-01-01", "to": "2017-02-01"}],
        "at_risk_shas": [],
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "ambiguous"
    assert "Maximum recoverable" in status
    assert best["full_name"] == "rescuer/data"
    assert "real gap" in _rows_dict(rows)["Coverage"]


def test_fork_that_stopped_updating_before_the_gap_ends_leaves_a_real_residual():
    """Regression: bridged_by_fork only checked that the fork started
    before the gap, not that its coverage reached the gap's END - the fork
    can stop being updated months earlier. Found live on octocat/data:
    rescuer/data starts on 2018-06-15 (before the gap) but its last
    commit (pushed_at) is from 2025-06-19, while the gap goes up to
    2026-03-25 - the message said "no content missing ... up to today",
    contradicting the "Partial coverage" headline just above it and hiding
    that 9 months were genuinely missing."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2018-06-15T09:46:39Z",
                              oldest_commit_sha="e878af6978",
                              pushed_at="2025-06-19T12:00:00Z")],
        "coverage_continuous": False,
        "gaps": [{"from": "2018-06-15", "to": "2026-03-25"}],
        "at_risk_shas": [{"sha": "orphan1", "parent_count": 1}],
    }
    status, variant, rows, best = _build_verdict(report)
    coverage = _rows_dict(rows)["Coverage"]
    assert "no content missing" not in coverage
    assert "2025-06-19" in coverage  # where the fork actually stops
    assert "2026-03-25" in coverage  # where the gap actually reaches
    assert variant == "ambiguous"


def test_fork_that_covers_past_the_gap_end_is_genuinely_complete():
    """Counter-proof: if pushed_at is after the gap's end, coverage is
    genuinely total - the "no content missing" message stays correct in
    this case (must not be broken by the previous fix)."""
    report = {
        "fork_ranking": [_fork("rescuer/data", "2018-06-15T09:46:39Z",
                              oldest_commit_sha="e878af6978",
                              pushed_at="2026-04-01T12:00:00Z")],
        "coverage_continuous": False,
        "gaps": [{"from": "2018-06-15", "to": "2026-03-25"}],
        "at_risk_shas": [],
    }
    status, variant, rows, best = _build_verdict(report)
    assert "no content missing" in _rows_dict(rows)["Coverage"]


def test_no_usable_fork_but_at_risk_shas_points_only_there():
    report = {
        "fork_ranking": [_fork("dead/fork", None, error="404")],
        "coverage_continuous": False,
        "gaps": [],
        "at_risk_shas": [{"sha": "abc123"}],
    }
    status, variant, rows, best = _build_verdict(report)
    assert variant == "ambiguous"
    assert "No useful fork" in status
    assert best is None  # no usable fork to offer for cloning


def test_nothing_recoverable_is_marked_lost():
    report = {"fork_ranking": [], "coverage_continuous": False, "gaps": [], "at_risk_shas": []}
    status, variant, rows, best = _build_verdict(report)
    assert variant == "lost"
    assert "No recovery path found" in status
    assert best is None
