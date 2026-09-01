"""oldest_commit_via_last_page + _parse_link_header are what the module's
own docstring calls "the trick" the entire fork-ranking pipeline depends
on: GET /commits?per_page=1, read Link: rel="last" for the total count,
then fetch that last page to get the oldest commit. Every existing test
that touches this (test_fork_scan.py, test_investigate_auto_reset_detection.py)
mocks the WHOLE method away and asserts only on downstream behavior - the
actual Link-header parsing and its branches were never pinned against a
realistic header/JSON body, which is exactly where a silent wrong answer
(the project's own stated top risk) would hide."""

from unittest.mock import MagicMock, patch

from forkforensics.github_rest import GitHubClient, _parse_link_header


def _resp(status_code, json_body=None, link_header=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = {"Link": link_header} if link_header else {}
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------- _parse_link_header

def test_parse_link_header_extracts_multiple_rels():
    header = ('<https://api.github.com/x?page=2>; rel="next", '
             '<https://api.github.com/x?page=34>; rel="last"')
    assert _parse_link_header(header) == {
        "next": "https://api.github.com/x?page=2",
        "last": "https://api.github.com/x?page=34",
    }


def test_parse_link_header_handles_none_and_empty():
    assert _parse_link_header(None) == {}
    assert _parse_link_header("") == {}


def test_parse_link_header_ignores_malformed_entries():
    assert _parse_link_header("not a link header at all") == {}


# ---------------------------------------------------------------- oldest_commit_via_last_page

def test_single_page_repo_with_no_link_header_returns_the_only_commit():
    """A repo with few enough commits that /commits?per_page=1 returns
    everything in one page: no Link header at all, so there's no "last"
    page to fetch - the single commit returned IS the oldest one."""
    client = GitHubClient(token="t")
    with patch.object(client, "_get", return_value=_resp(
        200, [{"sha": "onlyone", "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}],
    )) as mock_get:
        result = client.oldest_commit_via_last_page("acme", "widget")

    assert result == ("onlyone", "2020-01-01T00:00:00Z", 1)
    mock_get.assert_called_once()  # no second request needed


def test_multi_page_repo_fetches_the_last_page_for_the_oldest_commit():
    """The realistic case: a Link header names the last page, and the
    total commit count comes from ITS page= number, not from the first
    response - a wrong regex or off-by-one here silently mis-ranks every
    fork by depth."""
    client = GitHubClient(token="t")
    first = _resp(200, [{"sha": "newest", "commit": {"committer": {"date": "2024-01-01"}}}],
                 link_header='<https://api.github.com/repos/acme/widget/commits?per_page=1&page=500>; rel="last"')
    last_page = _resp(200, [{"sha": "oldest", "commit": {"committer": {"date": "2015-06-01"}}}])

    with patch.object(client, "_get", side_effect=[first, last_page]) as mock_get:
        result = client.oldest_commit_via_last_page("acme", "widget")

    assert result == ("oldest", "2015-06-01", 500)
    assert mock_get.call_count == 2
    # the second call must hit the exact "last" URL from the Link header,
    # with no extra params (the URL already carries page=500)
    second_call = mock_get.call_args_list[1]
    assert second_call.args[0] == (
        "https://api.github.com/repos/acme/widget/commits?per_page=1&page=500")


def test_falls_back_to_commit_list_length_when_the_last_url_has_no_page_number():
    """A malformed/unexpected last-page URL (no `page=` query param) must
    not crash - the total falls back to the length of whatever the last
    page actually returned."""
    client = GitHubClient(token="t")
    first = _resp(200, [{"sha": "newest", "commit": {"committer": {"date": "2024-01-01"}}}],
                 link_header='<https://api.github.com/repos/acme/widget/commits?weird>; rel="last"')
    last_page = _resp(200, [
        {"sha": "a", "commit": {"committer": {"date": "2015-01-01"}}},
        {"sha": "b", "commit": {"committer": {"date": "2015-06-01"}}},
    ])

    with patch.object(client, "_get", side_effect=[first, last_page]):
        result = client.oldest_commit_via_last_page("acme", "widget")

    assert result == ("b", "2015-06-01", 2)  # last element of the last page, count = len()


def test_empty_repo_returns_none_on_409():
    client = GitHubClient(token="t")
    with patch.object(client, "_get", return_value=_resp(409)):
        assert client.oldest_commit_via_last_page("acme", "widget") is None


def test_non_200_status_returns_none():
    client = GitHubClient(token="t")
    with patch.object(client, "_get", return_value=_resp(500)):
        assert client.oldest_commit_via_last_page("acme", "widget") is None


def test_empty_commit_list_with_no_link_header_returns_none():
    client = GitHubClient(token="t")
    with patch.object(client, "_get", return_value=_resp(200, [])):
        assert client.oldest_commit_via_last_page("acme", "widget") is None


def test_empty_last_page_returns_none():
    client = GitHubClient(token="t")
    first = _resp(200, [{"sha": "newest", "commit": {"committer": {"date": "2024-01-01"}}}],
                 link_header='<https://api.github.com/x?page=5>; rel="last"')
    with patch.object(client, "_get", side_effect=[first, _resp(200, [])]):
        assert client.oldest_commit_via_last_page("acme", "widget") is None


def test_sha_param_is_forwarded_when_given():
    """Used to scope the search to a specific ref (e.g. a fork's default
    branch) rather than the repo's default."""
    client = GitHubClient(token="t")
    with patch.object(client, "_get", return_value=_resp(
        200, [{"sha": "x", "commit": {"committer": {"date": "2020-01-01"}}}],
    )) as mock_get:
        client.oldest_commit_via_last_page("acme", "widget", sha="feature-branch")

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["sha"] == "feature-branch"
