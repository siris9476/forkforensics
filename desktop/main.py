"""Entry point of the desktop app: python -m desktop.main
No web server, no browser - a real native window (PySide6/Qt)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.main_window import MainWindow  # noqa: E402
from forkforensics.cache import CacheManager  # noqa: E402
from forkforensics.config import SETTINGS  # noqa: E402
from forkforensics.daily_cycle import run_daily_cycle  # noqa: E402


def _setup_logging() -> None:
    """A PyInstaller --windowed executable has no console at all: no harm
    as long as logging.basicConfig() only writes to stderr, BUT in that
    mode sys.stderr is literally None (not just invisible) - the first
    log.info() would try to write to a None object and fail. A log file is
    the only way a user without a console could ever see what happened, so
    it's always active; the console stream is only added if one actually
    exists."""
    SETTINGS.cache_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(SETTINGS.cache_dir / "forkforensics.log", encoding="utf-8"),
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


_setup_logging()
logger = logging.getLogger(__name__)

def _desktop_dir() -> Path:
    """In a PyInstaller executable the .py modules live inside an internal
    archive and __file__ isn't a real path on disk - resources need to be
    looked up next to sys._MEIPASS instead (where --add-data places
    them)."""
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return Path(frozen_base) / "desktop"
    return Path(__file__).resolve().parent


THEME_PATH = _desktop_dir() / "theme.qss"
ICON_PATH = _desktop_dir() / "resources" / "icon.ico"


class _CycleNotifier(QObject):
    """APScheduler runs _scheduled_cycle on ITS OWN background thread, not
    the Qt/GUI one - calling QSystemTrayIcon.showMessage directly from
    there wouldn't be safe. A Qt signal is already built for this: emitted
    from any thread, the connection to a slot living on the GUI thread
    gets executed there automatically (queued connection), with no need
    for extra synchronization."""
    cycle_done = Signal(dict)


def _scheduled_cycle(cache: CacheManager, raw_dir: Path, get_tokens, notifier: _CycleNotifier) -> None:
    tokens = get_tokens()
    if not tokens:
        logger.warning("daily cycle skipped: no GitHub token configured")
        return
    try:
        stats = asyncio.run(run_daily_cycle(cache, raw_dir, tokens))
        notifier.cycle_done.emit(stats)
    except Exception:  # noqa: BLE001
        logger.exception("daily cycle failed")


def main() -> int:
    if sys.platform == "win32":
        # Without an explicit AppUserModelID, Windows groups this process
        # under the generic python.exe/pythonw.exe identity for taskbar
        # purposes - the taskbar button then shows a generic/blank icon
        # instead of the one set on the window, regardless of
        # setWindowIcon(). This must run before QApplication() exists.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("forkforensics.desktop.1")

    SETTINGS.cache_dir.mkdir(parents=True, exist_ok=True)
    cache = CacheManager(SETTINGS.db_path)

    app = QApplication(sys.argv)
    app.setApplicationName("ForkForensics")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    if THEME_PATH.exists():
        app.setStyleSheet(THEME_PATH.read_text(encoding="utf-8"))

    window = MainWindow(cache, SETTINGS.raw_archive_dir)

    notifier = _CycleNotifier()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _scheduled_cycle, CronTrigger(hour=3, minute=0),
        args=[cache, SETTINGS.raw_archive_dir, window.settings_page.current_tokens, notifier],
        id="daily_cycle", replace_existing=True,
    )
    scheduler.start()

    # the X only closes it visually (MainWindow.closeEvent) - the app stays
    # alive in the tray until "Quit" is chosen from its menu, otherwise the
    # scheduler would die with the window and the nightly cycle (and its
    # notifications) would never run unless the window is deliberately
    # left open.
    app.setQuitOnLastWindowClosed(False)
    tray_icon_image = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
    tray = QSystemTrayIcon(tray_icon_image, app)
    tray.setToolTip("ForkForensics")

    def _show_window() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def _quit_app() -> None:
        # order matters: stop the scheduler BEFORE the workers, so a cycle
        # cannot start while we are shutting them down, and so nothing
        # emits into a QApplication that is being torn down.
        scheduler.shutdown(wait=True)
        window.shutdown_workers()
        app.quit()

    # the only real exit path, shared by the tray menu, Ctrl+Q, and the X
    # button on desktops that have no system tray to minimize into
    window.quit_requested = _quit_app

    menu = QMenu()
    menu.addAction("Show ForkForensics").triggered.connect(_show_window)
    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(_quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: _show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)

    def _on_cycle_done(stats: dict) -> None:
        if stats.get("cancelled") or stats.get("api_error"):
            return  # nothing to celebrate - not a normal update
        msg = (f"{stats.get('repos_seen', 0)} repos seen, "
              f"{stats.get('gone', 0)} disappearances found, "
              f"{stats.get('auto_investigated', 0)} automatically investigated")
        # repos whose status a GraphQL error left undecided this cycle -
        # not gone, just retried next time. Worth a mention: otherwise the
        # only place this count exists is the log file.
        if stats.get("undetermined"):
            msg += f", {stats['undetermined']} undetermined (retried next cycle)"
        tray.showMessage("ForkForensics — nightly cycle completed", msg,
                         tray_icon_image, 10000)

    notifier.cycle_done.connect(_on_cycle_done)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        # no tray on this desktop (common on GNOME/Wayland): the icon would
        # never appear and showMessage() silently does nothing, so the X
        # button quits for real instead (see MainWindow.closeEvent).
        logger.warning("no system tray available: closing the window will quit the app, "
                      "and nightly-cycle notifications will not be shown")

    window.show()
    exit_code = app.exec()

    # idempotent: _quit_app already did both on the normal exit path, but
    # app.exec() can also return without it (session logout, last window
    # closed by the platform).
    if scheduler.running:
        scheduler.shutdown(wait=True)
    window.shutdown_workers()
    cache.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
