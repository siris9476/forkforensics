"""Manual investigation of a specific repo - the same role
app/routes/investigate.py used to play: form -> estimate -> start ->
progress -> report. Secondary compared to the automatic feed, for forcing
an immediate check."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDateEdit, QDialog, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from desktop.workers import (CloneForkWorker, DailyCycleWorker, InvestigateWorker,
                             RecheckShaWorker, RescueWorker)
from forkforensics.archive_index import build_hour_list, estimate_range
from forkforensics.cache import CacheManager
from forkforensics.timeline import to_date

# beyond this threshold, an ingest over a date range risks repeating the
# known_repos bloat already seen live (5.7 million rows from a single
# 28-day range) - a preventive warning, not a hard limit.
RANGE_BLOAT_WARNING_DAYS = 7


class _EstimateWorker(QThread):
    """The HEAD calls for the estimate are lightweight, but for consistency
    with the "never network from the UI thread" rule they still run on a
    separate thread."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, date_from: date, date_to: date, cache_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.date_from, self.date_to, self.cache_dir = date_from, date_to, cache_dir

    def run(self) -> None:
        try:
            hours = build_hour_list(self.date_from, self.date_to)
            self.done.emit(estimate_range(hours, self.cache_dir))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def _build_verdict(report: dict) -> tuple[str, str, list[tuple[str, str]], dict | None]:
    """The rest of the dialog is a table of raw data - here the few fields
    that actually matter (which fork to use, how complete the coverage is,
    what to do about at-risk SHAs) get extracted as label:value rows
    instead of a prose explanation, so it's visually clear this is data
    read from the report, not fixed text. Returns (status, variant, rows,
    best_fork) - best_fork is the dict of the fork to offer for cloning,
    or None if there isn't a useful one."""
    ranking = report.get("fork_ranking", [])
    usable = [f for f in ranking if f.get("oldest_commit_date") and not f.get("error")]
    best = usable[0] if usable else None
    n_gaps = len(report.get("gaps", []))
    at_risk = report.get("at_risk_shas", [])

    fork_oldest_shas = {f.get("oldest_commit_sha") for f in ranking if f.get("oldest_commit_sha")}

    def _is_root(s: dict) -> tuple[bool, bool]:
        """(is it root?, is it a verified fact or just a clue?). Without
        any evidence (neither parent_count nor a match with a fork), the
        default is "not root" - better to flag a potentially useful SHA as
        such than to hide one that might genuinely extend coverage."""
        pc = s.get("parent_count")
        if pc is not None:
            return pc == 0, True
        if s.get("sha") in fork_oldest_shas:
            return True, False
        return False, False

    root_info = [_is_root(s) for s in at_risk]
    extends_coverage = any(not is_root for is_root, _ in root_info)
    all_root = bool(at_risk) and all(is_root for is_root, _ in root_info)
    all_confirmed = all(confirmed for _, confirmed in root_info) if all_root else False

    rows: list[tuple[str, str]] = []
    if best:
        rows.append(("Best fork",
                    f"{best['full_name']} — up to {best['oldest_commit_date']}, "
                    f"{best.get('commit_count') or '?'} commits"))
    gaps = report.get("gaps", [])
    # a "gap" between two dates is computed between the individual known
    # points (each fork's oldest commit, or an archive event) - it does NOT
    # walk the content between those points. If the best fork starts before
    # (or exactly at) the start of the gap, its history is nonetheless
    # continuous by construction (a git repository has no internal gaps):
    # the reported "gap" is just the lack of a second independent reference
    # point in between, not genuinely missing content - showing the raw
    # dates in that case makes it look like an entire period is lost when
    # the fork actually covers it in full.
    # comparison between real dates, not raw strings: oldest_commit_date
    # can have a time ("2018-06-15T09:46:39Z"), gaps[].from doesn't - a
    # string-prefix comparison would only work by coincidence of the ISO
    # format, real parsing is correct and explicit regardless
    best_date = to_date(best["oldest_commit_date"]) if best else None
    # "bridged" covers the START of the gap by construction (a git
    # repository is a continuous chain), but that's not enough if the fork
    # stopped being updated BEFORE the gap ends: pushed_at (the last commit
    # inherited by the fork, already used for automatic gap detection) is
    # the signal of WHERE its coverage actually stops. Verified live on a
    # real case: bridged_by_fork came out True only because the fork
    # started before the gap, but the fork stopped months before the
    # gap's actual end - the message said "no content missing ...
    # up to today" when in fact several months were missing, exactly the ones the
    # at-risk SHAs below are meant to fill.
    fork_end_date = to_date(best["pushed_at"]) if best and best.get("pushed_at") else None
    starts_bridged = bool(best) and bool(gaps) and all(
        best_date <= to_date(g["from"]) for g in gaps)
    residual_ranges = []
    if starts_bridged:
        for g in gaps:
            g_to = to_date(g["to"])
            # without pushed_at (data unavailable, e.g. older reports)
            # there's no way to know if the fork stops before the gap -
            # full coverage is assumed, as before this fix
            if fork_end_date and fork_end_date < g_to:
                residual_ranges.append((fork_end_date.isoformat(), g["to"]))
    if report.get("coverage_continuous"):
        coverage_value = "continuous"
    elif starts_bridged and not residual_ranges:
        coverage_value = (f"no content missing — the fork covers continuously from "
                          f"{best['oldest_commit_date']} to today; the detection flags a "
                          f"\"gap\" only because it has no second independent reference "
                          f"point in between")
    elif starts_bridged and residual_ranges:
        residual_txt = "; ".join(f"{a} → {b}" for a, b in residual_ranges)
        coverage_value = (f"the fork covers from {best['oldest_commit_date']} only up to "
                          f"{best['pushed_at']} (not updated since) — still missing "
                          f"{residual_txt}: check the at-risk SHAs below, they're meant to "
                          f"fill exactly this piece")
    else:
        gap_ranges = "; ".join(f"{g.get('from', '?')} → {g.get('to', '?')}" for g in gaps)
        coverage_value = f"{n_gaps} real gap: {gap_ranges}" if n_gaps == 1 else \
            f"{n_gaps} real gaps: {gap_ranges}"
    rows.append(("Coverage", coverage_value))
    if at_risk:
        if all_root and all_confirmed:
            at_risk_note = "confirmed root commit (0 parents) — redundant with the fork"
        elif all_root:
            at_risk_note = "matches the fork's oldest point (a clue, not confirmed)"
        elif extends_coverage:
            at_risk_note = "might extend coverage — check the preview below"
        else:
            at_risk_note = "check the preview below before recovering it"
        rows.append(("At-risk SHA", f"{len(at_risk)} — {at_risk_note}"))

    if best and report.get("coverage_continuous"):
        return "Full coverage", "recovered", rows, best
    if best and at_risk and extends_coverage:
        return "Partial coverage — check the at-risk SHAs", "ambiguous", rows, best
    if best and at_risk and all_root and all_confirmed:
        return "Substantially complete coverage (via fork)", "recovered", rows, best
    if best and at_risk and all_root:
        return "Probably complete (via fork)", "recovered", rows, best
    if best and at_risk:
        return "Partial coverage", "ambiguous", rows, best
    if best:
        return "Maximum recoverable with current tools", "ambiguous", rows, best
    if at_risk:
        return "No useful fork — direct SHA recovery only", "ambiguous", rows, None
    return "No recovery path found", "lost", rows, None


def _build_clone_fork_row(dialog: QDialog, fork_full_name: str, dest_root: Path) -> QWidget:
    """Actually clones the best fork (git clone) - unlike a single at-risk
    SHA, it brings along the entire commit chain."""
    row = QWidget()
    outer = QVBoxLayout(row)
    outer.setContentsMargins(0, 4, 0, 4)
    outer.setSpacing(4)

    action_row = QHBoxLayout()
    clone_btn = QPushButton(f"Clone {fork_full_name} locally")
    status_label = QLabel("")
    status_label.setWordWrap(True)
    status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
    action_row.addWidget(clone_btn)
    action_row.addWidget(status_label, stretch=1)
    outer.addLayout(action_row)

    # REAL progress (git clone --progress), not just a fixed "this may
    # take a while" text - a fork with years of history can weigh several
    # GB (verified live: over 2.3 GB and still growing)
    progress_bar = QProgressBar()
    progress_bar.setRange(0, 100)
    progress_bar.setTextVisible(True)
    # opts out of the global thin/textless QProgressBar rule, which painted
    # this percentage transparent inside an 8px-tall bar
    progress_bar.setObjectName("withText")
    progress_bar.hide()
    outer.addWidget(progress_bar)

    def _start() -> None:
        clone_btn.setEnabled(False)
        status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
        status_label.setText("starting clone...")
        progress_bar.setRange(0, 0)  # indeterminate until the first percentage arrives
        progress_bar.show()
        worker = CloneForkWorker(fork_full_name, dest_root, parent=dialog)
        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(lambda result: _on_ok(result))
        worker.failed.connect(lambda msg: _on_failed(msg))
        dialog._rescue_workers.append(worker)
        worker.start()

    def _on_progress(line: str, pct: float | None) -> None:
        status_label.setText(line)
        if pct is not None:
            progress_bar.setRange(0, 100)
            progress_bar.setValue(int(pct * 100))

    def _on_ok(result) -> None:
        clone_btn.setEnabled(True)
        clone_btn.setText(f"Redo clone {fork_full_name}")
        progress_bar.hide()
        status_label.setStyleSheet("color: #8fae6f; font-size: 11px;")
        status_label.setText(f"cloned to {result.local_path}")

    def _on_failed(msg: str) -> None:
        clone_btn.setEnabled(True)
        progress_bar.hide()
        status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
        status_label.setText(f"failed: {msg}")

    clone_btn.clicked.connect(_start)
    return row


def show_report_dialog(parent: QWidget, report: dict, raw_dir: Path | None = None,
                       get_tokens=None, cache: CacheManager | None = None) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Report: {report['owner']}/{report['repo']}")
    dialog.resize(880, 700)
    dialog._rescue_workers = []  # keeps the QThreads alive as long as the dialog exists

    outer_layout = QVBoxLayout(dialog)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    content = QWidget()
    layout = QVBoxLayout(content)
    scroll.setWidget(content)
    outer_layout.addWidget(scroll)
    # With many forks and/or at-risk SHAs (verified live: 24 forks + 3
    # detailed SHAs on a real case) the content easily exceeds a fixed
    # height - without scrolling, the QVBoxLayout compresses the table (the
    # only widget with an Expanding size policy) until it becomes
    # unreadable instead of shrinking the other elements, which have a
    # fixed size.

    if report.get("cancelled"):
        # a partial run must say so before the verdict, which is computed
        # from incomplete data and will understate coverage
        cancelled_warn = QLabel(
            "⚠ Stopped on request before finishing — the verdict below is based on "
            "partial data and may understate what is actually recoverable. Re-run "
            "without stopping for a complete picture.")
        cancelled_warn.setWordWrap(True)
        cancelled_warn.setStyleSheet(
            "background-color: #3a3018; color: #d9b34e; border-radius: 6px; "
            "padding: 8px 10px; font-size: 11px;")
        layout.addWidget(cancelled_warn)

    status, verdict_variant, verdict_rows, best_fork = _build_verdict(report)
    verdict_colors = {
        "recovered": ("#2a3320", "#8fae6f"),
        "ambiguous": ("#3a3018", "#d9b34e"),
        "lost": ("#3a2119", "#cf6a52"),
    }
    bg, fg = verdict_colors[verdict_variant]
    verdict_box = QWidget()
    verdict_box.setStyleSheet(f"background-color: {bg}; border-radius: 8px;")
    verdict_layout = QVBoxLayout(verdict_box)
    verdict_layout.setContentsMargins(12, 10, 12, 10)
    verdict_layout.setSpacing(3)
    status_label = QLabel(status)
    status_label.setStyleSheet(f"color: {fg}; font-weight: 600; font-size: 12px;")
    verdict_layout.addWidget(status_label)
    for field_label, field_value in verdict_rows:
        row_label = QLabel(f"{field_label}: {field_value}")
        row_label.setWordWrap(True)
        # carries remote-derived values (fork names, dates) - see the note
        # in _build_commit_preview
        row_label.setTextFormat(Qt.TextFormat.PlainText)
        row_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row_label.setStyleSheet(
            f"color: {fg}; font-family: 'JetBrains Mono', monospace; font-size: 11px;")
        verdict_layout.addWidget(row_label)
    layout.addWidget(verdict_box)

    if best_fork and raw_dir:
        clone_row = _build_clone_fork_row(dialog, best_fork["full_name"], raw_dir.parent / "recovered")
        layout.addWidget(clone_row)

    layout.addWidget(QLabel(f"Generated: {report.get('generated_at', '')}"))

    layout.addWidget(QLabel("Fork ranking by historical depth (the best one is first) — "
                            "double-click a row to open the fork in your browser:"))
    ranking = report.get("fork_ranking", [])
    table = QTableWidget(len(ranking), 5)
    table.setHorizontalHeaderLabels(["Fork", "URL", "Oldest commit", "Date", "Commit count"])
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    for i, f in enumerate(ranking):
        full_name = f.get("full_name", "")
        url = f"https://github.com/{full_name}" if full_name else ""
        table.setItem(i, 0, QTableWidgetItem(full_name))
        url_item = QTableWidgetItem(url)
        url_item.setForeground(QColor("#d99a4e"))
        table.setItem(i, 1, url_item)
        table.setItem(i, 2, QTableWidgetItem((f.get("oldest_commit_sha") or "")[:10]))
        table.setItem(i, 3, QTableWidgetItem(f.get("oldest_commit_date") or f.get("error") or "-"))
        table.setItem(i, 4, QTableWidgetItem(str(f.get("commit_count") or "-")))
    table.cellDoubleClicked.connect(
        lambda row, _col: QDesktopServices.openUrl(QUrl(table.item(row, 1).text()))
        if table.item(row, 1) and table.item(row, 1).text() else None)
    # height based on the number of rows (up to a ceiling) so a short
    # ranking shows in full and a long one scrolls internally, instead of
    # being squeezed by the rest of the content
    table.setMinimumHeight(min(46 + 28 * max(len(ranking), 1), 320))
    layout.addWidget(table)

    at_risk = report.get("at_risk_shas", [])
    layout.addWidget(QLabel(f"At-risk SHAs (alive but orphaned now): {len(at_risk)}"))
    dest_root = raw_dir.parent / "recovered" if raw_dir else None
    for s in at_risk:
        row = _build_rescue_row(dialog, report["owner"], report["repo"], s, dest_root,
                                get_tokens, cache)
        layout.addWidget(row)

    close_btn = QPushButton("Close")
    close_btn.clicked.connect(dialog.accept)
    outer_layout.addWidget(close_btn)  # outside the scroll: always visible
    dialog.exec()
    # The dialog is parented to the page, so without this it survived
    # exec() as a permanent child - fork table, at-risk rows and all - and
    # twenty opened reports stayed resident for the app's lifetime.
    # Deleted only once nothing is still running inside it: the rescue and
    # clone callbacks capture dialog-owned widgets, so tearing it down
    # under a live worker would turn a leak into a crash.
    if not any(w.isRunning() for w in dialog._rescue_workers):
        dialog.deleteLater()


def _build_commit_preview(sha_info: dict) -> QWidget:
    """What's actually in this commit, not just that it's technically
    recoverable - the metadata was already downloaded for the api_alive
    check (client.get_commit), here it's shown instead of being discarded,
    so the decision to recover it is informed."""
    box = QLabel()
    box.setWordWrap(True)
    # PlainText, not the AutoText default: this renders a commit message
    # written by whoever pushed to the scanned repository. On AutoText, Qt
    # interprets anything that looks like markup, so a crafted commit
    # message could forge official-looking verdict text inside this report.
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    box.setStyleSheet(
        "background-color: #100d09; border: 1px solid #292319; border-radius: 6px; "
        "padding: 8px 10px; color: #b3a996; font-size: 11px;")

    message = sha_info.get("commit_message")
    if not message:
        box.setText("commit preview not available (probed before this feature existed, "
                    "or the commit is no longer reachable via the API)")
        return box

    first_line = message.strip().splitlines()[0] if message.strip() else "(empty message)"
    author = sha_info.get("commit_author") or "unknown author"
    when = sha_info.get("commit_date") or "unknown date"
    n_files = sha_info.get("files_changed")
    changed_files = sha_info.get("changed_files")
    if changed_files:
        shown = ", ".join(changed_files[:10])
        if n_files and n_files > len(changed_files):
            shown += f", +{n_files - len(changed_files)} more"
        files_part = f"\nFiles: {shown}"
    elif n_files is not None:
        files_part = f" · {n_files} files changed"
    else:
        files_part = ""
    box.setText(f"“{first_line}”\n{author} — {when}{files_part}")

    parent_count = sha_info.get("parent_count")
    if parent_count == 0:
        warn = QLabel(
            "confirmed root commit (0 parents in the API response): recovering it "
            "in isolation gets you only this commit, not a history built on top of it")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #d9b34e; font-size: 11px; padding-top: 2px;")
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(2)
        wrapper_layout.addWidget(box)
        wrapper_layout.addWidget(warn)
        return wrapper
    return box


def _build_rescue_row(dialog: QDialog, owner: str, repo: str, sha_info: dict,
                      dest_root: Path | None, get_tokens=None,
                      cache: CacheManager | None = None) -> QWidget:
    """A row for an at-risk SHA: info + a button that actually runs
    init/remote/fetch/checkout (forkforensics.rescue), instead of just
    leaving the commands to copy by hand."""
    row = QWidget()
    outer = QVBoxLayout(row)
    outer.setContentsMargins(0, 4, 0, 4)
    outer.setSpacing(4)

    sha = sha_info["sha"]

    def _info_text(info: dict) -> str:
        return (f"{sha[:12]}  (api_alive={info.get('api_alive')} "
               f"raw_alive={info.get('raw_alive')} git_fetch_alive={info.get('git_fetch_alive')})")

    info_label = QLabel(_info_text(sha_info))
    info_label.setStyleSheet("font-family: 'JetBrains Mono', monospace; font-size: 11px;")
    outer.addWidget(info_label)

    # separate container (not just the preview widget) so a recheck can
    # REPLACE it with fresh data instead of stacking old previews under
    # the new one
    preview_box = QWidget()
    preview_layout = QVBoxLayout(preview_box)
    preview_layout.setContentsMargins(0, 0, 0, 0)
    preview_layout.addWidget(_build_commit_preview(sha_info))
    outer.addWidget(preview_box)

    def _replace_preview(new_info: dict) -> None:
        info_label.setText(_info_text(new_info))
        while preview_layout.count():
            item = preview_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        preview_layout.addWidget(_build_commit_preview(new_info))

    action_row = QHBoxLayout()
    rescue_btn = QPushButton("Clone locally")
    rescue_btn.setObjectName("secondary")
    recheck_btn = QPushButton("Recheck")
    recheck_btn.setObjectName("secondary")
    status_label = QLabel("")
    status_label.setWordWrap(True)
    status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
    action_row.addWidget(rescue_btn)
    action_row.addWidget(recheck_btn)
    action_row.addWidget(status_label, stretch=1)
    outer.addLayout(action_row)

    if sha_info.get("git_fetch_alive") is False:
        status_label.setText("warning: the direct fetch failed in the last check - "
                             "the attempt might fail")

    if get_tokens is None or cache is None:
        recheck_btn.setEnabled(False)
        recheck_btn.setToolTip("not available for this report")
    else:
        def _start_recheck() -> None:
            tokens = get_tokens()
            if not tokens:
                status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
                status_label.setText("configure a GitHub token in Settings to recheck")
                return
            recheck_btn.setEnabled(False)
            status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
            status_label.setText("rechecking — doesn't reuse the result already in "
                                 "the cache, actually checks NOW...")
            worker = RecheckShaWorker(cache, tokens, owner, repo, sha, parent=dialog)
            worker.finished_ok.connect(lambda new_info: _on_recheck_ok(new_info))
            worker.failed.connect(lambda msg: _on_recheck_failed(msg))
            dialog._rescue_workers.append(worker)
            worker.start()

        def _on_recheck_ok(new_info: dict) -> None:
            recheck_btn.setEnabled(True)
            _replace_preview(new_info)
            sha_info.update(new_info)
            still_at_risk = (not new_info.get("reachable_from_current_refs")
                            and (new_info.get("api_alive") or new_info.get("raw_alive")
                                or new_info.get("git_fetch_alive")))
            if still_at_risk:
                status_label.setStyleSheet("color: #8fae6f; font-size: 11px;")
                status_label.setText(f"rechecked at {new_info.get('checked_at', '')} — still alive")
            else:
                status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
                status_label.setText(f"rechecked at {new_info.get('checked_at', '')} — "
                                     "no longer recoverable: it may have been "
                                     "garbage-collected by GitHub in the meantime")

        def _on_recheck_failed(msg: str) -> None:
            recheck_btn.setEnabled(True)
            status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
            status_label.setText(f"recheck failed: {msg}")

        recheck_btn.clicked.connect(_start_recheck)

    if dest_root is None:
        rescue_btn.setEnabled(False)
        status_label.setText("no local path available for this report")
        return row

    def _start() -> None:
        rescue_btn.setEnabled(False)
        status_label.setStyleSheet("color: #8a8070; font-size: 11px;")
        status_label.setText("cloning in progress (git init/fetch/checkout) — if the SHA "
                             "isn't a root commit it brings along the whole history built "
                             "on top of it, not just this commit...")
        worker = RescueWorker(owner, repo, sha, dest_root, parent=dialog)
        worker.finished_ok.connect(lambda result: _on_ok(result))
        worker.failed.connect(lambda msg: _on_failed(msg))
        dialog._rescue_workers.append(worker)
        worker.start()

    def _on_ok(result) -> None:
        rescue_btn.setEnabled(True)
        rescue_btn.setText("Redo")
        status_label.setStyleSheet("color: #8fae6f; font-size: 11px;")
        status_label.setText(f"cloned to {result.local_path} (branch {result.branch})")

    def _on_failed(msg: str) -> None:
        rescue_btn.setEnabled(True)
        status_label.setStyleSheet("color: #cf6a52; font-size: 11px;")
        status_label.setText(f"failed: {msg}")

    rescue_btn.clicked.connect(_start)
    return row


def show_range_result_dialog(parent: QWidget, stats: dict) -> None:
    """Outcome of a date-range-only search (no specific repo) - the same
    statistics as the automatic daily cycle, here launched manually over a
    chosen range instead of just yesterday."""
    cancelled = stats.get("cancelled", False)
    dialog = QDialog(parent)
    title = f"Cycle {stats.get('date_from', '')} → {stats.get('date_to', '')}"
    dialog.setWindowTitle(f"{title} (cancelled)" if cancelled else title)
    dialog.resize(460, 340)
    layout = QVBoxLayout(dialog)

    if cancelled:
        warn = QLabel("Cancelled on request before covering the whole range — "
                      "the statistics below reflect only the part already analyzed.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #cf6a52; font-size: 12px;")
        layout.addWidget(warn)

    if stats.get("api_error"):
        warn = QLabel(
            "Investigations stopped due to a persistent GitHub API error "
            f"(likely rate limit exhausted): {stats['api_error']}\n"
            f"{stats.get('investigate_remaining', 0)} remain queued, they will be "
            "resumed on the next cycle.")
        warn.setWordWrap(True)
        # consistent with the rest of this file: PlainText on anything
        # that embeds API-derived text, even though this particular string
        # (an exception message wrapping a URL) isn't fork-owner-controlled
        warn.setTextFormat(Qt.TextFormat.PlainText)
        warn.setStyleSheet("color: #cf6a52; font-size: 12px;")
        layout.addWidget(warn)

    layout.addWidget(QLabel(
        f"Requested range: {stats.get('date_from', '')} → {stats.get('date_to', '')}"))

    rows = [
        ("Repository sightings", stats.get("repos_seen", 0)),
        ("Repositories rechecked", stats.get("checked", 0)),
        ("Still alive", stats.get("alive", 0)),
        ("Renamed", stats.get("renamed", 0)),
        ("Vanished (new)", stats.get("gone", 0)),
        ("Undetermined (retried next cycle)", stats.get("undetermined", 0)),
        ("Recovery investigations started", stats.get("auto_investigated", 0)),
    ]
    for label, value in rows:
        layout.addWidget(QLabel(f"{label}: {value}"))

    if stats.get("gone", 0):
        hint = QLabel("Go to the \"Feed\" tab to see the disappearances found and "
                      "the outcome of the automatic recovery.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8070; font-size: 12px;")
        layout.addWidget(hint)

    close_btn = QPushButton("Close")
    close_btn.clicked.connect(dialog.accept)
    layout.addWidget(close_btn)
    dialog.exec()


class InvestigatePage(QWidget):
    def __init__(self, cache: CacheManager, raw_dir: Path, get_tokens, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.raw_dir = raw_dir
        self.get_tokens = get_tokens
        self._worker: InvestigateWorker | DailyCycleWorker | None = None
        self._estimate_worker: _EstimateWorker | None = None
        self._current_job_id: str | None = None
        self._mode = "repo"  # "repo" (owner+repo) or "range" (date range only)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        heading = QLabel("Investigate a repo, or search by date range")
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        outer.addWidget(heading)
        subtitle = QLabel("Manual flow, secondary compared to the automatic feed.")
        subtitle.setStyleSheet("color: #8a8070; font-size: 12px;")
        outer.addWidget(subtitle)

        form = QGroupBox()
        form_layout = QVBoxLayout(form)
        row1 = QHBoxLayout()
        self.owner_input = QLineEdit()
        self.owner_input.setPlaceholderText("owner (e.g. octocat)")
        self.repo_input = QLineEdit()
        self.repo_input.setPlaceholderText("repo (e.g. Hello-World)")
        row1.addWidget(self.owner_input)
        row1.addWidget(self.repo_input)
        form_layout.addLayout(row1)

        owner_repo_hint = QLabel(
            "Leave both owner and repo empty to search only within the date "
            "range below, without targeting a specific repo: it ingests those "
            "days from GH Archive and flags any repo seen that turned out to "
            "have vanished in the meantime.")
        owner_repo_hint.setWordWrap(True)
        owner_repo_hint.setStyleSheet("color: #8a8070; font-size: 11px;")
        form_layout.addWidget(owner_repo_hint)

        self.use_dates = QCheckBox("Use a date range for GH Archive reconstruction")
        self.use_dates.toggled.connect(self._toggle_dates)
        form_layout.addWidget(self.use_dates)

        row2 = QHBoxLayout()
        self.date_from = QDateEdit(calendarPopup=True)
        self.date_from.setDate(date.today().replace(year=date.today().year - 1))
        self.date_to = QDateEdit(calendarPopup=True)
        self.date_to.setDate(date.today())
        self.date_from.setEnabled(False)
        self.date_to.setEnabled(False)
        row2.addWidget(QLabel("From:"))
        row2.addWidget(self.date_from)
        row2.addWidget(QLabel("To:"))
        row2.addWidget(self.date_to)
        form_layout.addLayout(row2)

        buttons_row = QHBoxLayout()
        self.estimate_btn = QPushButton("Estimate")
        self.estimate_btn.setObjectName("secondary")
        self.estimate_btn.clicked.connect(self._estimate)
        self.start_btn = QPushButton("Start investigation")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("secondary")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.hide()
        buttons_row.addWidget(self.estimate_btn)
        buttons_row.addWidget(self.start_btn)
        buttons_row.addWidget(self.stop_btn)
        form_layout.addLayout(buttons_row)

        self.estimate_label = QLabel("")
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setStyleSheet("color: #8a8070; font-size: 12px;")
        form_layout.addWidget(self.estimate_label)

        outer.addWidget(form)

        self.status_label = QLabel("")
        outer.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        outer.addWidget(self.progress)
        outer.addStretch()

    def _toggle_dates(self, checked: bool) -> None:
        self.date_from.setEnabled(checked)
        self.date_to.setEnabled(checked)

    def _estimate(self) -> None:
        if not self.use_dates.isChecked():
            self.estimate_label.setText("No date range selected: only the already-known "
                                        "forks and candidate SHAs will be checked, "
                                        "without downloading GH Archive.")
            return
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        self.estimate_btn.setEnabled(False)
        self.estimate_label.setText("Estimating...")
        self._estimate_worker = _EstimateWorker(d_from, d_to, self.raw_dir.parent, parent=self)
        self._estimate_worker.done.connect(self._on_estimate_done)
        self._estimate_worker.failed.connect(self._on_estimate_failed)
        self._estimate_worker.start()

    def _on_estimate_done(self, result) -> None:
        self.estimate_btn.setEnabled(True)
        ok = "sufficient" if result.disk_ok else "MIGHT NOT BE ENOUGH"
        self.estimate_label.setText(
            f"{result.n_hours} hourly files, ~{result.estimated_gb:.1f} GB estimated "
            f"(sampled {result.sampled_hours} files). "
            f"Free space: {result.free_disk_bytes / 1e9:.1f} GB — {ok}."
        )

    def _on_estimate_failed(self, message: str) -> None:
        self.estimate_btn.setEnabled(True)
        self.estimate_label.setText(f"Estimate failed: {message}")

    def _start(self) -> None:
        owner = self.owner_input.text().strip()
        repo = self.repo_input.text().strip()
        range_only = not owner and not repo

        if not range_only and (not owner or not repo):
            QMessageBox.warning(self, "Missing data",
                                "Enter both owner and repo, or leave both "
                                "empty to search only within the date range.")
            return
        if range_only and not self.use_dates.isChecked():
            QMessageBox.warning(self, "Missing date range",
                                "Without owner/repo, select a date range to "
                                "analyze (checkbox above the date fields).")
            return

        tokens = self.get_tokens()
        if not tokens:
            QMessageBox.warning(self, "Missing token",
                                "Configure a GitHub token in Settings before investigating.")
            return

        date_from = date_to = None
        if self.use_dates.isChecked():
            date_from = self.date_from.date().toPython()
            date_to = self.date_to.date().toPython()
            # a reversed range used to be masked by abs() in the size
            # estimate below, then build_hour_list returned an empty list
            # and the run "completed" instantly having done nothing
            if date_to < date_from:
                QMessageBox.warning(
                    self, "Invalid date range",
                    f"The end date ({date_to.isoformat()}) is before the start date "
                    f"({date_from.isoformat()}). Swap them and try again.")
                return
            if date_to >= date.today():
                QMessageBox.information(
                    self, "Incomplete day",
                    "GH Archive publishes each day's files after the day ends, so "
                    "today is always incomplete. The scan will run, but expect gaps "
                    "at the end of the range.")

        if range_only and date_from and date_to:
            n_days = abs((date_to - date_from).days) + 1
            if n_days > RANGE_BLOAT_WARNING_DAYS:
                # found live: a 28-day range left 5.7 million rows in
                # known_repos (almost all repos seen only once in one hour
                # of GH Archive and never relevant again) - a warning
                # BEFORE repeating that scale, not just a way to clean up
                # the result afterwards.
                confirm = QMessageBox.question(
                    self, "Wide range",
                    f"This range covers {n_days} days. Every day of GH Archive can "
                    f"add hundreds of thousands of repos to the monitored population "
                    f"(known_repos) — a previous test over 28 days produced 5.7 "
                    f"million, slowing the app down until indexes were added. "
                    f"You can always clean them up later from Monitored → \"Clear all\". Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if confirm != QMessageBox.StandardButton.Yes:
                    return

        self._mode = "range" if range_only else "repo"
        self._current_job_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        job_owner = owner or "(range)"
        job_repo = repo or f"{date_from.isoformat()}..{date_to.isoformat()}"
        self.cache.create_job(self._current_job_id, job_owner, job_repo,
                              date_from.isoformat() if date_from else "",
                              date_to.isoformat() if date_to else "", now)

        self.start_btn.setEnabled(False)
        self.stop_btn.show()
        self.stop_btn.setEnabled(True)
        self.progress.show()
        self.progress.setValue(0)
        self.status_label.setText("Starting...")

        # parent=self so shutdown_workers()'s findChildren(QThread) scan can
        # see these threads - unparented, they stayed invisible to graceful
        # shutdown even after that mechanism was fixed elsewhere, and these
        # are the two workers behind "Start investigation"/range scans,
        # writing to SQLite for as long as they run
        if range_only:
            self._worker = DailyCycleWorker(self.cache, self.raw_dir, tokens, date_from, date_to,
                                            parent=self)
        else:
            self._worker = InvestigateWorker(self.cache, tokens, owner, repo, self.raw_dir,
                                             date_from, date_to, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _stop(self) -> None:
        if self._worker is not None:
            self.stop_btn.setEnabled(False)
            self.status_label.setText("Stopping... (finishing the current hour/file)")
            self._worker.requestInterruption()

    def _on_progress(self, phase: str, detail: str, pct: float) -> None:
        self.status_label.setText(f"{phase}: {detail}")
        self.progress.setValue(int(pct * 100))

    def _on_finished(self, result: dict) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.hide()
        self.progress.hide()
        self.status_label.setText("Cancelled." if result.get("cancelled") else "Completed.")
        if self._current_job_id:
            self.cache.update_job(self._current_job_id,
                                  status="cancelled" if result.get("cancelled") else "done",
                                  progress_pct=1.0, result_json=json.dumps(result),
                                  updated_at=datetime.now(timezone.utc).isoformat())
        if self._mode == "range":
            show_range_result_dialog(self, result)
        else:
            show_report_dialog(self, result, self.raw_dir, self.get_tokens, self.cache)

    def _on_failed(self, message: str) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.hide()
        self.progress.hide()
        self.status_label.setText("Investigation failed.")
        if self._current_job_id:
            self.cache.update_job(self._current_job_id, status="error", error=message,
                                  updated_at=datetime.now(timezone.utc).isoformat())
        QMessageBox.critical(self, "Error", message)
