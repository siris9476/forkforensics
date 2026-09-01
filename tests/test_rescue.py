from unittest.mock import MagicMock, patch

import pytest

from forkforensics.rescue import RescueError, clone_fork_locally, rescue_sha_locally


def _ok(returncode=0, stderr=b""):
    return MagicMock(returncode=returncode, stderr=stderr)


def test_rescue_sha_locally_runs_the_expected_git_sequence(tmp_path):
    with patch("forkforensics.rescue.subprocess.run", return_value=_ok()) as mock_run:
        result = rescue_sha_locally("octocat", "data", "abc123def456", tmp_path)

    assert result.local_path == tmp_path / "octocat_data_abc123def4"
    assert result.branch == "rescue/abc123def456"
    assert result.local_path.is_dir()

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert commands[0][:2] == ["git", "init"]
    assert "remote" in commands[1] and "origin" in commands[1]
    # "--" separates the ref from the options: git's positional ref slot is
    # option-parsed, so a SHA-shaped string starting with "--" would
    # otherwise be read as a flag
    assert commands[2][3:7] == ["fetch", "origin", "--", "abc123def456"]
    assert commands[3][3:5] == ["checkout", "-b"]


def test_rescue_sha_locally_raises_clear_error_when_git_missing(tmp_path):
    with patch("forkforensics.rescue.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(RescueError, match="git"):
            rescue_sha_locally("octocat", "data", "abc123d", tmp_path)


def test_rescue_sha_locally_raises_with_stderr_on_command_failure(tmp_path):
    with patch("forkforensics.rescue.subprocess.run",
              return_value=_ok(returncode=1, stderr=b"fatal: couldn't find remote ref abc123")):
        with pytest.raises(RescueError, match="couldn't find remote ref"):
            rescue_sha_locally("octocat", "data", "abc123d", tmp_path)


def test_rescue_sha_locally_refuses_to_overwrite_existing_folder(tmp_path):
    existing = tmp_path / "octocat_data_abc123def4"
    existing.mkdir()
    with pytest.raises(RescueError, match="already exists"):
        rescue_sha_locally("octocat", "data", "abc123def456", tmp_path)


def test_clone_fork_locally_runs_git_clone(tmp_path):
    with patch("forkforensics.rescue.subprocess.run", return_value=_ok()) as mock_run:
        result = clone_fork_locally("rescuer/data", tmp_path)

    assert result.local_path == tmp_path / "rescuer_data_fork"
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[:2] == ["git", "clone"]
    assert "https://github.com/rescuer/data.git" in cmd
    assert str(tmp_path / "rescuer_data_fork") in cmd


def test_clone_fork_locally_refuses_to_overwrite_existing_folder(tmp_path):
    existing = tmp_path / "rescuer_data_fork"
    existing.mkdir()
    with pytest.raises(RescueError, match="already exists"):
        clone_fork_locally("rescuer/data", tmp_path)


def test_clone_fork_locally_raises_with_stderr_on_failure(tmp_path):
    with patch("forkforensics.rescue.subprocess.run",
              return_value=_ok(returncode=1, stderr=b"fatal: repository not found")):
        with pytest.raises(RescueError, match="repository not found"):
            clone_fork_locally("rescuer/data", tmp_path)


class _FakeStderr:
    """git writes progress by updating the same line with \\r instead of
    \\n per line - the real reader reads character by character, this
    fake reproduces exactly that format for the test."""
    def __init__(self, text: str) -> None:
        self._chars = list(text)
        self._i = 0

    def read(self, _n: int = 1) -> str:
        if self._i >= len(self._chars):
            return ""
        ch = self._chars[self._i]
        self._i += 1
        return ch


class _FakePopen:
    def __init__(self, stderr_text: str, returncode: int = 0) -> None:
        self.stderr = _FakeStderr(stderr_text)
        self._returncode = returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self) -> None:
        pass


def test_clone_fork_locally_with_progress_cb_reports_real_percentages(tmp_path):
    """Regression/feature: without progress_cb the only signal was a fixed
    "this may take a while" text - with progress_cb (git's --progress) you
    get the real progress, line by line, including percentages."""
    stderr_text = ("Cloning into 'x'...\r"
                  "Receiving objects:  45% (450/1000), 12 MiB | 3 MiB/s\r"
                  "Receiving objects: 100% (1000/1000), 30 MiB | 3 MiB/s, done.\n"
                  "Resolving deltas: 100% (200/200), done.\n")
    fake_proc = _FakePopen(stderr_text)
    events = []

    with patch("forkforensics.rescue.subprocess.Popen", return_value=fake_proc):
        result = clone_fork_locally("rescuer/data", tmp_path,
                                    progress_cb=lambda line, pct: events.append((line, pct)))

    assert result.local_path == tmp_path / "rescuer_data_fork"
    percents = [pct for _, pct in events if pct is not None]
    assert 0.45 in percents
    assert 1.0 in percents


def test_clone_fork_locally_with_progress_cb_raises_on_failure(tmp_path):
    stderr_text = "Cloning into 'x'...\nfatal: repository 'x' not found\n"
    fake_proc = _FakePopen(stderr_text, returncode=128)

    with patch("forkforensics.rescue.subprocess.Popen", return_value=fake_proc):
        with pytest.raises(RescueError, match="not found"):
            clone_fork_locally("rescuer/data", tmp_path, progress_cb=lambda line, pct: None)


def test_clone_fork_locally_cleans_up_a_partial_clone_on_failure(tmp_path):
    """Regression: git clone creates the destination directory itself
    before it can fail partway through (timeout, network error, invalid
    repo) - without cleanup, that partial directory permanently blocks
    every retry with "folder already exists" instead of allowing one, the
    exact bug rescue_sha_locally was already fixed for."""
    local_path = tmp_path / "rescuer_data_fork"

    def _popen_that_creates_the_dir(*a, **k):
        # side_effect, not return_value: the directory must appear only
        # once the code under test actually calls Popen (mirroring real
        # `git clone`, which creates the destination itself), not before -
        # otherwise the pre-existing-folder guard fires instead of the
        # code path this test targets
        local_path.mkdir(parents=True)
        return _FakePopen("fatal: repository not found\n", returncode=128)

    with patch("forkforensics.rescue.subprocess.Popen", side_effect=_popen_that_creates_the_dir):
        with pytest.raises(RescueError):
            clone_fork_locally("rescuer/data", tmp_path, progress_cb=lambda line, pct: None)

    assert not local_path.exists()

    # and the cleanup means a retry doesn't die on "folder already exists"
    # (with subprocess.run mocked, git itself never runs, so it's the
    # ABSENCE of that specific error - not directory creation - being
    # proven here)
    with patch("forkforensics.rescue.subprocess.run", return_value=_ok()) as mock_run:
        result = clone_fork_locally("rescuer/data", tmp_path)
    assert result.local_path == local_path
    mock_run.assert_called_once()


def test_clone_fork_locally_with_progress_cb_raises_clear_error_when_git_missing(tmp_path):
    with patch("forkforensics.rescue.subprocess.Popen", side_effect=FileNotFoundError()):
        with pytest.raises(RescueError, match="git"):
            clone_fork_locally("rescuer/data", tmp_path, progress_cb=lambda line, pct: None)
