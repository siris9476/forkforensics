"""Builds the report context - used both by the Jinja2 template (HTML)
and by the JSON export, a single source of truth."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from .models import ForkCoverage, ProbeResult, TimelineEntry
from .timeline import find_gaps, identify_at_risk


def rescue_commands(owner: str, repo: str, sha: str, rescue_repo_url: str) -> list[str]:
    """The same commands used by hand to rescue a real vanished repository."""
    return [
        f"git init rescue-{repo}",
        f"cd rescue-{repo}",
        f"git remote add origin https://github.com/{owner}/{repo}.git",
        f"git fetch origin {sha}",
        f"git checkout -b rescue/{sha} FETCH_HEAD",
        f"git remote add rescue {rescue_repo_url}",
        f"git push rescue rescue/{sha}",
    ]


def build_report(owner: str, repo: str, coverages: list[ForkCoverage],
                 timeline: list[TimelineEntry], probe_results: list[ProbeResult]) -> dict:
    gaps = find_gaps(timeline)
    at_risk = identify_at_risk(probe_results)

    return {
        "owner": owner,
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fork_ranking": [
            {**asdict(c.fork), "oldest_commit_sha": c.oldest_commit_sha,
             "oldest_commit_date": c.oldest_commit_date,
             "commit_count": c.commit_count, "error": c.error}
            for c in coverages
        ],
        "timeline": [asdict(e) for e in timeline],
        "gaps": [{"from": a, "to": b} for a, b in gaps],
        "at_risk_shas": [
            {**asdict(p), "rescue_commands": rescue_commands(
                owner, repo, p.sha, f"https://github.com/<your-username>/{repo}-rescue.git")}
            for p in at_risk
        ],
        "coverage_continuous": len(gaps) == 0,
    }
