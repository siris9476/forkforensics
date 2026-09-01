"""Settings page - the same role app/templates/settings.html used to play:
GitHub token, never shown again in plaintext after saving."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from desktop.widgets.badge import Badge
from desktop.widgets.disappearance_card import Card
from desktop.workers import RateLimitWorker
from forkforensics.cache import CacheManager


class SettingsPage(QWidget):
    def __init__(self, cache: CacheManager, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self._rate_limit_worker: RateLimitWorker | None = None
        self._last_rate_limit_check: float | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        heading = QLabel("Settings")
        heading.setProperty("class", "heading")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(heading)

        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("GitHub token:"))
        self.status_badge = Badge("not configured", "lost")
        status_row.addWidget(self.status_badge)
        status_row.addStretch()
        card_layout.addLayout(status_row)

        note = QLabel(
            "Needed for the daily cycle (GraphQL has no anonymous access) and for "
            "higher REST limits (5000/h instead of 60/h). The token stays on your "
            "computer and is only ever sent to GitHub. Note that it is stored "
            "UNENCRYPTED in the local database — this field merely stops displaying "
            "it after saving. A classic token needs no scopes ticked at all. "
            "You can enter multiple tokens separated by commas: when one is about to "
            "run out of budget it automatically switches to the next instead of "
            "waiting for the reset."
        )
        note.setWordWrap(True)
        note.setProperty("class", "muted")
        note.setStyleSheet("color: #8a8070; font-size: 12px;")
        card_layout.addWidget(note)

        input_row = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("ghp_... or github_pat_... (comma-separated for multiple tokens)")
        input_row.addWidget(self.token_input)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        input_row.addWidget(save_btn)
        card_layout.addLayout(input_row)

        layout.addWidget(card)

        limits_card = Card()
        limits_layout = QVBoxLayout(limits_card)
        limits_layout.setContentsMargins(18, 16, 18, 16)
        limits_layout.setSpacing(8)

        limits_header = QHBoxLayout()
        limits_header.addWidget(QLabel("REST limits per token:"))
        limits_header.addStretch()
        check_btn = QPushButton("Check now")
        check_btn.setObjectName("secondary")
        # lambda, not a direct connect: clicked emits a bool that would
        # otherwise land in `force` and defeat the button
        check_btn.clicked.connect(lambda: self._check_rate_limits(force=True))
        limits_header.addWidget(check_btn)
        limits_layout.addLayout(limits_header)

        self.limits_container = QWidget()
        self.limits_rows_layout = QVBoxLayout(self.limits_container)
        self.limits_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.limits_rows_layout.setSpacing(4)
        limits_layout.addWidget(self.limits_container)

        self.limits_placeholder = QLabel("not checked yet")
        self.limits_placeholder.setStyleSheet("color: #8a8070; font-size: 11px;")
        self.limits_rows_layout.addWidget(self.limits_placeholder)

        layout.addWidget(limits_card)
        layout.addStretch()
        self._refresh_status()

    # don't re-query GitHub more often than this when the tab is merely
    # re-opened; "Check now" always forces a fresh read
    RATE_LIMIT_MAX_AGE_SECONDS = 60

    def refresh(self) -> None:
        """Called every time the tab is opened (same mechanism as
        MonitoredPage/FeedPage) - so the budget shown doesn't stay whatever
        it was the last time someone pressed "Check now"."""
        self._refresh_status()
        if self.current_tokens():
            self._check_rate_limits()

    def _check_rate_limits(self, force: bool = False) -> None:
        tokens = self.current_tokens()
        if not tokens or (self._rate_limit_worker is not None and self._rate_limit_worker.isRunning()):
            return
        # flipping between tabs used to fire GET /rate_limit for every
        # configured token every single time
        if not force and self._last_rate_limit_check is not None:
            age = time.monotonic() - self._last_rate_limit_check
            if age < self.RATE_LIMIT_MAX_AGE_SECONDS:
                return
        self._set_limits_message("checking...")
        worker = RateLimitWorker(tokens, parent=self)
        worker.finished_ok.connect(self._on_rate_limits_ok)
        worker.failed.connect(self._on_rate_limits_failed)
        # deleteLater() only SCHEDULES destruction of the underlying C++
        # object - once the event loop actually processes it, a later call
        # touching self._rate_limit_worker (a live Python wrapper around an
        # already-destroyed object) raises "libshiboken: Internal C++
        # object already deleted". Clearing the reference in the same
        # finished signal removes it before that can happen.
        worker.finished.connect(self._forget_rate_limit_worker)
        worker.finished.connect(worker.deleteLater)
        self._rate_limit_worker = worker
        self._last_rate_limit_check = time.monotonic()
        worker.start()

    def _forget_rate_limit_worker(self) -> None:
        self._rate_limit_worker = None

    def _set_limits_message(self, text: str) -> None:
        while self.limits_rows_layout.count():
            item = self.limits_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        label = QLabel(text)
        label.setStyleSheet("color: #8a8070; font-size: 11px;")
        self.limits_rows_layout.addWidget(label)

    def _on_rate_limits_ok(self, results: list) -> None:
        while self.limits_rows_layout.count():
            item = self.limits_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for i, info in enumerate(results, start=1):
            row = QLabel(self._format_rate_limit_row(i, info))
            row.setStyleSheet("font-family: 'JetBrains Mono', monospace; font-size: 11px;")
            if info.get("error"):
                row.setStyleSheet(row.styleSheet() + " color: #cf6a52;")
            elif (info.get("remaining") or 0) < 100:
                row.setStyleSheet(row.styleSheet() + " color: #d9b34e;")
            self.limits_rows_layout.addWidget(row)

    def _on_rate_limits_failed(self, msg: str) -> None:
        self._set_limits_message(f"check failed: {msg.splitlines()[0]}")

    @staticmethod
    def _format_rate_limit_row(index: int, info: dict) -> str:
        if info.get("error"):
            return f"Token {index}: error ({info['error']})"
        remaining = info.get("remaining")
        limit = info.get("limit")
        reset_at = info.get("reset_at")
        when = ""
        if reset_at:
            delta = datetime.fromtimestamp(reset_at, tz=timezone.utc) - datetime.now(timezone.utc)
            minutes = max(0, int(delta.total_seconds() // 60))
            when = f", resets in {minutes} min" if minutes > 0 else ", reset imminent"
        return f"Token {index}: {remaining}/{limit} requests remaining{when}"

    def _refresh_status(self) -> None:
        tokens = self.current_tokens()
        if not tokens:
            self.status_badge.set_variant("lost", "not configured")
        elif len(tokens) == 1:
            self.status_badge.set_variant("recovered", "configured")
        else:
            self.status_badge.set_variant("recovered", f"{len(tokens)} tokens configured")

    def _save(self) -> None:
        value = self.token_input.text().strip()
        if value:
            self.cache.set_setting("github_token", value)
            self.token_input.clear()
            self._refresh_status()
            self._check_rate_limits(force=True)  # a new token: show its real budget now

    def current_token(self) -> str | None:
        """A single token (backward-compatible) - the first one if more
        than one is configured."""
        tokens = self.current_tokens()
        return tokens[0] if tokens else None

    def current_tokens(self) -> list[str]:
        """All configured tokens, for automatic rotation on rate limits
        (GitHubClient). An environment variable (a single token, if
        present) takes precedence over the saved value, comma- or
        newline-separated."""
        env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if env:
            return [env]
        raw = self.cache.get_setting("github_token") or ""
        return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
