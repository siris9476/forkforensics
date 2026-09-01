"""SHAs reach the git command line straight from third-party GH Archive
JSON. Two things make that worth validating: git's positional ref slot is
option-parsed (a value starting with "--" is read as a flag), and the same
value is interpolated into the destination folder name, where separators
escape the destination root."""

from unittest.mock import MagicMock, patch

import pytest

from forkforensics.rescue import RescueError, _contained, clone_fork_locally, rescue_sha_locally
from forkforensics.survival_probe import check_git_fetch_alive


def _ok():
    return MagicMock(returncode=0, stderr=b"")


@pytest.mark.parametrize("bad_sha", [
    "--upload-pack=touch pwned",   # option injection
    "../../../escape",             # path traversal via the folder name
    "abc",                         # too short to be a real short SHA
    "zzzzzzz",                     # not hexadecimal
    "",
])
def test_rescue_refuses_a_malformed_sha_without_running_git(bad_sha, tmp_path):
    with patch("forkforensics.rescue.subprocess.run") as mock_run:
        with pytest.raises(RescueError, match="not a valid commit SHA"):
            rescue_sha_locally("octocat", "data", bad_sha, tmp_path)
    mock_run.assert_not_called()


def test_rescue_refuses_a_malformed_owner_or_repo(tmp_path):
    with pytest.raises(RescueError, match="not a valid owner"):
        rescue_sha_locally("../evil", "data", "abc123def456", tmp_path)
    with pytest.raises(RescueError, match="not a valid repository name"):
        rescue_sha_locally("octocat", "da/ta", "abc123def456", tmp_path)


def test_a_traversing_sha_can_no_longer_escape_the_destination_root(tmp_path):
    """Before validation, dest_root / f"{owner}_{repo}_{sha[:10]}" with a
    sha of "../../../x" resolved OUTSIDE dest_root - and the folder was then
    created there."""
    outside = tmp_path.parent / "x"
    with pytest.raises(RescueError):
        rescue_sha_locally("octocat", "data", "../../../x", tmp_path)
    assert not outside.exists()


def test_clone_refuses_a_malformed_fork_name(tmp_path):
    with patch("forkforensics.rescue.subprocess.run") as mock_run:
        with pytest.raises(RescueError, match="not a valid fork"):
            clone_fork_locally("../../evil/data", tmp_path)
    mock_run.assert_not_called()


def test_probe_refuses_a_malformed_sha_instead_of_shelling_out():
    with patch("forkforensics.survival_probe.subprocess.run") as mock_run:
        assert check_git_fetch_alive("o", "r", "--upload-pack=evil") is False
    mock_run.assert_not_called()


def test_contained_rejects_a_multi_segment_escaping_path(tmp_path):
    """Regression: an earlier version rebuilt the checked path as
    `dest_root / local_path.name`, discarding any traversal already baked
    into local_path's directory portion - .name alone is just the last
    component, so the escape was invisible by the time the check ran. Only
    ever harmless because both real call sites build a single flat
    component; this pins _contained itself as genuine defense-in-depth for
    any future caller that doesn't."""
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    escaping = dest_root / "safe" / ".." / ".." / "outside"

    with pytest.raises(RescueError, match="refusing to write outside"):
        _contained(dest_root, escaping)


def test_contained_accepts_a_genuinely_nested_safe_path(tmp_path):
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    nested = dest_root / "a" / "b"

    result = _contained(dest_root, nested)

    assert result == nested.resolve()


def test_a_failed_rescue_cleans_up_its_folder_so_a_retry_can_work(tmp_path):
    """The folder was created BEFORE the git steps, so any failure left it
    behind and every later retry died with "folder already exists" instead
    of retrying."""
    failing = MagicMock(returncode=1, stderr=b"fatal: couldn't find remote ref")
    with patch("forkforensics.rescue.subprocess.run", return_value=failing):
        with pytest.raises(RescueError):
            rescue_sha_locally("octocat", "data", "abc123def456", tmp_path)

    assert not (tmp_path / "octocat_data_abc123def4").exists()

    # and a subsequent successful attempt is not blocked by leftovers
    with patch("forkforensics.rescue.subprocess.run", return_value=_ok()):
        result = rescue_sha_locally("octocat", "data", "abc123def456", tmp_path)
    assert result.local_path.is_dir()
