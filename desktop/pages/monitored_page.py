"""Page "Monitored" - the same role app/routes/monitored.py used to play:
every repo seen by the ingest, not just isolated disappearances."""

from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from desktop.widgets.disappearance_card import Card
from desktop.workers import AddToWatchlistWorker, MonitoredRefreshWorker
from forkforensics.cache import CacheManager

PAGE_SIZE = 50
STATUS_LABELS = {"": "all", "active": "active", "renamed": "renamed",
                "gone_or_private": "vanished"}


class MonitoredPage(QWidget):
    def __init__(self, cache: CacheManager, get_tokens=None, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.get_tokens = get_tokens
        self.page = 1
        self._worker: MonitoredRefreshWorker | None = None
        self._watch_worker: AddToWatchlistWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        header_row = QHBoxLayout()
        heading = QLabel("Monitored repositories")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        header_row.addWidget(heading)
        header_row.addStretch()
        clear_btn = QPushButton("Clear all")
        clear_btn.setObjectName("secondary")
        clear_btn.clicked.connect(self._clear_all)
        header_row.addWidget(clear_btn)
        outer.addLayout(header_row)

        self.summary = Card()
        summary_layout = QHBoxLayout(self.summary)
        summary_layout.setContentsMargins(18, 14, 18, 14)
        self.summary_labels: dict[str, QLabel] = {}
        for key, title in (("total", "total"), ("active", "active"),
                          ("renamed", "renamed/transferred"), ("gone_or_private", "vanished"),
                          ("watchlist", "watched")):
            box = QVBoxLayout()
            value = QLabel("0")
            value.setStyleSheet("font-size: 18px; font-weight: 600;")
            self.summary_labels[key] = value
            box.addWidget(value)
            sub = QLabel(title)
            sub.setStyleSheet("color: #8a8070; font-size: 11px;")
            box.addWidget(sub)
            summary_layout.addLayout(box)
        outer.addWidget(self.summary)

        watch_row = QHBoxLayout()
        self.watch_input = QLineEdit()
        self.watch_input.setPlaceholderText("owner/repo to always watch, even if it never "
                                            "shows up in a public event again...")
        self.watch_input.returnPressed.connect(self._add_to_watchlist)
        watch_row.addWidget(self.watch_input, stretch=1)
        self.watch_btn = QPushButton("Add to watchlist")
        self.watch_btn.clicked.connect(self._add_to_watchlist)
        watch_row.addWidget(self.watch_btn)
        outer.addLayout(watch_row)
        self.watch_status_label = QLabel("")
        self.watch_status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
        outer.addWidget(self.watch_status_label)

        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search owner/repo...")
        self.search_input.returnPressed.connect(self._reload)
        filters.addWidget(self.search_input)

        self.status_combo = QComboBox()
        for value, label in STATUS_LABELS.items():
            self.status_combo.addItem(label, value)
        self.status_combo.currentIndexChanged.connect(self._reload)
        filters.addWidget(self.status_combo)

        filter_btn = QPushButton("Filter")
        filter_btn.clicked.connect(self._reload)
        filters.addWidget(filter_btn)
        outer.addLayout(filters)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Repo", "Status", "Last activity", "Last checked", "Next check", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(5, 110)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(40)
        outer.addWidget(self.table)

        pager = QHBoxLayout()
        self.page_label = QLabel("")
        self.page_label.setStyleSheet("color: #8a8070; font-size: 12px;")
        pager.addWidget(self.page_label)
        pager.addStretch()
        self.prev_btn = QPushButton("← previous")
        self.prev_btn.setObjectName("secondary")
        self.prev_btn.clicked.connect(self._prev_page)
        self.next_btn = QPushButton("next →")
        self.next_btn.setObjectName("secondary")
        self.next_btn.clicked.connect(self._next_page)
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.next_btn)
        outer.addLayout(pager)

        self.refresh()

    def refresh(self) -> None:
        self.page = 1
        self._reload()

    def _reload(self) -> None:
        # known_repos can have millions of rows (e.g. a test over a wide
        # range) - a COUNT/GROUP BY or an ORDER BY at that scale takes
        # several seconds: running these queries on the UI thread (like
        # before) blocks the tab switch exactly like network/subprocess
        # work would, which is why they now follow the same worker+signal
        # pattern as the others.
        if self._worker is not None and self._worker.isRunning():
            return
        status = self.status_combo.currentData() or None
        search = self.search_input.text().strip() or None
        offset = (self.page - 1) * PAGE_SIZE

        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.page_label.setText("loading...")

        worker = MonitoredRefreshWorker(self.cache, status, search, PAGE_SIZE, offset, parent=self)
        worker.finished_ok.connect(self._on_reload_ok)
        worker.failed.connect(self._on_reload_failed)
        # parented to the page, so without this every table reload left a
        # finished QThread in the object tree for the app's whole lifetime.
        # But deleteLater() only SCHEDULES destruction of the underlying
        # C++ object - once the event loop actually processes it, the next
        # reload's `self._worker.isRunning()` guard hit a live Python
        # wrapper around an already-destroyed C++ object and raised
        # "libshiboken: Internal C++ object already deleted", silently
        # (pythonw has no stderr) freezing this tab after its first load.
        # Clearing the reference in the same finished signal removes it
        # before any later call could ever touch it again.
        worker.finished.connect(self._forget_reload_worker)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _forget_reload_worker(self) -> None:
        self._worker = None

    def _on_reload_ok(self, result: dict) -> None:
        counts = result["counts"]
        self.summary_labels["total"].setText(str(sum(counts.values())))
        self.summary_labels["active"].setText(str(counts.get("active", 0)))
        self.summary_labels["renamed"].setText(str(counts.get("renamed", 0)))
        self.summary_labels["gone_or_private"].setText(str(counts.get("gone_or_private", 0)))
        self.summary_labels["watchlist"].setText(str(result.get("watchlist_count", 0)))

        rows = result["rows"]
        total = result["total"]
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(row["full_name"]))
            status_label = {"active": "active", "renamed": "renamed",
                            "gone_or_private": "vanished"}.get(row["status"], row["status"])
            self.table.setItem(i, 1, QTableWidgetItem(status_label))
            self.table.setItem(i, 2, QTableWidgetItem(row["last_seen_active"] or "-"))
            self.table.setItem(i, 3, QTableWidgetItem(row["last_verified_alive"] or "-"))
            self.table.setItem(i, 4, QTableWidgetItem(row["next_check_due"] or "-"))
            watched = bool(row.get("watched"))
            toggle_btn = QPushButton("remove" if watched else "watch")
            toggle_btn.setObjectName("secondary")
            repo_id = row["repo_id"]
            toggle_btn.clicked.connect(
                lambda _checked=False, rid=repo_id, w=watched: self._toggle_watch(rid, not w))
            self.table.setCellWidget(i, 5, toggle_btn)

        offset = (self.page - 1) * PAGE_SIZE
        shown = len(rows)
        self.page_label.setText(f"{shown} of {total} results (page {self.page})")
        self.prev_btn.setEnabled(self.page > 1)
        self.next_btn.setEnabled(offset + PAGE_SIZE < total)

    def _on_reload_failed(self, msg: str) -> None:
        self.page_label.setText(f"loading error: {msg.splitlines()[0]}")
        self.prev_btn.setEnabled(self.page > 1)
        # a failed load tells us nothing about whether a next page exists;
        # enabling it unconditionally let the user page past the end
        self.next_btn.setEnabled(False)

    def _toggle_watch(self, repo_id: int, watched: bool) -> None:
        self.cache.set_watched(repo_id, watched)
        self._reload()

    def _add_to_watchlist(self) -> None:
        text = self.watch_input.text().strip()
        if "/" not in text:
            self.watch_status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
            self.watch_status_label.setText("expected format: owner/repo")
            return
        owner, _, repo = text.partition("/")
        if not owner or not repo:
            self.watch_status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
            self.watch_status_label.setText("expected format: owner/repo")
            return
        tokens = self.get_tokens() if self.get_tokens else None
        if not tokens:
            self.watch_status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
            self.watch_status_label.setText(
                "configure a GitHub token in Settings to add to the watchlist")
            return
        if self._watch_worker is not None and self._watch_worker.isRunning():
            return

        self.watch_btn.setEnabled(False)
        self.watch_status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
        self.watch_status_label.setText(f"resolving {owner}/{repo}...")
        worker = AddToWatchlistWorker(self.cache, tokens, owner, repo, parent=self)
        worker.finished_ok.connect(self._on_watch_added)
        worker.failed.connect(self._on_watch_failed)
        # see _forget_reload_worker's comment: without clearing the
        # reference too, the NEXT add-to-watchlist click could hit a
        # RuntimeError on an already-destroyed C++ object
        worker.finished.connect(self._forget_watch_worker)
        worker.finished.connect(worker.deleteLater)
        self._watch_worker = worker
        worker.start()

    def _forget_watch_worker(self) -> None:
        self._watch_worker = None

    def _on_watch_added(self, info: dict) -> None:
        self.watch_btn.setEnabled(True)
        self.watch_input.clear()
        self.watch_status_label.setStyleSheet("color: #8fae6f; font-size: 11px;")
        self.watch_status_label.setText(
            f"{info['full_name']} added to the watchlist — it will be rechecked "
            "forever, even if it never shows up in a public event again")
        self._reload()

    def _on_watch_failed(self, msg: str) -> None:
        self.watch_btn.setEnabled(True)
        self.watch_status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
        self.watch_status_label.setText(msg.splitlines()[0])

    def _prev_page(self) -> None:
        if self.page > 1:
            self.page -= 1
            self._reload()

    def _next_page(self) -> None:
        self.page += 1
        self._reload()

    def _clear_all(self) -> None:
        to_delete = self.cache.count_known_repos() - self.cache.count_watchlist()
        if not to_delete:
            return
        confirm = QMessageBox.question(
            self, "Clear all",
            f"Delete {to_delete} rows passively discovered from the monitored "
            "population? Repos added to the watchlist stay intact. Not "
            "destructive for normal use: the daily cycle repopulates it on its "
            "own, for free, from the next GH Archive ingest. Useful for "
            "clearing the leftovers of a test over a wide date range. This "
            "action is not reversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.cache.clear_known_repos()
            self.refresh()
