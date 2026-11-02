"""Pre-flight check for the Qt xcb platform plugin (needs libxcb-cursor.so.0).

Without that library Qt aborts the whole process ("Aborted (core dumped)") inside the
QApplication constructor. This module detects it first so we can explain the fix instead.
No Qt imports; every lookup is injectable for tests.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

XCB_CURSOR_LIB = "libxcb-cursor.so.0"
INSTALL_COMMAND = "sudo apt install libxcb-cursor0"
MESSAGE = (
    "SoundCloud Library Manager cannot start: the Qt 'xcb' display plugin needs "
    "libxcb-cursor0, which is not installed.\n\nInstall it with:\n    " + INSTALL_COMMAND
)


def xcb_will_be_used(env: Mapping[str, str]) -> bool:
    platform = env.get("QT_QPA_PLATFORM", "")
    if platform and platform != "xcb":
        return False
    return bool(env.get("DISPLAY")) and not env.get("WAYLAND_DISPLAY")


def _default_loadable() -> bool:
    if ctypes.util.find_library("xcb-cursor"):
        return True
    try:
        ctypes.CDLL(XCB_CURSOR_LIB)
        return True
    except OSError:
        return False


def _default_notify(message: str) -> None:
    """Best-effort graphical notice for desktop-launcher users (no terminal)."""
    if shutil.which("zenity"):
        cmd = ["zenity", "--error", "--title=SoundCloud Library Manager", "--no-markup",
               f"--text={message}"]
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "--urgency=critical", "SoundCloud Library Manager", message]
    else:
        return
    try:
        subprocess.run(cmd, timeout=60, check=False, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.debug("Could not show the graphical message", exc_info=True)


def check_xcb_cursor(
    env: Mapping[str, str] | None = None,
    *,
    loadable: Callable[[], bool] = _default_loadable,
    notify: Callable[[str], None] = _default_notify,
    stderr=None,
) -> bool:
    """Return True to proceed with startup, False if the user was told to install the lib.

    Never raises: if the check itself fails, startup proceeds.
    """
    try:
        env = os.environ if env is None else env
        if not xcb_will_be_used(env):
            return True
        if loadable():
            return True
    except Exception:
        log.debug("xcb-cursor pre-flight check failed; continuing", exc_info=True)
        return True
    log.error("libxcb-cursor.so.0 is missing; the Qt xcb plugin cannot load. Run: %s",
              INSTALL_COMMAND)
    import sys
    print(f"sclm: {MESSAGE}", file=stderr or sys.stderr)
    try:
        notify(MESSAGE)
    except Exception:
        log.debug("notify failed", exc_info=True)
    return False
