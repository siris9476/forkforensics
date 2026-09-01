"""Regression: the Ctrl+Q shortcut was built from
QKeySequence.StandardKey.Quit, which has NO platform binding on Windows at
all (QKeySequence(StandardKey.Quit).isEmpty() is True there - it only
resolves to a real key on X11/macOS). A QShortcut built from an empty
QKeySequence is silently inert: pressing Ctrl+Q did nothing, with no error,
on the one platform this app is actually verified on."""

import sys

import pytest
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication

from desktop.main_window import MainWindow
from forkforensics.cache import CacheManager


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_quit_shortcut_has_a_real_non_empty_key_binding(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    window = MainWindow(cache, tmp_path)

    shortcuts = window.findChildren(QShortcut)

    assert len(shortcuts) >= 1
    assert not shortcuts[0].key().isEmpty()
    assert shortcuts[0].key().toString() == "Ctrl+Q"
    # MainWindow.__init__ builds a real MonitoredPage, which starts a real
    # MonitoredRefreshWorker QThread against `cache` on construction (see
    # monitored_page.py's refresh()-on-init). Closing the cache without
    # stopping it first races that thread's SQL query against the
    # connection being closed - main.py always calls shutdown_workers()
    # before cache.close() for exactly this reason (see its comment);
    # skipping it here segfaulted the whole test process, non-deterministically.
    window.shutdown_workers()
    cache.close()


def test_activating_the_shortcut_calls_quit_requested(tmp_path):
    cache = CacheManager(tmp_path / "t.db")
    window = MainWindow(cache, tmp_path)

    calls = []
    window.quit_requested = lambda: calls.append(1)

    shortcut = window.findChildren(QShortcut)[0]
    shortcut.activated.emit()

    assert calls == [1]
    window.shutdown_workers()
    cache.close()


def test_activating_the_shortcut_before_quit_requested_is_set_does_not_raise(tmp_path):
    """quit_requested is None until main() wires it up - the shortcut must
    tolerate being triggered in that window without crashing."""
    cache = CacheManager(tmp_path / "t.db")
    window = MainWindow(cache, tmp_path)

    shortcut = window.findChildren(QShortcut)[0]
    shortcut.activated.emit()  # must not raise
    window.shutdown_workers()
    cache.close()
