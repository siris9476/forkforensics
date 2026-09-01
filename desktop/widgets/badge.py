""""Badge" widget - the same semantics as the web redesign's CSS badges
(ambiguous/recovered/lost), here as a QLabel with a QSS class applied."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

VARIANTS = {"ambiguous", "recovered", "lost"}


class Badge(QLabel):
    def __init__(self, text: str, variant: str = "ambiguous", parent=None) -> None:
        super().__init__(text, parent)
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
        # every current caller's text is either a fixed string or derived
        # from GitHub data (e.g. recoverable_summary embeds a fork's
        # full_name) - PlainText here means no future caller has to
        # remember to set it themselves to avoid markup injection.
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName("badge")
        self.setProperty("badgeVariant", variant)
        self._variant = variant

    def set_variant(self, variant: str, text: str | None = None) -> None:
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
        self._variant = variant
        self.setProperty("badgeVariant", variant)
        if text is not None:
            self.setText(text)
        # force Qt to re-read the QSS for the new "badgeVariant" property value
        self.style().unpolish(self)
        self.style().polish(self)
