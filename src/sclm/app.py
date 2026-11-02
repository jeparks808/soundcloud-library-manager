"""Application entry point and composition root.

``main()`` wires everything together: paths -> logging -> settings -> storage ->
QApplication -> DownloadBridge -> DownloadManager -> MainWindow. Nothing else constructs
these objects.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from sclm import APP_DISPLAY_NAME, __version__
from sclm.context import AppContext, CoreServices
from sclm.core.logging_setup import install_excepthook, setup_logging
from sclm.core.paths import AppPaths
from sclm.core.settings import SettingsStore
from sclm.core.storage import Storage

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from sclm.download.manager import DownloadManager

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S = 15.0


def build_core(paths: AppPaths) -> CoreServices:
    """Create directories, load settings and open the database (no Qt).

    Also marks downloads left unfinished by a previous run as FAILED (interrupted).
    """
    paths.ensure()
    settings = SettingsStore(paths.settings_file)
    storage = Storage(paths.db_file)
    interrupted = storage.mark_interrupted_downloads()
    if interrupted:
        log.info("Marked %d unfinished download(s) from the last session as interrupted",
                 interrupted)
    return CoreServices(paths=paths, settings=settings, storage=storage)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sclm", description=APP_DISPLAY_NAME)
    parser.add_argument("--version", action="version", version=f"sclm {__version__}")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    # Qt consumes its own options (e.g. -platform); ignore what we don't know.
    args, _unknown = parser.parse_known_args(argv)
    return args


def _show_startup_error(message: str) -> None:
    print(f"sclm: {message}", file=sys.stderr)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        # A modal dialog would block forever on headless platforms.
        if app is not None and app.platformName() not in ("offscreen", "minimal"):
            QMessageBox.critical(None, APP_DISPLAY_NAME, message)
    except Exception:  # pragma: no cover - best effort only
        pass


def _route_signals_to_quit(app: QApplication) -> Callable[[], None]:
    """Make SIGTERM (``kill``, session logout) and SIGINT (Ctrl+C) quit the event loop, so
    the normal shutdown in :func:`main` runs instead of the process dying mid-download.

    Python runs signal handlers only when the interpreter gets control, which it doesn't
    while Qt's event loop sits in C++. ``signal.set_wakeup_fd`` makes the C-level handler
    write a byte into a socketpair; a ``QSocketNotifier`` on the other end wakes the loop and
    runs a Python slot, and the Python handler runs right then (no polling timer). The
    handler posts ``quit`` with a zero timer, so a signal that arrives before ``exec()`` ends
    the loop as soon as it starts. Main thread only. Returns a function that restores the
    previous handlers (a second signal during shutdown then has its default effect).
    """
    from PySide6.QtCore import QSocketNotifier, QTimer

    try:
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)
        wsock.setblocking(False)
        previous_fd = signal.set_wakeup_fd(wsock.fileno(), warn_on_full_buffer=False)
    except (OSError, ValueError) as exc:
        log.warning("Could not install signal handlers: %s", exc)
        return lambda: None

    def on_signal(signum: int, _frame: object) -> None:
        log.info("Received %s; quitting", signal.Signals(signum).name)
        QTimer.singleShot(0, app.quit)

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read)

    def drain() -> None:
        try:
            while rsock.recv(512):
                pass
        except OSError:
            pass

    notifier.activated.connect(drain)

    def restore() -> None:
        notifier.setEnabled(False)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        signal.set_wakeup_fd(previous_fd)
        rsock.close()
        wsock.close()

    return restore


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    argv = list(sys.argv if argv is None else argv)
    args = _parse_args(argv[1:])

    paths = AppPaths.from_env()
    paths.ensure()
    log_file = setup_logging(paths.log_dir, level=logging.DEBUG if args.debug else logging.INFO)
    install_excepthook()
    log.info("Starting sclm %s (log: %s)", __version__, log_file)

    # Use a user-installed yt-dlp update when one is present and newer (Help → Update
    # yt-dlp…). Must happen before anything imports yt_dlp; never fatal.
    try:
        from sclm.download.updater import activate_override

        activate_override(paths)
    except Exception:
        log.exception("Could not activate the yt-dlp override; using the bundled version")

    try:
        core = build_core(paths)
    except Exception as exc:
        log.exception("Startup failed")
        print(f"sclm: startup failed: {exc}", file=sys.stderr)
        return 1

    from sclm.core.platform_check import check_xcb_cursor

    if not check_xcb_cursor():
        return 1

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(argv)
    # The launcher uses StartupWMClass=sclm, so the Qt application name must be "sclm".
    app.setApplicationName("sclm")
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("sclm")
    app.setDesktopFileName("sclm")
    try:
        from sclm.gui.theme import app_icon, apply_theme

        apply_theme(app)
        icon = app_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)
    except Exception:
        log.exception("Could not apply the theme")

    restore_signals = _route_signals_to_quit(app)
    manager = None
    try:
        from sclm.download.manager import DownloadManager
        from sclm.gui.bridge import DownloadBridge
        from sclm.gui.main_window import MainWindow

        bridge = DownloadBridge()
        manager = DownloadManager(core.storage, core.settings, core.paths, listener=bridge)
        ctx = AppContext(paths=core.paths, settings=core.settings, storage=core.storage,
                         manager=manager, bridge=bridge)
        window = MainWindow(ctx)
        window.show()
    except Exception as exc:
        restore_signals()
        log.exception("Could not create the main window")
        _show_startup_error(f"SCLM could not start:\n{exc}\n\nSee the log: {log_file}")
        if manager is not None:
            _shutdown(manager, core.storage)
        else:
            core.storage.close()
        return 1

    from PySide6.QtCore import QTimer

    QTimer.singleShot(0, lambda: log.info("Ready (%.2f s after start)",
                                          time.monotonic() - started))
    try:
        return int(app.exec())
    finally:
        restore_signals()
        # ``window`` and ``bridge`` stay referenced until main() returns, i.e. until after
        # the manager has stopped and the listener is detached.
        _shutdown(manager, core.storage)


def _shutdown(manager: DownloadManager, storage: Storage) -> None:
    """Stop downloads, then detach the GUI, then close the DB, in that order.

    Runs after the event loop ended, while the window and the bridge still exist. The manager
    goes first so every final state is persisted (and announced) before anything closes.
    Then the listener is detached so a worker that is still finishing never emits into Qt
    objects being torn down. The DB is closed only if no worker is left: a worker stuck in a
    network resolve (cancellation can't interrupt it) still writes its final state, and
    Python joins it at exit anyway.
    """
    log.info("Shutting down")
    finished = False
    try:
        finished = manager.shutdown(wait=True, timeout=SHUTDOWN_TIMEOUT_S)
    except Exception:
        log.exception("Download manager shutdown failed")
    manager.set_listener(None)
    if finished:
        storage.close()
    else:
        log.warning("A download is still stopping; leaving the database open until it ends")
