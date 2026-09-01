"""Shared dataclasses, all JSON-serializable via dataclasses.asdict."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ForkInfo:
    full_name: str
    owner: str
    repo_name: str
    default_branch: str
    created_at: str
    pushed_at: str
    depth: int  # 0 = original repo, 1 = direct fork, 2 = fork-of-fork


@dataclass
class ForkCoverage:
    fork: ForkInfo
    oldest_commit_sha: str | None = None
    oldest_commit_date: str | None = None
    commit_count: int | None = None
    error: str | None = None


@dataclass
class ArchiveEvent:
    ts: str
    actor: str
    ref: str
    before_sha: str | None
    head_sha: str | None
    commit_shas: list[str] = field(default_factory=list)
    source_hour_file: str = ""


@dataclass
class ProbeResult:
    sha: str
    reachable_from_current_refs: bool
    api_alive: bool
    raw_alive: bool
    git_fetch_alive: bool
    checked_at: str
    # content preview - already downloaded to verify api_alive
    # (client.get_commit), previously discarded after the boolean
    # check; used to decide WHETHER it's worth recovering the commit,
    # not just that it's technically possible
    commit_message: str | None = None
    commit_author: str | None = None
    commit_date: str | None = None
    files_changed: int | None = None
    # 0 is a verifiable fact (the "parents" field of the API response is
    # empty), not an assumption: a commit with no parents carries no
    # history with it if recovered in isolation, by construction - unlike
    # a SHA with parent_count > 0, which is the tip of a real chain
    parent_count: int | None = None
    # file names (not just the count) - they really say WHAT is in the
    # commit before deciding whether to recover it, not just how many files
    changed_files: list[str] | None = None


@dataclass
class TimelineEntry:
    date: str
    sha: str
    source: str  # "fork:<name>" | "archive" | "current-history"
    at_risk: bool = False


@dataclass
class KnownRepo:
    repo_id: int
    full_name: str
    owner: str
    first_seen: str
    last_seen_active: str
    last_verified_alive: str | None = None
    next_check_due: str | None = None
    status: str = "active"  # active | gone_or_private | renamed


@dataclass
class Disappearance:
    repo_id: int
    full_name: str
    detected_date: str
    last_known_alive: str | None = None
    investigation_status: str = "pending"  # pending | running | done | error
    recoverable_summary: str | None = None
    disappearance_id: int | None = None
    # owner_gone: the owner account has also disappeared/been suspended ->
    #   almost certainly a genuine deletion, not just made private
    # owner_active: the owner is still alive -> genuinely ambiguous
    # unknown: the check was not performed or failed
    owner_status: str = "unknown"


@dataclass
class JobStatus:
    job_id: str
    owner: str
    repo: str
    date_from: str
    date_to: str
    status: str = "queued"  # queued | running | done | error
    current_phase: str = ""
    phase_detail: str = ""
    progress_pct: float = 0.0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
