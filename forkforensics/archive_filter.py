"""Line-by-line parsing of GH Archive hourly files (JSON Lines, gzip).

GH Archive has had TWO schemas over its history:
  - legacy (up to and including 2014-12-31): "repository" field (not "repo"),
    payload.shas = [[sha, author, message, url], ...] for PushEvents.
  - current (from 2015-01-01): "repo" field with "name" = "owner/repo",
    payload.commits = [{"sha":..., "author":{...}, "message":...}, ...].

Parsing IMMEDIATELY discards any line that isn't of interest (different repo
or irrelevant event) without accumulating the whole file in memory.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from .models import ArchiveEvent

logger = logging.getLogger(__name__)

SCHEMA_CUTOVER = "2015-01-01"


def _repo_names_match(event_repo_name: str, target_full_name: str,
                      aliases: set[str]) -> bool:
    name = event_repo_name.lower()
    if name == target_full_name.lower():
        return True
    return name in {a.lower() for a in aliases}


def _extract_repo_field(raw: dict) -> tuple[str | None, int | None]:
    """Returns (full_name, repo_id) from an event, handling both schemas."""
    repo = raw.get("repo")
    if repo and isinstance(repo, dict) and repo.get("name"):
        return repo["name"], repo.get("id")
    legacy = raw.get("repository")
    if legacy and isinstance(legacy, dict):
        owner = legacy.get("owner")
        name = legacy.get("name")
        if owner and name:
            return f"{owner}/{name}", legacy.get("id")
    return None, None


def normalize_push_event(raw: dict) -> ArchiveEvent | None:
    """Normalizes a GH Archive event into an ArchiveEvent if it's a valid
    PushEvent; None otherwise (not a push, or missing repo)."""
    if raw.get("type") != "PushEvent":
        return None
    full_name, _repo_id = _extract_repo_field(raw)
    if not full_name:
        return None

    payload = raw.get("payload") or {}
    actor = (raw.get("actor") or {}).get("login", "")
    ref = payload.get("ref", "")
    ts = raw.get("created_at", "")

    shas: list[str] = []
    if "commits" in payload and isinstance(payload["commits"], list):
        shas = [c.get("sha") for c in payload["commits"] if c.get("sha")]
    elif "shas" in payload and isinstance(payload["shas"], list):
        # legacy: [sha, author, message, url] per item
        shas = [s[0] for s in payload["shas"] if s]

    before_sha = payload.get("before")
    head_sha = payload.get("head") or (shas[-1] if shas else None)

    return ArchiveEvent(
        ts=ts, actor=actor, ref=ref,
        before_sha=before_sha, head_sha=head_sha,
        commit_shas=shas,
    )


def extract_any_repo(raw: dict) -> tuple[str | None, int | None]:
    """For the general ingest (not just push): returns (full_name, repo_id)
    for ANY event type that has a valid repo/repository field."""
    return _extract_repo_field(raw)


def process_hour_file(gz_path: Path, target_full_name: str | None = None,
                      aliases: set[str] | None = None) -> list[ArchiveEvent]:
    """Filters an hourly file for PushEvents of a specific repo (used by
    the targeted investigation). If target_full_name is None, no filtering
    is applied (used by the general ingest via iter_all_repo_sightings)."""
    aliases = aliases or set()
    events: list[ArchiveEvent] = []
    n_lines = n_matched = n_corrupt = 0
    with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            n_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                n_corrupt += 1
                continue
            ev = normalize_push_event(raw)
            if ev is None:
                continue
            full_name, _ = _extract_repo_field(raw)
            if target_full_name and not _repo_names_match(full_name or "", target_full_name, aliases):
                continue
            ev.source_hour_file = gz_path.name
            events.append(ev)
            n_matched += 1
    if n_corrupt:
        logger.warning("%s: %d corrupt lines out of %d", gz_path.name, n_corrupt, n_lines)
    return events


def iter_all_repo_sightings(gz_path: Path) -> list[tuple[int, str]]:
    """For the general daily ingest: extracts (repo_id, full_name) from
    ANY event in the file, not just PushEvent - used to build the
    population of repos "with recent public activity"."""
    seen: dict[int, str] = {}
    with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            full_name, repo_id = extract_any_repo(raw)
            if full_name and repo_id is not None:
                seen[repo_id] = full_name
    return list(seen.items())
