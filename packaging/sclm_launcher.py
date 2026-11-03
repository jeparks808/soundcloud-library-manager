"""Entry point of the frozen (PyInstaller / AppImage) build.

Behaves exactly like ``python -m sclm`` (``sclm.app.main``), plus packaging-only hooks
that never run in a normal launch:

* ``--self-test [--network] [--download]``: checks the frozen environment (imports, Qt
  plugins, bundled libxcb-cursor, certifi, bundled ffmpeg, optionally a real yt-dlp
  preview/download) without opening a window, prints a report and exits 0/1.
  Implemented in ``packaging/sclm_selftest.py``.
* ``--playback-smoke [--network] [--screenshot-dir DIR]``: drives the real window through
  local playback and the embedded SoundCloud player in temporary data; audio goes to a
  temporary null sink.
  Implemented in ``packaging/sclm_playback_smoke.py``.
* ``SCLM_SMOKE_QUIT_AFTER_MS=<n>``: quits the Qt event loop ``n`` ms after it starts, so
  smoke tests exercise the real startup *and* shutdown path (manager shutdown, DB close).
"""

from __future__ import annotations

import os
import sys


def _restore_child_environment() -> None:
    """Keep the bundle's library/plugin paths out of the environment.

    The PyInstaller bootloader exports LD_LIBRARY_PATH=<bundle> and its PySide6 hook exports
    QT_PLUGIN_PATH/QML2_IMPORT_PATH=<bundle>. This process no longer needs them: the dynamic
    loader read LD_LIBRARY_PATH at startup, and Qt finds its plugins through the qt.conf that
    PySide6 / the PyInstaller hook registered. Leaking them would make child processes
    (ffmpeg on PATH, xdg-open, the file manager) load the bundle's libraries/plugins.
    A user-set QT_PLUGIN_PATH is deliberately not restored: system Qt plugins built against
    another Qt version must not be loaded into this process.
    """
    if not getattr(sys, "frozen", False):
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)
    os.environ.pop("QT_PLUGIN_PATH", None)
    os.environ.pop("QML2_IMPORT_PATH", None)


def _install_smoke_quit(ms: int) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    original_exec = QApplication.exec

    def _exec(*_args):  # QApplication.exec is static in PySide6
        QTimer.singleShot(ms, QApplication.quit)
        return original_exec()

    QApplication.exec = _exec


def _main() -> int:
    _restore_child_environment()
    args = sys.argv[1:]
    if "--playback-smoke" in args:
        import sclm_playback_smoke

        shots = args[args.index("--screenshot-dir") + 1] if "--screenshot-dir" in args else None
        return sclm_playback_smoke.run(network="--network" in args, screenshot_dir=shots)
    if "--self-test" in args:
        import sclm_selftest

        return sclm_selftest.run(network="--network" in args, download="--download" in args)

    quit_ms = os.environ.get("SCLM_SMOKE_QUIT_AFTER_MS", "").strip()
    if quit_ms.isdigit():
        _install_smoke_quit(int(quit_ms))

    from sclm.app import main

    return main()


if __name__ == "__main__":
    sys.exit(_main())
