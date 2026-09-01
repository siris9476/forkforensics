from unittest.mock import MagicMock, patch

from forkforensics.github_rest import GitHubClient


def _client_with_mocked_get(status_code, json_body=None):
    client = GitHubClient(token="fake-token")
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    patcher = patch.object(client, "_get", return_value=resp)
    patcher.start()
    return client, patcher


def test_get_user_returns_data_when_found():
    client, patcher = _client_with_mocked_get(200, {"login": "octocat", "type": "User"})
    try:
        user = client.get_user("octocat")
        assert user == {"login": "octocat", "type": "User"}
    finally:
        patcher.stop()


def test_get_user_returns_none_on_404():
    client, patcher = _client_with_mocked_get(404)
    try:
        assert client.get_user("nonexistent-user-xyz") is None
    finally:
        patcher.stop()


def test_get_user_works_for_organizations_too():
    """/users/{name} responds 200 for organizations too, not just for
    individual users - empirically verified against the real API."""
    client, patcher = _client_with_mocked_get(200, {"login": "some-org", "type": "Organization"})
    try:
        org = client.get_user("some-org")
        assert org["type"] == "Organization"
    finally:
        patcher.stop()


def test_single_token_backward_compatible():
    client = GitHubClient(token="fake-token")
    assert client.session.headers["Authorization"] == "Bearer fake-token"
    assert len(client._sessions) == 1


def test_multiple_tokens_get_independent_sessions_and_limiters():
    client = GitHubClient(tokens=["tok-a", "tok-b", "tok-c"])
    assert len(client._sessions) == 3
    auths = {s.headers["Authorization"] for s in client._sessions}
    assert auths == {"Bearer tok-a", "Bearer tok-b", "Bearer tok-c"}
    assert len({id(lim) for lim in client._limiters}) == 3


def test_rotates_to_a_token_with_more_budget_when_active_one_is_low():
    """Regression/feature: with a single token, before this fix, running
    out of budget meant waiting for the reset (up to an hour) - with
    multiple tokens configured, one with margin to spare is chosen instead
    of waiting, and it only waits if ALL of them are exhausted."""
    client = GitHubClient(tokens=["tok-a", "tok-b"])
    client._limiters[0].remaining = 2  # below min_remaining (default 5)
    client._limiters[1].remaining = 100
    client._maybe_rotate()
    assert client._active == 1
    assert client.session.headers["Authorization"] == "Bearer tok-b"


def test_does_not_rotate_when_active_token_has_enough_budget():
    client = GitHubClient(tokens=["tok-a", "tok-b"])
    client._limiters[0].remaining = 500
    client._limiters[1].remaining = 500
    client._maybe_rotate()
    assert client._active == 0


def test_get_rate_limit_all_reports_status_per_token():
    client = GitHubClient(tokens=["tok-a", "tok-b"])
    responses = [
        MagicMock(status_code=200, json=lambda: {
            "resources": {"core": {"limit": 5000, "remaining": 4821, "reset": 1234567}}}),
        MagicMock(status_code=200, json=lambda: {
            "resources": {"core": {"limit": 5000, "remaining": 12, "reset": 7654321}}}),
    ]
    for sess, resp in zip(client._sessions, responses):
        resp.raise_for_status = lambda: None
        patch.object(sess, "get", return_value=resp).start()

    result = client.get_rate_limit_all()

    assert result[0] == {"limit": 5000, "remaining": 4821, "reset_at": 1234567}
    assert result[1] == {"limit": 5000, "remaining": 12, "reset_at": 7654321}


def test_get_rate_limit_all_reports_error_per_token_without_raising():
    client = GitHubClient(tokens=["tok-a"])
    patch.object(client._sessions[0], "get", side_effect=Exception("timeout")).start()

    result = client.get_rate_limit_all()

    assert "error" in result[0]


def test_stays_put_when_every_token_is_exhausted():
    """No token has margin to spare: stay on the current one,
    before_request() will do its job (wait for the reset) just like with a
    single token - it must not loop forever or raise an exception."""
    client = GitHubClient(tokens=["tok-a", "tok-b"])
    client._limiters[0].remaining = 1
    client._limiters[1].remaining = 1
    client._maybe_rotate()
    assert client._active == 0
