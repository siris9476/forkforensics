"""investigate() as orchestrator: a targeted test on the choice of
candidate SHAs to probe, not on the whole pipeline (already covered
piecemeal by the individual modules' tests)."""

from datetime import date
from pathlib import Path

import forkforensics.investigate as inv
from forkforensics.models import ArchiveEvent, ProbeResult


class _FakeClient:
    def get_refs(self, owner, repo):
        return []


def _probe_stub(probed_shas):
    def _check_alive(client, cache, owner, repo, sha, refs):
        probed_shas.append(sha)
        return ProbeResult(sha=sha, reachable_from_current_refs=False,
                           api_alive=False, raw_alive=False, git_fetch_alive=False,
                           checked_at="2026-01-01T00:00:00Z")
    return _check_alive


def _setup_no_forks(monkeypatch):
    monkeypatch.setattr(inv, "discover_forks", lambda *a, **k: [])
    monkeypatch.setattr(inv, "rank_forks", lambda coverages: coverages)


def test_before_sha_of_a_push_event_becomes_a_probe_candidate(tmp_path, monkeypatch):
    """Regression: before_sha (the tip of history BEFORE a push - on a
    reset/force-push it is exactly the orphan commit that carries the
    deleted chain with it) used to be discarded, only head_sha (which
    after a reset points to the NEW history, already reachable) became a
    candidate. Found while re-reading the actual manual recovery of the
    2025-06-19 -> 2026-03-26 gap in octocat/data: the best fork
    stopped at the "before", the official import resumed from the
    "after" - what filled the gap was the before_sha of the reset push,
    not the SHA of any fork."""
    _setup_no_forks(monkeypatch)
    event = ArchiveEvent(ts="2026-03-26T10:00:00Z", actor="someone", ref="refs/heads/main",
                         before_sha="deadbeef" * 5, head_sha="cafebabe" * 5)
    monkeypatch.setattr(inv, "build_hour_list", lambda d1, d2: [(d1.isoformat(), 0)])
    monkeypatch.setattr(inv, "fetch_range_threaded",
                        lambda hour_list, raw_dir, cache, concurrency=4: iter([Path("dummy.gz")]))
    monkeypatch.setattr(inv, "process_hour_file", lambda gz_path, target_full_name: [event])
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)

    probed = []
    monkeypatch.setattr(inv, "check_alive", _probe_stub(probed))

    inv.investigate(_FakeClient(), cache=None, owner="octocat", repo="data",
                    raw_dir=tmp_path, date_from=date(2026, 3, 26), date_to=date(2026, 3, 27))

    assert "deadbeef" * 5 in probed
    assert "cafebabe" * 5 in probed


def test_null_before_sha_of_a_new_branch_is_not_probed(tmp_path, monkeypatch):
    """before_sha = "0"*40 is GitHub's conventional value for the first
    push of a new branch (no prior history) - it is not a real SHA,
    probing it would waste an empty API call on every single branch
    creation in the analyzed range."""
    _setup_no_forks(monkeypatch)
    event = ArchiveEvent(ts="2026-03-26T10:00:00Z", actor="someone", ref="refs/heads/feature",
                         before_sha="0" * 40, head_sha="cafebabe" * 5)
    monkeypatch.setattr(inv, "build_hour_list", lambda d1, d2: [(d1.isoformat(), 0)])
    monkeypatch.setattr(inv, "fetch_range_threaded",
                        lambda hour_list, raw_dir, cache, concurrency=4: iter([Path("dummy.gz")]))
    monkeypatch.setattr(inv, "process_hour_file", lambda gz_path, target_full_name: [event])
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)

    probed = []
    monkeypatch.setattr(inv, "check_alive", _probe_stub(probed))

    inv.investigate(_FakeClient(), cache=None, owner="acme", repo="widget",
                    raw_dir=tmp_path, date_from=date(2026, 3, 26), date_to=date(2026, 3, 27))

    assert "0" * 40 not in probed
    assert "cafebabe" * 5 in probed
