"""Card for a feed row - QFrame with the look designed in the web redesign
(app/templates/feed.html), here as a real native widget."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from .badge import Badge


class Card(QFrame):
    """Named subclass (not just QFrame) so the QSS selector "Card { ... }"
    in theme.qss can target it by its real class name."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # needed so the QSS ":hover" has an effect on QFrame, which
        # normally doesn't track hover state for styling purposes
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)


class DisappearanceCard(Card):
    clicked = Signal(int)  # emits the disappearance_id on click

    def __init__(self, row: dict, parent=None) -> None:
        super().__init__(parent)
        self._disappearance_id = row["disappearance_id"]
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # the feed used to be mouse-only: Tab now walks the cards and
        # Enter/Space opens the focused one
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Disappearance: {row['full_name']}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        name = QLabel(row["full_name"])
        # PlainText, not the AutoText default: full_name comes from the
        # GitHub API / GH Archive. On AutoText, Qt would interpret anything
        # in it that looks like markup.
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setProperty("class", "heading")
        name.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(name)

        if row.get("owner_status") == "owner_gone":
            badge = Badge("almost certainly deleted — the account is gone too", "lost")
        else:
            badge = Badge("vanished — deleted or made private, indistinguishable", "ambiguous")
        layout.addWidget(badge)

        meta = QLabel(f"last known activity: {row.get('last_known_alive') or 'unknown'}")
        meta.setTextFormat(Qt.TextFormat.PlainText)
        meta.setProperty("class", "muted")
        meta.setObjectName("")  # no shared id here, just the dynamic class below
        meta.setStyleSheet("color: #8a8070; font-size: 11px;")
        layout.addWidget(meta)

        status = row.get("investigation_status")
        if status == "done":
            outcome = Badge(row.get("recoverable_summary") or "recoverable", "recovered")
        elif status == "running":
            outcome = QLabel("recovery investigation in progress…")
            outcome.setStyleSheet("color: #8a8070; font-size: 11px;")
        elif status == "pending":
            # not "in progress" - the automatic cycle investigates up to
            # 5000 pending disappearances per run (list_pending_disappearances),
            # stopping early only on cancellation or a persistent API error;
            # anything past that (or past a halt) stays queued for next time
            outcome = QLabel("queued for the next automatic cycle")
            outcome.setStyleSheet("color: #8a8070; font-size: 11px;")
        else:
            outcome = Badge("investigation failed", "lost")
        layout.addWidget(outcome)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - override Qt
        # on RELEASE and left-button only: it used to fire on press and for
        # any button, so a right-click or middle-click opened the detail
        # dialog and there was no way to change your mind by dragging off
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self._disappearance_id)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - override Qt
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self._disappearance_id)
            return
        super().keyPressEvent(event)
