from pathlib import Path

from forkforensics.archive_filter import (iter_all_repo_sightings, normalize_push_event,
                                        process_hour_file)

FIXTURES = Path(__file__).parent / "fixtures"


def test_process_hour_file_current_schema_matches_target():
    events = process_hour_file(FIXTURES / "sample_hour_2023.json.gz",
                               target_full_name="acme/widget")
    assert len(events) == 1
    assert events[0].head_sha == "bbb222"
    assert events[0].before_sha == "aaa111"


def test_process_hour_file_ignores_non_matching_repo():
    events = process_hour_file(FIXTURES / "sample_hour_2023.json.gz",
                               target_full_name="other/repo")
    assert len(events) == 1
    assert events[0].head_sha == "ddd444"


def test_process_hour_file_ignores_watch_event():
    events = process_hour_file(FIXTURES / "sample_hour_2023.json.gz",
                               target_full_name="acme/widget")
    assert all(e.head_sha != "" for e in events)
    assert len(events) == 1  # only the PushEvent, not the WatchEvent


def test_process_hour_file_survives_corrupt_line():
    events = process_hour_file(FIXTURES / "sample_hour_2023.json.gz",
                               target_full_name="acme/widget")
    assert isinstance(events, list)  # didn't raise an exception on the corrupt line


def test_legacy_schema_shas_extraction():
    events = process_hour_file(FIXTURES / "sample_hour_2013_legacy_schema.json.gz",
                               target_full_name="acme/widget")
    assert len(events) == 1
    assert events[0].commit_shas == ["eee555"]


def test_normalize_push_event_returns_none_for_non_push():
    ev = normalize_push_event({"type": "IssuesEvent", "repo": {"name": "a/b"}})
    assert ev is None


def test_iter_all_repo_sightings_includes_non_push_events():
    sightings = dict(iter_all_repo_sightings(FIXTURES / "sample_hour_2023.json.gz"))
    assert sightings[42] == "acme/widget"
    assert sightings[99] == "other/repo"
    assert len(sightings) == 2  # dedup by repo_id despite 2 events on repo 42


def test_iter_all_repo_sightings_legacy_schema():
    sightings = dict(iter_all_repo_sightings(FIXTURES / "sample_hour_2013_legacy_schema.json.gz"))
    assert sightings[7] == "acme/widget"
