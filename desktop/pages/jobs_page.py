"""History of past investigations - the same role
app/routes/investigate.py's jobs_list/job_report used to play, reusing the
same SQLite tables (create_job/update_job/list_jobs) already in cache.py."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from forkforensics.cache import CacheManager

STATUS_LABELS = {"queued": "queued", "running": "running", "done": "completed",
                "error": "error", "cancelled": "cancelled"}


class JobsPage(QWidget):
    def __init__(self, cache: CacheManager, raw_dir: Path, get_tokens=None, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.raw_dir = raw_dir
        self.get_tokens = get_tokens

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        header_row = QHBoxLayout()
        heading = QLabel("Past investigations")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        header_row.addWidget(heading)
        header_row.addStretch()
        self.clear_btn = QPushButton("Clear history")
        self.clear_btn.setObjectName("secondary")
        self.clear_btn.clicked.connect(self._clear_all)
        header_row.addWidget(self.clear_btn)
        outer.addLayout(header_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Repo", "Status", "Created", "", ""])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        # ResizeToContents doesn't compute the width well for cells with a
        # QPushButton inside (the QSS padding isn't applied yet at
        # computation time) - explicit fixed width is more reliable.
        for col in (3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 140)
        self.table.setColumnWidth(4, 110)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        # default height too short for the buttons in the cells (9px
        # vertical padding + text): it was clipping them out of the visible area.
        self.table.verticalHeader().setDefaultSectionSize(44)
        outer.addWidget(self.table)

        self.refresh()

    def refresh(self) -> None:
        rows = self.cache.list_jobs()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(f"{row['owner']}/{row['repo']}"))
            self.table.setItem(i, 1, QTableWidgetItem(
                STATUS_LABELS.get(row["status"], row["status"])))
            self.table.setItem(i, 2, QTableWidgetItem(row["created_at"]))
            open_btn = QPushButton("open report")
            open_btn.setObjectName("secondary")
            job_id = row["job_id"]
            open_btn.clicked.connect(lambda _checked=False, jid=job_id: self._open(jid))
            self.table.setCellWidget(i, 3, open_btn)
            delete_btn = QPushButton("delete")
            delete_btn.setObjectName("secondary")
            delete_btn.clicked.connect(lambda _checked=False, jid=job_id: self._delete(jid))
            self.table.setCellWidget(i, 4, delete_btn)

    def _clear_all(self) -> None:
        if not self.cache.list_jobs(limit=1):
            return
        confirm = QMessageBox.question(
            self, "Clear history",
            "Delete the entire investigation history, including any rows "
            "still stuck queued? This action is not reversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.cache.clear_jobs()
            self.refresh()

    def _delete(self, job_id: str) -> None:
        self.cache.delete_job(job_id)
        self.refresh()

    def _open(self, job_id: str) -> None:
        row = self.cache.get_job(job_id)
        if row is None or not row["result_json"]:
            QMessageBox.information(self, "Report not available",
                                    "This job doesn't have a saved report yet (or anymore).")
            return
        report = json.loads(row["result_json"])
        from desktop.pages.investigate_page import show_range_result_dialog, show_report_dialog
        if "repos_seen" in report:
            show_range_result_dialog(self, report)
        else:
            show_report_dialog(self, report, self.raw_dir, self.get_tokens, self.cache)
