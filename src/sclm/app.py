"""Application entry point and composition root.

``main()`` wires everything together: paths -> logging -> settings -> storage ->
QApplication -> DownloadBridge -> DownloadManager -> MainWindow. Nothing else constructs
these objects.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sclm import APP_DISPLAY_NAME, __version__
from sclm.context import AppContext, CoreServices
from sclm.core.logging_setup import install_excepthook, setup_logging
from sclm.core.paths import AppPaths
from sclm.core.settings import SettingsStore
from sclm.core.storage import Storage

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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = _parse_args(argv[1:])

    paths = AppPaths.from_env()
    paths.ensure()
    log_file = setup_logging(paths.log_dir, level=logging.DEBUG if args.debug else logging.INFO)
    install_excepthook()
    log.info("Starting sclm %s (log: %s)", __version__, log_file)

    try:
        core = build_core(paths)
    except Exception as exc:
        log.exception("Startup failed")
        print(f"sclm: startup failed: {exc}", file=sys.stderr)
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
        log.exception("Could not create the main window")
        _show_startup_error(f"SCLM could not start:\n{exc}\n\nSee the log: {log_file}")
        core.storage.close()
        return 1

    try:
        return int(app.exec())
    finally:
        log.info("Shutting down")
        try:
            manager.shutdown(wait=True, timeout=SHUTDOWN_TIMEOUT_S)
        except Exception:
            log.exception("Download manager shutdown failed")
        core.storage.close()
