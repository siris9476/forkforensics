"""shutdown_workers() is the last thing that runs before the SQLite
connection is closed and the process exits. If it misses a running thread,
that thread keeps writing to a database that is about to be closed - so
what it collects, and that it actually asks threads to stop, are both worth
pinning."""

import sys

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QDialog, QWidget


@pytest.fixture(scope="session", autouse=True)
def qapp():
    # a real QApplication, not QCoreApplication: this file constructs real
    # QWidget/QDialog instances (needed to exercise findChildren across a
    # genuine Qt parent-child tree), and QWidget requires the GUI
    # application object to exist first.
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _FakeWorker(QThread):
    """Runs until interrupted, exactly like the real workers (which all
    poll isInterruptionRequested via their cancel_cb)."""

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self.msleep(5)


class _FakeWindow(QWidget):
    """A real QWidget, not a plain stand-in: _all_workers uses Qt's own
    parent-child tree (findChildren), which only exists between actual
    QObjects - a fake built from plain Python attributes couldn't exercise
    the thing being tested."""

    from desktop.main_window import MainWindow
    _all_workers = MainWindow._all_workers
    shutdown_workers = MainWindow.shutdown_workers

    def __init__(self):
        super().__init__()
        self.feed_page = QWidget(self)
        self.monitored_page = QWidget(self)
        self.investigate_page = QWidget(self)
        self.jobs_page = QWidget(self)
        self.settings_page = QWidget(self)


def test_collects_a_worker_parented_directly_to_a_page():
    window = _FakeWindow()
    worker = _FakeWorker(parent=window.feed_page)

    assert worker in window._all_workers()


def test_collects_a_worker_that_lives_on_a_transient_report_dialog():
    """Regression: investigate_page.py's clone/rescue/recheck workers are
    constructed with parent=dialog, where dialog is a QDialog(parent=page)
    created and held only as a LOCAL variable inside show_report_dialog() -
    never stored as an attribute of investigate_page. A version of
    _all_workers that scanned pages' __dict__ (even broadly, including
    lists) could never see this: the dialog itself is invisible to that
    scan. Because the dialog's Qt parent is the page, and the worker's Qt
    parent is the dialog, findChildren on the window still reaches it
    through the real object tree - a `git clone` worker can be minutes and
    gigabytes into its work when Quit is pressed."""
    window = _FakeWindow()
    dialog = QDialog(window.investigate_page)  # exactly how show_report_dialog builds it
    worker = _FakeWorker(parent=dialog)

    assert worker in window._all_workers()


def test_shutdown_requests_interruption_and_actually_stops_running_threads():
    """The old code called wait(3000) WITHOUT requestInterruption(), so on
    any long-running operation the wait was guaranteed to time out and the
    caller went on to close the database underneath a live thread."""
    window = _FakeWindow()
    worker = _FakeWorker(parent=window.investigate_page)
    worker.start()
    assert worker.isRunning()

    window.shutdown_workers()

    assert not worker.isRunning()


def test_shutdown_stops_a_worker_living_on_a_dialog_too():
    window = _FakeWindow()
    dialog = QDialog(window.investigate_page)
    worker = _FakeWorker(parent=dialog)
    worker.start()
    assert worker.isRunning()

    window.shutdown_workers()

    assert not worker.isRunning()


def test_shutdown_is_safe_when_nothing_is_running():
    window = _FakeWindow()
    _FakeWorker(parent=window.feed_page)
    window.shutdown_workers()  # must not raise
