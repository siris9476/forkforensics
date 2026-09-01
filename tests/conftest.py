import gzip
import json
from pathlib import Path

import pytest

# no sys.path hack needed: pyproject.toml's [tool.pytest.ini_options]
# pythonpath = ["."] puts the repo root on sys.path before collection.

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)


def _write_gz_jsonl(path: Path, lines: list) -> None:
    """gzip.open() stamps the header with the current write timestamp -
    regenerating the same fixture bit-for-bit on every run produced a
    spurious git diff (identical content, only the binary header changed),
    to be discarded by hand after every test run. mtime=0 pins the
    header, so the resulting file is bit-identical on every regeneration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n"
        for line in lines
    ).encode("utf-8")
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as f:
        f.write(content)


@pytest.fixture(scope="session", autouse=True)
def build_fixtures():
    """Builds the synthetic .json.gz fixtures once per test session."""
    current = [
        {
            "type": "PushEvent", "created_at": "2023-05-01T10:00:00Z",
            "actor": {"login": "alice"},
            "repo": {"id": 42, "name": "acme/widget"},
            "payload": {"ref": "refs/heads/main", "before": "aaa111",
                       "commits": [{"sha": "bbb222", "author": {"name": "alice"}, "message": "x"}]},
        },
        {
            "type": "PushEvent", "created_at": "2023-05-01T10:05:00Z",
            "actor": {"login": "bob"},
            "repo": {"id": 99, "name": "other/repo"},
            "payload": {"ref": "refs/heads/main", "before": "ccc333",
                       "commits": [{"sha": "ddd444", "author": {"name": "bob"}, "message": "y"}]},
        },
        {
            "type": "WatchEvent", "created_at": "2023-05-01T10:06:00Z",
            "actor": {"login": "carol"},
            "repo": {"id": 42, "name": "acme/widget"},
            "payload": {"action": "started"},
        },
        "this line is not valid JSON {{{",
    ]
    _write_gz_jsonl(FIXTURES / "sample_hour_2023.json.gz", current)

    legacy = [
        {
            "type": "PushEvent", "created_at": "2013-06-01T10:00:00Z",
            "actor": {"login": "dave"},
            "repository": {"id": 7, "owner": "acme", "name": "widget"},
            "payload": {"ref": "refs/heads/master",
                       "shas": [["eee555", "dave", "old commit", "url"]]},
        },
    ]
    _write_gz_jsonl(FIXTURES / "sample_hour_2013_legacy_schema.json.gz", legacy)
