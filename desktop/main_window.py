"""Main window: sidebar navigation + pages, the same role
app/templates/base.html used to play but as a real native window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QListWidget, QListWidgetItem, QStackedWidget,
                               QSystemTrayIcon, QWidget)

from desktop.pages.feed_page import FeedPage
from desktop.pages.investigate_page import InvestigatePage
from desktop.pages.jobs_page import JobsPage
from desktop.pages.monitored_page import MonitoredPage
from desktop.pages.settings_page import SettingsPage
from forkforensics.cache import CacheManager

NAV_ITEMS = ["Feed", "Monitored", "Investigate", "History", "Settings"]


class MainWindow(QWidget):
    def __init__(self, cache: CacheManager, raw_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("ForkForensics")
        self.resize(1000, 700)
        # set by main() - the single real exit path (stops workers, the
        # scheduler and the app). Used by closeEvent when there is no tray
        # to minimize into, and by the Ctrl+Q shortcut below.
        self.quit_requested = None

        # QKeySequence.StandardKey.Quit has NO binding on Windows at all
        # (isEmpty() == True - it only resolves to Ctrl+Q on X11 and Cmd+Q
        # on macOS) - a QShortcut built from it is silently inert on the
        # one platform this app is actually verified on. Bound explicitly
        # instead so it's the same key everywhere.
        quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        quit_shortcut.activated.connect(
            lambda: self.quit_requested() if self.quit_requested else None)

        self.settings_page = SettingsPage(cache)
        self.feed_page = FeedPage(cache, raw_dir, self.settings_page.current_tokens)
        self.monitored_page = MonitoredPage(cache, self.settings_page.current_tokens)
        self.investigate_page = InvestigatePage(cache, raw_dir, self.settings_page.current_tokens)
        self.jobs_page = JobsPage(cache, raw_dir, self.settings_page.current_tokens)

        self.stack = QStackedWidget()
        for page in (self.feed_page, self.monitored_page, self.investigate_page,
                    self.jobs_page, self.settings_page):
            self.stack.addWidget(page)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(170)
        for name in NAV_ITEMS:
            QListWidgetItem(name, self.sidebar)
        self.sidebar.currentRowChanged.connect(self._on_nav_changed)
        self.sidebar.setCurrentRow(0)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, stretch=1)

    def _on_nav_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.stack.widget(index)
        if hasattr(page, "refresh"):
            page.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802 - override Qt
        # the X minimizes to the system tray instead of actually closing -
        # otherwise the nightly cycle's notifications wouldn't make sense
        # (the scheduler would die with the window). It only actually
        # closes from "Quit" in the tray menu (shutdown_workers()).
        #
        # ONLY when a tray actually exists, though: several Linux desktops
        # (GNOME/Wayland by default) have no tray at all, and there the
        # icon never appears - hiding the window would leave the app
        # running with no icon, no menu and no way back, killable only from
        # a terminal. Without a tray the X must really mean quit.
        if self.quit_requested is not None and not QSystemTrayIcon.isSystemTrayAvailable():
            self.quit_requested()
            event.accept()
            return
        event.ignore()
        self.hide()

    def _all_workers(self) -> list:
        """Every QThread this window (or a dialog it opened) may still own.

        Uses Qt's own parent-child tree (findChildren) instead of naming
        attributes by hand or scanning known ones: a hardcoded list of 3
        attributes silently missed the watchlist, rate-limit, estimate and
        clone workers, and even a broader scan of the five pages' __dict__
        still misses workers that live on a REPORT DIALOG rather than a
        page - investigate_page.py's clone/rescue/recheck workers are
        constructed with parent=dialog, and the dialog itself is a
        transient local object, never stored as a page attribute. Because
        each worker's Qt parent chain still runs dialog -> page -> this
        window, findChildren(QThread) on the window finds them regardless
        of where in that tree they're actually held - a `git clone` worker
        can be minutes and gigabytes into its work when Quit is pressed."""
        return self.findChildren(QThread)

    def shutdown_workers(self) -> None:
        """Called before the process actually terminates. Asks every thread
        to stop FIRST (they all honour isInterruptionRequested), then waits.
        Without the interruption request the wait was guaranteed to time out
        on any long operation, and the caller went on to close the SQLite
        connection while those threads were still writing to it."""
        workers = [w for w in self._all_workers() if w.isRunning()]
        for worker in workers:
            worker.requestInterruption()
        for worker in workers:
            if not worker.wait(10000):
                # last resort: a thread blocked in a subprocess/socket that
                # cannot observe the interruption flag. Better than closing
                # the database out from under it.
                worker.terminate()
                worker.wait(2000)
