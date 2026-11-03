"""Listening engine: the system WebKitGTK player process, embedded into the Listen page.

Why not QtWebEngine: SoundCloud streams AAC over HLS (``mp4a.40.2``) only, and the
QtWebEngine shipped in the PySide6 wheels is built without proprietary codecs, so every
track failed with "There was a problem playing this track". The distribution's WebKitGTK
plays it through GStreamer. ``webkit_player.py`` runs under the *system* python3 (PyGObject
is not bundled) and is controlled over a JSON-lines pipe; see that module for the protocol.

Nothing here reads cookies or page content; URLs are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

HELPER = Path(__file__).with_name("webkit_player.py")
QUIT_WAIT_MS = 1500

NOTICES = {
    "blocked": "That page can't open in the player. Use Open in browser.",
    "download": "Use the Download page for authorized downloads. This page is for listening.",
    "pause_failed": "Pause SoundCloud with its own play button.",
}


def system_python() -> str | None:
    """The interpreter that has the distribution's PyGObject + WebKitGTK bindings."""
    override = os.environ.get("SCLM_WEBKIT_PYTHON")
    if override:
        return override
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def embeddable() -> bool:
    return QGuiApplication.platformName() == "xcb"


class WebKitBrowser(QObject):
    """Same interface the Listen page used for QtWebEngine, plus audio state and pause."""

    url_changed = Signal(str)
    loaded = Signal(bool)
    notice = Signal(str)
    audio_changed = Signal(bool)
    history_changed = Signal(bool, bool)
    playback_error = Signal()
    external_requested = Signal(str)
    failed = Signal(str)       # the engine could not start or stopped; text for the user

    def __init__(self, paths, parent: QWidget, *, program: str | None = None,
                 args: list[str] | None = None) -> None:
        super().__init__(parent)
        self.closed = False
        self.embedded = embeddable()
        self.playing = False
        self._pending: list[dict] = []
        self._ready = False
        self._buffer = b""
        self._stderr_tail = b""
        self._window: QWindow | None = None
        self.container: QWidget | None = None

        self.view = QWidget(parent)
        layout = QVBoxLayout(self.view)
        layout.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("Starting the SoundCloud player…")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        layout.addWidget(self._placeholder)

        program = program or system_python()
        if program is None or (args is None and not HELPER.is_file()):
            raise RuntimeError("system python3 or the player helper is missing")
        if args is None:
            args = ["-I", str(HELPER),
                    "--data", str(paths.data_dir / "listening-webkit"),
                    "--cache", str(paths.cache_dir / "listening-webkit")]
            if not self.embedded:
                args.append("--standalone")
        self.process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for name in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            env.remove(name)
        if self.embedded:
            env.insert("GDK_BACKEND", "x11")
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.start(program, args)

    # --- process I/O ------------------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if self.closed:
            return
        if not self._ready:
            self._pending.append(msg)
            return
        self.process.write((json.dumps(msg) + "\n").encode())

    def _read_stderr(self) -> None:
        # Kept only to explain an abnormal exit; never logged during normal operation.
        self._stderr_tail = (self._stderr_tail + bytes(self.process.readAllStandardError()))[-2048:]

    def _read_stdout(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self._handle(msg)

    def _handle(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "ready":
            self._ready = True
            xid = msg.get("xid")
            if isinstance(xid, int) and self.embedded:
                self._embed(xid)
            else:
                self._placeholder.setText("The SoundCloud player opened in its own window.")
            pending, self._pending = self._pending, []
            for m in pending:
                self._send(m)
        elif event == "url":
            self.url_changed.emit(str(msg.get("url", "")))
        elif event == "loaded":
            self.loaded.emit(bool(msg.get("ok")))
        elif event == "history":
            self.history_changed.emit(bool(msg.get("back")), bool(msg.get("forward")))
        elif event == "audio":
            self.playing = bool(msg.get("playing"))
            self.audio_changed.emit(self.playing)
        elif event == "playback_error":
            self.playback_error.emit()
        elif event == "external":
            self.external_requested.emit(str(msg.get("url", "")))
        elif event == "notice":
            text = NOTICES.get(str(msg.get("code")))
            if text:
                self.notice.emit(text)
        elif event == "crashed":
            self.notice.emit("The player page stopped unexpectedly. Choose Reload or Open in browser.")
        elif event == "unavailable":
            self.failed.emit(
                "The built-in player needs WebKitGTK. Install it with: sudo apt install "
                "gir1.2-webkit2-4.1 gstreamer1.0-libav — or use Open in browser.")

    def _embed(self, xid: int) -> None:
        self._window = QWindow.fromWinId(xid)
        self.container = QWidget.createWindowContainer(self._window, self.view)
        self.container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.container.setAccessibleName("SoundCloud website")
        self.view.layout().addWidget(self.container)
        self._placeholder.hide()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self.closed:
            self.failed.emit("The built-in player could not start (python3 not found). "
                             "Use Open in browser.")

    def _finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._drop_container()
        if self.closed or code == 3:
            return
        tail = re.sub(rb"\?\S*", b"?...", self._stderr_tail).decode("utf-8", "replace")
        log.warning("Listening player exited (code %s, %s): %s", code, status.name,
                    tail.strip()[-600:])
        self.playing = False
        self.audio_changed.emit(False)
        self.failed.emit("The built-in player stopped. Choose Home to restart it, or use "
                         "Open in browser.")

    def _drop_container(self) -> None:
        if self.container is not None:
            self.container.hide()
            self.container.deleteLater()
            self.container = None
            self._window = None

    # --- API used by ListeningView ----------------------------------------------------------

    def navigate(self, url: str) -> None:
        self._send({"cmd": "navigate", "url": url})

    def action(self, name: str) -> None:
        if name in ("back", "forward", "reload"):
            self._send({"cmd": name})

    def pause(self) -> None:
        if self.playing:
            self._send({"cmd": "pause"})

    def show_window(self) -> None:
        self._send({"cmd": "show"})

    def dispose(self, wait: bool = False) -> None:
        """Stop the player process (and its audio). ``wait`` blocks briefly: app exit."""
        if self.closed:
            return
        if self._ready:
            self.process.write(b'{"cmd": "quit"}\n')
        self.closed = True
        if self.container is not None:
            self.container.hide()          # unmap before the X window goes away
        if wait:
            if not self.process.waitForFinished(QUIT_WAIT_MS):
                self.process.kill()
                self.process.waitForFinished(QUIT_WAIT_MS)
        elif self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.closeWriteChannel()
            QTimer.singleShot(QUIT_WAIT_MS, self._kill)
        self._drop_container()
        self.view.deleteLater()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self.deleteLater()
        else:
            self.process.finished.connect(self.deleteLater)

    def _kill(self) -> None:
        try:
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.process.kill()
        except RuntimeError:       # already deleted
            pass
