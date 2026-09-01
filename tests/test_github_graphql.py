import base64

from forkforensics.github_graphql import legacy_id_to_node_id


def test_legacy_id_to_node_id_matches_the_real_github_node_id():
    """The README cites this test as the evidence that the legacy-id ->
    node-id encoding is correct. It therefore has to assert the actual
    value: the previous version only checked "is a non-empty string", which
    would have passed for a completely wrong encoding.

    1296269 is octocat/Hello-World; MDEwOlJlcG9zaXRvcnkxMjk2MjY5 is the
    node id GitHub really returns for it."""
    assert legacy_id_to_node_id(1296269) == "MDEwOlJlcG9zaXRvcnkxMjk2MjY5"


def test_encoding_is_base64_of_the_documented_prefix():
    """Pins the scheme itself, so a change in the encoding fails here with
    a readable diff rather than as a mysterious wave of false
    disappearances at run time."""
    decoded = base64.b64decode(legacy_id_to_node_id(42))
    assert decoded == b"010:Repository42"


def test_legacy_id_to_node_id_differs_per_id():
    assert legacy_id_to_node_id(1) != legacy_id_to_node_id(2)
