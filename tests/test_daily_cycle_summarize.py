"""_summarize() produces the recoverable_summary text shown in the Feed UI
(desktop/widgets/disappearance_card.py, via a Badge). Every existing
test_daily_cycle.py test monkeypatches investigate() to always return the
same fixed empty report ({"fork_ranking": [], "at_risk_shas": []}), so only
the "no recovery path found" branch of this function ever ran - the other
two, which cover the actual success cases, could each be deleted and no
test would notice."""

from forkforensics.daily_cycle import _summarize


def test_reports_the_best_fork_when_one_covers_the_history():
    report = {
        "fork_ranking": [{"full_name": "rescuer/data", "oldest_commit_date": "2018-06-15"}],
        "at_risk_shas": [],
    }

    assert _summarize(report) == (
        "history preserved in fork rescuer/data up to 2018-06-15; 0 at-risk SHAs found")


def test_mentions_at_risk_shas_alongside_a_usable_fork():
    report = {
        "fork_ranking": [{"full_name": "rescuer/data", "oldest_commit_date": "2018-06-15"}],
        "at_risk_shas": [{"sha": "a" * 40}, {"sha": "b" * 40}],
    }

    assert "2 at-risk SHAs found" in _summarize(report)


def test_reports_orphan_shas_when_no_fork_is_usable():
    """The fork exists in the ranking but never got a usable commit date
    (e.g. the fork itself errored) - must fall through to the at-risk-only
    branch, not treat an incomplete fork entry as "best"."""
    report = {
        "fork_ranking": [{"full_name": "dead/fork", "oldest_commit_date": None, "error": "404"}],
        "at_risk_shas": [{"sha": "c" * 40}],
    }

    assert _summarize(report) == "no useful fork, but 1 orphan SHAs still alive found"


def test_no_fork_and_no_at_risk_shas_reports_nothing_recoverable():
    report = {"fork_ranking": [], "at_risk_shas": []}

    assert _summarize(report) == "no recovery path found"


def test_missing_keys_are_treated_as_empty_not_a_crash():
    """Defensive: an unexpected report shape must degrade to the same
    "nothing recoverable" message, not raise a KeyError shown to the user."""
    assert _summarize({}) == "no recovery path found"
