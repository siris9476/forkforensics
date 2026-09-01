"""Static guard against an entire class of bug that has recurred twice
already this session: a *Worker QThread constructed without parent=self (or
parent=dialog) never joins the Qt object tree, so
MainWindow.shutdown_workers()'s findChildren(QThread) scan can't see it -
graceful shutdown silently skips it, and cache.close() can run while it's
still writing to SQLite. Rather than pin the handful of call sites found so
far one by one, this scans every *Worker(...) construction in the desktop
layer and fails loudly if a new one appears without a parent."""

import ast
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parent.parent / "desktop"
SCANNED_FILES = sorted(DESKTOP_DIR.glob("pages/*.py")) + [DESKTOP_DIR / "main.py"]


def _worker_calls_missing_parent(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if not name or not name.endswith("Worker"):
            continue
        has_parent_kwarg = any(kw.arg == "parent" for kw in node.keywords)
        if not has_parent_kwarg:
            missing.append(f"{path.relative_to(DESKTOP_DIR.parent)}:{node.lineno}: {name}(...)")
    return missing


@pytest.mark.parametrize("path", SCANNED_FILES, ids=lambda p: p.name)
def test_every_worker_construction_passes_a_parent(path):
    missing = _worker_calls_missing_parent(path)
    assert missing == [], (
        "Worker(s) constructed without parent=... - invisible to "
        "shutdown_workers()'s findChildren(QThread) scan:\n" + "\n".join(missing)
    )
