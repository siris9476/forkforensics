import json
from unittest.mock import MagicMock, patch

from forkforensics.survival_probe import check_alive, check_git_fetch_alive, check_reachable


def test_check_reachable_true_when_ancestor():
    client = MagicMock()
    client.is_ancestor.return_value = True
    refs = [{"commit": {"sha": "headsha"}}]
    assert check_reachable(client, "o", "r", "targetsha", refs) is True


def test_check_reachable_false_when_no_ref_matches():
    client = MagicMock()
    client.is_ancestor.return_value = False
    refs = [{"commit": {"sha": "headsha"}}]
    assert check_reachable(client, "o", "r", "targetsha", refs) is False


def test_check_reachable_true_on_exact_match():
    client = MagicMock()
    client.is_ancestor.return_value = False
    refs = [{"commit": {"sha": "targetsha"}}]
    assert check_reachable(client, "o", "r", "targetsha", refs) is True


def test_check_alive_orphan_but_alive():
    """The central phenomenon: reachable=False but api_alive=True."""
    client = MagicMock()
    client.is_ancestor.return_value = False
    client.get_commit.return_value = {"files": [{"filename": "README.md"}]}
    client.check_raw_alive.return_value = True
    cache = MagicMock()
    cache.get_probe.return_value = None

    result = check_alive(client, cache, "o", "r", "orphansha", refs=[],
                         force_reprobe=True, check_git_fetch=False)
    assert result.reachable_from_current_refs is False
    assert result.api_alive is True
    assert result.raw_alive is True
    cache.record_probe.assert_called_once()


def test_check_alive_captures_commit_preview():
    """The commit metadata is already downloaded for the api_alive check
    (client.get_commit) - previously it was discarded after the boolean
    check, now it's used to show the user WHAT they're about to rescue,
    not just that it's technically possible."""
    client = MagicMock()
    client.is_ancestor.return_value = False
    client.get_commit.return_value = {
        "files": [{"filename": "README.md"}, {"filename": "src/x.py"}],
        "commit": {"message": "Fix the thing\n\nlonger body here",
                   "author": {"name": "Ada Lovelace", "date": "2018-06-15T09:46:39Z"}},
        "parents": [{"sha": "parentsha"}],
    }
    client.check_raw_alive.return_value = True
    cache = MagicMock()
    cache.get_probe.return_value = None

    result = check_alive(client, cache, "o", "r", "sha", refs=[],
                         force_reprobe=True, check_git_fetch=False)

    assert result.commit_message == "Fix the thing\n\nlonger body here"
    assert result.parent_count == 1
    assert result.commit_author == "Ada Lovelace"
    assert result.commit_date == "2018-06-15T09:46:39Z"
    assert result.files_changed == 2
    assert result.changed_files == ["README.md", "src/x.py"]

    recorded_args = cache.record_probe.call_args.args
    assert recorded_args[8:13] == ("Fix the thing\n\nlonger body here", "Ada Lovelace",
                                   "2018-06-15T09:46:39Z", 2, 1)
    assert json.loads(recorded_args[13]) == ["README.md", "src/x.py"]


def test_check_alive_truly_dead():
    client = MagicMock()
    client.is_ancestor.return_value = False
    client.get_commit.return_value = None
    cache = MagicMock()
    cache.get_probe.return_value = None

    result = check_alive(client, cache, "o", "r", "deadsha", refs=[],
                         force_reprobe=True, check_git_fetch=False)
    assert result.api_alive is False
    assert result.raw_alive is False
    assert result.git_fetch_alive is False


def test_check_alive_uses_cache_when_available():
    client = MagicMock()
    cache = MagicMock()
    cache.get_probe.return_value = {
        "reachable": 0, "api_alive": 1, "raw_alive": 0, "git_fetch_alive": 1,
        "checked_at": "2020-01-01", "commit_message": None, "commit_author": None,
        "commit_date": None, "files_changed": None, "parent_count": None,
        "changed_files": None,
    }
    result = check_alive(client, cache, "o", "r", "sha", refs=[], force_reprobe=False)
    assert result.api_alive is True
    assert result.git_fetch_alive is True
    assert result.changed_files is None
    client.get_commit.assert_not_called()


def test_check_alive_decodes_changed_files_from_cache():
    """changed_files is stored as JSON in a TEXT column - it must be
    decoded back into a list when it comes from the cache, not left as a
    raw string."""
    client = MagicMock()
    cache = MagicMock()
    cache.get_probe.return_value = {
        "reachable": 0, "api_alive": 1, "raw_alive": 0, "git_fetch_alive": 1,
        "checked_at": "2020-01-01", "commit_message": "Initial commit",
        "commit_author": "whateverpal", "commit_date": "2018-06-15T09:46:39Z",
        "files_changed": 2, "parent_count": 0,
        "changed_files": json.dumps(["README.md", "LICENSE"]),
    }
    result = check_alive(client, cache, "o", "r", "sha", refs=[], force_reprobe=False)
    assert result.changed_files == ["README.md", "LICENSE"]
    assert result.git_fetch_alive is True
    client.get_commit.assert_not_called()


def test_check_alive_skips_git_fetch_when_reachable():
    """If the SHA is already reachable from a ref, the fetch would
    trivially succeed and adds no information - it shouldn't even be
    attempted."""
    client = MagicMock()
    client.is_ancestor.return_value = True
    client.get_commit.return_value = {"files": [{"filename": "README.md"}]}
    client.check_raw_alive.return_value = True
    cache = MagicMock()
    cache.get_probe.return_value = None

    with patch("forkforensics.survival_probe.check_git_fetch_alive") as mocked:
        result = check_alive(client, cache, "o", "r", "reachablesha",
                             refs=[{"commit": {"sha": "reachablesha"}}],
                             force_reprobe=True, check_git_fetch=True)
        mocked.assert_not_called()
    assert result.git_fetch_alive is False


def test_check_alive_calls_git_fetch_for_orphan_when_enabled():
    client = MagicMock()
    client.is_ancestor.return_value = False
    client.get_commit.return_value = None
    cache = MagicMock()
    cache.get_probe.return_value = None

    with patch("forkforensics.survival_probe.check_git_fetch_alive", return_value=True) as mocked:
        result = check_alive(client, cache, "o", "r", "orphansha", refs=[],
                             force_reprobe=True, check_git_fetch=True)
        mocked.assert_called_once_with("o", "r", "orphansha")
    assert result.git_fetch_alive is True


@patch("forkforensics.survival_probe.subprocess.run")
def test_check_git_fetch_alive_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert check_git_fetch_alive("o", "r", "5ba123c") is True


@patch("forkforensics.survival_probe.subprocess.run")
def test_check_git_fetch_alive_nonzero_exit(mock_run):
    mock_run.return_value = MagicMock(returncode=128)
    assert check_git_fetch_alive("o", "r", "5ba123c") is False


@patch("forkforensics.survival_probe.subprocess.run")
def test_check_git_fetch_alive_uses_correct_command(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    check_git_fetch_alive("acme", "widget", "deadbeef")
    fetch_call = mock_run.call_args_list[-1]
    cmd = fetch_call.args[0]
    assert cmd[:2] == ["git", "-C"]
    assert "fetch" in cmd
    assert "--depth=1" in cmd
    assert "origin" in cmd
    assert "deadbeef" in cmd
    remote_call = mock_run.call_args_list[-2]
    assert "https://github.com/acme/widget.git" in remote_call.args[0]


@patch("forkforensics.survival_probe.subprocess.run", side_effect=FileNotFoundError())
def test_check_git_fetch_alive_missing_git_binary(mock_run):
    assert check_git_fetch_alive("o", "r", "5ba123c") is False


@patch("forkforensics.survival_probe.subprocess.run")
def test_check_git_fetch_alive_timeout(mock_run):
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="git fetch", timeout=25)
    assert check_git_fetch_alive("o", "r", "5ba123c") is False
