"""Actually executes the command sequence shown in report.rescue_commands,
instead of leaving it as something to copy by hand: git init -> remote add
-> fetch the orphan SHA -> checkout - the same technique already verified
in survival_probe.check_git_fetch_alive (uses the same command), here into
a persistent folder instead of a throwaway tempdir. Stays purely local: no
repository gets created on the user's GitHub, no push - just a local clone
with the rescued commit inside, and the user decides what to do with it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCB = Callable[[str, float | None], None]  # (raw line, 0-1 percentage or None)

_PERCENT_RE = re.compile(r"(\d{1,3})%")

RESCUE_TIMEOUT = 60
# verified live on a real fork: over 2.3 GB and still growing after
# 10+ minutes - a fork with years of history (especially with data, not
# just code) can weigh far more than a single commit would suggest.
# Generous margin rather than an optimistic timeout that fails halfway
# through downloading a large but otherwise legitimate fork.
CLONE_TIMEOUT = 3600


_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,100}\Z")


class RescueError(Exception):
    """Message already written to be shown to the user as-is."""


def _validate_sha(sha: str) -> str:
    """SHAs reach here from GH Archive payloads - third-party JSON that
    nothing else validates. Two reasons this matters: git's positional ref
    slot is option-parsed (a value starting with `--` is read as a flag,
    not a ref), and the value is interpolated into the destination folder
    name, where separators would escape dest_root."""
    if not _SHA_RE.match(sha or ""):
        raise RescueError(f"not a valid commit SHA: {sha!r}")
    return sha


def _validate_name(value: str, what: str) -> str:
    if not _NAME_RE.match(value or ""):
        raise RescueError(f"not a valid {what}: {value!r}")
    return value


def _contained(dest_root: Path, local_path: Path) -> Path:
    """Belt-and-braces after the component validation above: the folder we
    are about to create must genuinely sit inside dest_root.

    Checks the resolved LOCAL_PATH itself, not just its final component: an
    earlier version rebuilt the path as `root / local_path.name`, which
    silently discarded any traversal already baked into local_path's
    directory portion (e.g. dest_root/"safe"/".."/".."/"outside" resolves
    to a path outside root, but its .name alone is just "outside" - the
    check passed on that name with the escape already invisible). Harmless
    today only because both call sites build local_path as a single flat
    component; this makes the check meaningful for any future caller too."""
    root = dest_root.resolve()
    resolved = local_path.resolve()
    if not resolved.is_relative_to(root):
        raise RescueError(f"refusing to write outside {root}")
    return resolved


@dataclass
class RescueResult:
    local_path: Path
    branch: str


@dataclass
class CloneResult:
    local_path: Path


def _run(args: list[str], timeout: int = RESCUE_TIMEOUT) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise RescueError(
            "'git' command not found - install Git and make sure it's on the PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RescueError(f"command timed out after {timeout}s: {' '.join(args)}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RescueError(stderr or f"command failed: {' '.join(args)}")


def rescue_sha_locally(owner: str, repo: str, sha: str, dest_root: Path,
                      remote_url: str | None = None) -> RescueResult:
    """Rebuilds the orphan commit <sha> locally in a dedicated folder under
    dest_root. Fails with RescueError (message already ready to show the
    user) if git is missing, the folder already exists, or one of the
    steps fails (e.g. the SHA is no longer alive in the meantime).

    remote_url is overridable (defaults to https://github.com/{owner}/{repo}.git)
    only for integration tests: lets you point at a real local repository
    instead of github.com, to verify the whole sequence against a real git
    instead of a mocked subprocess."""
    _validate_sha(sha)
    _validate_name(owner, "owner")
    _validate_name(repo, "repository name")

    local_path = _contained(dest_root, dest_root / f"{owner}_{repo}_{sha[:10]}")
    if local_path.exists():
        raise RescueError(f"folder already exists: {local_path}")
    branch = f"rescue/{sha}"
    url = remote_url or f"https://github.com/{owner}/{repo}.git"

    local_path.mkdir(parents=True)
    try:
        _run(["git", "init", "--quiet", str(local_path)])
        _run(["git", "-C", str(local_path), "remote", "add", "origin", url])
        # "--" so the SHA can never be parsed as an option
        _run(["git", "-C", str(local_path), "fetch", "origin", "--", sha])
        _run(["git", "-C", str(local_path), "checkout", "-b", branch, "FETCH_HEAD"])
    except Exception:
        # don't leave a half-built folder behind: it would make every later
        # retry fail with "folder already exists" instead of retrying
        shutil.rmtree(local_path, ignore_errors=True)
        raise
    return RescueResult(local_path=local_path, branch=branch)


def clone_fork_locally(fork_full_name: str, dest_root: Path,
                       progress_cb: ProgressCB | None = None,
                       remote_url: str | None = None) -> CloneResult:
    """Actually clones the fork (usually the real source of the recovery: unlike
    a single orphan SHA, a fork brings along the entire commit chain built
    on top of the starting point).

    A fork with years of history can weigh several GB (verified live: over
    2.3 GB and still growing) - without a way to know HOW FAR ALONG it is,
    the only signal for the user was a fixed "this may take a while" text.
    git clone --progress writes REAL progress to stderr, updating the same
    line with \\r instead of a \\n per line - it must be read character by
    character, a normal readline() would get stuck waiting for a \\n that
    never comes on a progress line until the phase changes."""
    owner, _, repo = fork_full_name.partition("/")
    _validate_name(owner, "fork owner")
    _validate_name(repo, "fork repository name")
    local_path = _contained(dest_root, dest_root / f"{owner}_{repo}_fork")
    if local_path.exists():
        raise RescueError(f"folder already exists: {local_path}")
    url = remote_url or f"https://github.com/{fork_full_name}.git"

    try:
        if progress_cb is None:
            _run(["git", "clone", url, str(local_path)], timeout=CLONE_TIMEOUT)
            return CloneResult(local_path=local_path)

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        args = ["git", "clone", "--progress", url, str(local_path)]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    env=env, text=True, bufsize=1)
        except FileNotFoundError as exc:
            raise RescueError(
                "'git' command not found - install Git and make sure it's on the PATH") from exc

        stderr_lines: list[str] = []

        def _read_progress() -> None:
            buf = ""
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    if buf.strip():
                        stderr_lines.append(buf.strip())
                        match = _PERCENT_RE.search(buf)
                        pct = int(match.group(1)) / 100.0 if match else None
                        progress_cb(buf.strip(), pct)
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                stderr_lines.append(buf.strip())

        reader = threading.Thread(target=_read_progress, daemon=True)
        reader.start()
        try:
            returncode = proc.wait(timeout=CLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RescueError(f"clone timed out after {CLONE_TIMEOUT}s")
        reader.join(timeout=5)

        if returncode != 0:
            raise RescueError("\n".join(stderr_lines[-5:]) or "git clone failed")
        return CloneResult(local_path=local_path)
    except Exception:
        # same reasoning as rescue_sha_locally: git clone creates the
        # destination directory before it can fail partway through (a
        # timeout, a network error, an invalid repo) - without this, that
        # partial clone permanently blocks every retry with "folder already
        # exists" instead of allowing one.
        shutil.rmtree(local_path, ignore_errors=True)
        raise
