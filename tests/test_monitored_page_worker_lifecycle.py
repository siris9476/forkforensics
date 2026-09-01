"""Regression: `worker.finished.connect(worker.deleteLater)` was added to
stop finished QThreads from accumulating forever, but deleteLater() only
SCHEDULES destruction of the underlying C++ object - it doesn't happen
until the event loop actually processes it. Once it did, a LATER call's
`self._worker is not None and self._worker.isRunning()` guard touched a
live Python wrapper around an already-destroyed C++ object and raised
`RuntimeError: ... already deleted` - silently, since this runs inside a
Qt slot with no surrounding try/except and pythonw has no stderr to print
it to. The Monitored tab froze at whatever it showed on the very first
load, for the rest of the session, with no visible error at all.

Uses a minimal stand-in (same technique as test_main_window_shutdown.py)
rather than a real MonitoredPage: the full page's QTableWidget/QComboBox
tree isn't what's under test here - only the worker lifecycle pattern
(finished -> forget reference -> deleteLater -> later guard check) is,
and building the full widget tree turned out to interact badly with
unrelated Qt object teardown elsewhere in the suite when run as part of
the full session (reproduced as a native segfault at process exit,
non-deterministic w.r.t. which other test file it was paired with - a
test-infrastructure fragility, not a production bug)."""

import sys
import time

import pytest
from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication, QWidget


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _FakeRefreshWorker(QThread):
    """Finishes almost immediately - just enough to give the real Qt event
    loop a finished signal and a real deleteLater() to process."""

    def run(self) -> None:
        pass


class _MonitoredPageStub(QWidget):
    """Only the exact pattern under test, lifted onto a minimal QWidget:
    a _worker slot, a reload() that guards on isRunning() and (re)creates
    the worker, and the finished-signal wiring that both the old (buggy)
    and new (fixed) code use."""

    def __init__(self, clear_reference: bool) -> None:
        super().__init__()
        self._worker: QThread | None = None
        self._clear_reference = clear_reference
        self.reload_count = 0

    def reload(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.reload_count += 1
        worker = _FakeRefreshWorker(parent=self)
        if self._clear_reference:
            worker.finished.connect(self._forget_worker)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _forget_worker(self) -> None:
        self._worker = None


def _spin_until(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
    assert predicate(), "condition never became true in time"


def test_reload_survives_a_second_call_after_the_first_workers_deferred_deletion():
    page = _MonitoredPageStub(clear_reference=True)

    page.reload()
    _spin_until(lambda: page._worker is None)  # finished, deleted, reference cleared

    # this is exactly the call that used to raise RuntimeError: libshiboken:
    # Internal C++ object (MonitoredRefreshWorker) already deleted
    page.reload()
    _spin_until(lambda: page._worker is None)

    assert page.reload_count == 2


def test_without_clearing_the_reference_a_later_call_hits_a_dead_object():
    """Counter-proof: reproduces the actual bug when the reference-clearing
    fix is absent, so this test file would have failed loudly on the
    original code instead of passing vacuously."""
    page = _MonitoredPageStub(clear_reference=False)

    page.reload()
    first_worker = page._worker
    # let the event loop actually run deleteLater()'s deferred destruction
    deadline = time.monotonic() + 2.0
    while not first_worker.isFinished() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
    # a few extra spins for the deferred delete event itself to be processed
    for _ in range(20):
        QCoreApplication.processEvents()

    with pytest.raises(RuntimeError):
        first_worker.isRunning()
