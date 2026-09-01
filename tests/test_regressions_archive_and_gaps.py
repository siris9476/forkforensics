"""Regression tests for two silent-wrong-answer bugs: archive hours that
were re-parsed forever, and coverage gaps that were reported where the data
actually showed continuous coverage."""

from forkforensics.cache import CacheManager
from forkforensics.models import TimelineEntry
from forkforensics.timeline import find_gaps


def test_hour_is_marked_processed_even_without_a_prior_download_row(tmp_path):
    """fetch_hour() returns early (without calling mark_hour_downloaded)
    when the .json.gz is already on disk, so for those hours no
    archive_hours row exists. mark_hour_processed used to be a bare UPDATE,
    which matched nothing and reported no error - is_hour_processed then
    never returned True and every run re-parsed the same ~30k-row hours
    from scratch, making the resume mechanism useless."""
    cache = CacheManager(tmp_path / "t.db")

    cache.mark_hour_processed("2024-06-01", 12, 100, "2024-06-01")

    assert cache.is_hour_processed("2024-06-01", 12) is True
    cache.close()


def test_marking_processed_twice_keeps_the_latest_counts(tmp_path):
    """The upsert must update in place, not insert a duplicate row (the
    table is keyed on (date, hour))."""
    cache = CacheManager(tmp_path / "t.db")

    cache.mark_hour_downloaded("2024-06-02", 5, 999)
    cache.mark_hour_processed("2024-06-02", 5, 10, "first")
    cache.mark_hour_processed("2024-06-02", 5, 42, "second")

    rows = list(cache._conn.execute(
        "SELECT event_count, processed_at FROM archive_hours WHERE date=? AND hour=?",
        ("2024-06-02", 5)))
    assert len(rows) == 1
    assert rows[0]["event_count"] == 42
    assert rows[0]["processed_at"] == "second"
    cache.close()


def test_fork_head_without_a_matching_fork_start_still_counts_as_coverage():
    """Regression: build_unified_timeline emits a "fork-head" entry (the
    fork's pushed_at) even when its oldest commit could not be computed, so
    that fork has an END with no START. find_gaps used to iterate fork
    starts only, silently discarding those points - a date known to be
    covered dropped out of the analysis and the whole span around it was
    reported as one large gap that did not exist."""
    timeline = [
        TimelineEntry(date="2020-01-01", sha="a", source="archive"),
        TimelineEntry(date="2020-06-01", sha="", source="fork-head:someone/repo"),
        TimelineEntry(date="2021-01-01", sha="b", source="archive"),
    ]

    gaps = find_gaps(timeline)

    # the 2020-06-01 point is real coverage: it splits the span in two
    # rather than being swallowed into a single fabricated 2020->2021 gap
    assert gaps == [("2020-01-01", "2020-06-01"), ("2020-06-01", "2021-01-01")]


def test_a_complete_fork_interval_is_still_treated_as_one_covered_span():
    """Counter-proof for the fix above: a fork WITH both a start and an end
    must still collapse into a single covered interval, not two points."""
    timeline = [
        TimelineEntry(date="2018-01-01", sha="x", source="fork:someone/repo"),
        TimelineEntry(date="2022-01-01", sha="", source="fork-head:someone/repo"),
    ]

    assert find_gaps(timeline) == []
