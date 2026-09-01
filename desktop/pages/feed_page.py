"""Feed page - the same role app/templates/feed.html used to play:
vanished repositories by day, populated by the automatic cycle. No repo
to enter by hand - the app discovers them on its own."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from desktop.widgets.disappearance_card import DisappearanceCard
from desktop.workers import DailyCycleWorker
from forkforensics.cache import CacheManager


class FeedPage(QWidget):
    def __init__(self, cache: CacheManager, raw_dir: Path, get_tokens, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.raw_dir = raw_dir
        self.get_tokens = get_tokens
        self._worker: DailyCycleWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        header_row = QHBoxLayout()
        heading = QLabel("Vanished repositories")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        header_row.addWidget(heading)
        header_row.addStretch()
        self.refresh_btn = QPushButton("Refresh now")
        self.refresh_btn.setObjectName("secondary")
        self.refresh_btn.clicked.connect(self._run_cycle_now)
        header_row.addWidget(self.refresh_btn)
        outer.addLayout(header_row)

        subtitle = QLabel("Populated automatically by the daily cycle "
                          "(GH Archive ingest + GraphQL verification).")
        subtitle.setProperty("class", "muted")
        subtitle.setStyleSheet("color: #8a8070; font-size: 12px;")
        outer.addWidget(subtitle)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate: progress isn't known in advance
        self.progress.hide()
        outer.addWidget(self.progress)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self._container)
        outer.addWidget(self.scroll)
        # deliberately no refresh() here: MainWindow's setCurrentRow(0)
        # fires _on_nav_changed -> refresh() right after construction, so
        # calling it here as well ran the query twice and built (then threw
        # away) up to 200 cards on every launch.

    def refresh(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        rows = [dict(r) for r in self.cache.list_disappearances(limit=200)]
        if not rows:
            empty = QLabel("No disappearance detected yet.\nThe feed populates after "
                           "the daily cycle has run at least once.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color: #8a8070; padding: 48px;")
            self._list_layout.addWidget(empty)
            return

        by_date: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_date[row["detected_date"]].append(row)
        today_str = date.today().isoformat()

        for day in sorted(by_date, reverse=True):
            label = day + (" · today" if day == today_str else "")
            day_label = QLabel(label)
            day_label.setStyleSheet("font-size: 13px; font-weight: 500; color: #b3a996; "
                                    "margin-top: 8px;")
            self._list_layout.addWidget(day_label)
            for row in by_date[day]:
                card = DisappearanceCard(row)
                card.clicked.connect(self._show_detail)
                self._list_layout.addWidget(card)

    def _show_detail(self, disappearance_id: int) -> None:
        row = self.cache.get_disappearance(disappearance_id)
        if row is None:
            return
        text = (f"Repo: {row['full_name']}\n"
               f"Detected on: {row['detected_date']}\n"
               f"Last known activity: {row['last_known_alive'] or 'unknown'}\n"
               f"Investigation status: {row['investigation_status']}\n\n"
               f"{row['recoverable_summary'] or ''}")
        QMessageBox.information(self, row["full_name"], text)

    def _run_cycle_now(self) -> None:
        tokens = self.get_tokens()
        if not tokens:
            QMessageBox.warning(self, "Missing token",
                                "Configure a GitHub token in Settings before "
                                "starting the cycle (needed for GraphQL).")
            return
        self.refresh_btn.setEnabled(False)
        self.progress.show()
        # parent=self so shutdown_workers()'s findChildren(QThread) scan can
        # actually see this thread - unparented, it stayed invisible to
        # graceful shutdown even after that mechanism was fixed, and this is
        # the worker behind the nightly cycle itself, writing to SQLite the
        # whole time it runs
        self._worker = DailyCycleWorker(self.cache, self.raw_dir, tokens, parent=self)
        self._worker.finished_ok.connect(self._on_cycle_done)
        self._worker.failed.connect(self._on_cycle_failed)
        self._worker.start()

    def _on_cycle_done(self, stats: dict) -> None:
        self.refresh_btn.setEnabled(True)
        self.progress.hide()
        self.refresh()
        QMessageBox.information(
            self, "Cycle completed",
            f"Repos seen: {stats.get('repos_seen', 0)}\n"
            f"Checked: {stats.get('checked', 0)}\n"
            f"Vanished found: {stats.get('gone', 0)}\n"
            f"Renamed: {stats.get('renamed', 0)}\n"
            # a GraphQL error can leave a repo's status undecided for this
            # cycle - not gone, just retried next time. Without this line
            # "checked" can silently exceed the other counts added up, with
            # no visible explanation of where the difference went.
            f"Undetermined (retried next cycle): {stats.get('undetermined', 0)}\n"
            f"Automatically investigated: {stats.get('auto_investigated', 0)}",
        )

    def _on_cycle_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.progress.hide()
        QMessageBox.critical(self, "Cycle failed", message)
