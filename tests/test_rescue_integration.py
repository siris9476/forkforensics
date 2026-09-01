"""Real integration: forkforensics.rescue run against a REAL git binary and a
real local repository, not a mocked subprocess.run like in test_rescue.py.
Verifies that the init/remote/fetch/checkout sequence (and git clone)
ACTUALLY works, not just that it calls the right commands in the right
order - mocking subprocess.run could never have caught, for example, a
wrong git flag or a working-directory issue."""

import shutil
import subprocess

import pytest

from forkforensics.rescue import RescueError, clone_fork_locally, rescue_sha_locally

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="requires the 'git' binary on the PATH")


def _git(args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})


def _sha(cwd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_local_origin_with_an_orphan_commit(tmp_path):
    """A real local git repository: two commits, then a reset --hard that
    makes the second one an orphan (not reachable from any ref, but still
    in the object database until a gc happens - exactly the project's
    central phenomenon, reproduced locally instead of against
    github.com."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "--quiet"], cwd=origin)
    (origin / "a.txt").write_text("first version")
    _git(["add", "a.txt"], cwd=origin)
    _git(["commit", "--quiet", "-m", "first commit"], cwd=origin)

    (origin / "b.txt").write_text("content of the commit that will become an orphan")
    _git(["add", "b.txt"], cwd=origin)
    _git(["commit", "--quiet", "-m", "second commit (will become an orphan)"], cwd=origin)
    orphan_sha = _sha(origin)

    _git(["reset", "--quiet", "--hard", "HEAD~1"], cwd=origin)
    return origin, orphan_sha


def test_rescue_sha_locally_against_a_real_git_repository(tmp_path):
    origin, orphan_sha = _make_local_origin_with_an_orphan_commit(tmp_path)
    dest_root = tmp_path / "rescued"
    dest_root.mkdir()

    result = rescue_sha_locally("acme", "widget", orphan_sha, dest_root,
                                remote_url=str(origin))

    assert result.local_path.is_dir()
    assert (result.local_path / "b.txt").read_text() == "content of the commit that will become an orphan"
    assert _sha(result.local_path) == orphan_sha
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=result.local_path,
                            check=True, capture_output=True, text=True).stdout.strip()
    assert branch == result.branch


def test_rescue_sha_locally_against_a_real_repo_fails_for_a_sha_that_never_existed(tmp_path):
    origin, _orphan_sha = _make_local_origin_with_an_orphan_commit(tmp_path)
    dest_root = tmp_path / "rescued"
    dest_root.mkdir()

    with pytest.raises(RescueError):
        rescue_sha_locally("acme", "widget", "f" * 40, dest_root, remote_url=str(origin))


def test_clone_fork_locally_against_a_real_git_repository(tmp_path):
    origin, orphan_sha = _make_local_origin_with_an_orphan_commit(tmp_path)
    dest_root = tmp_path / "cloned"
    dest_root.mkdir()

    result = clone_fork_locally("acme/widget", dest_root, remote_url=str(origin))

    assert result.local_path.is_dir()
    assert (result.local_path / "a.txt").exists()
    # the reset --hard made the second commit an orphan: a normal clone
    # (not a fetch targeted at the SHA) only gets what the current refs
    # reach, so b.txt (the orphan commit) must NOT be there
    assert not (result.local_path / "b.txt").exists()
    assert _sha(result.local_path) != orphan_sha


def test_clone_fork_locally_with_progress_against_a_real_git_repository(tmp_path):
    """The same, but going through the progress_cb branch (subprocess.Popen
    + character-by-character reading) instead of the plain subprocess.run
    branch - both paths need to be verified against a real git."""
    origin, _orphan_sha = _make_local_origin_with_an_orphan_commit(tmp_path)
    dest_root = tmp_path / "cloned_progress"
    dest_root.mkdir()

    events = []
    result = clone_fork_locally("acme/widget", dest_root,
                                progress_cb=lambda line, pct: events.append((line, pct)),
                                remote_url=str(origin))

    assert result.local_path.is_dir()
    assert (result.local_path / "a.txt").exists()
    assert len(events) > 0  # git wrote at least one real progress line
