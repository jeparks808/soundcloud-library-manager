"""Playback smoke test of the real main window, in isolated temporary app/Qt-settings data.

    python packaging/sclm_playback_smoke.py [--network] [--screenshot-dir DIR]
    SoundCloud_Library_Manager-*.AppImage --playback-smoke [--network] [--screenshot-dir DIR]

Local: generated MP3s (one with cover art) are added to the library, played by double-click,
kept playing across page switches, seeked, paused, advanced, and a missing file is skipped.
Web (needs WebKitGTK): the Listen page starts one embedded player process. With --network
on an X11 desktop it opens SoundCloud's official embed player for a public track, presses
its play button with a real (XTest) click, checks that audio keeps playing on other pages,
and that local and web playback pause each other. No user data, login or music library is
read.

Silence: all audio goes to a temporary PipeWire/PulseAudio null sink (PULSE_SINK), removed
at the end. Nothing is muted at the sound-server level: WirePlumber remembers stream mutes
per media role, and a test mute would silence the user's own music streams afterwards.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WIDGET = ("https://w.soundcloud.com/player/?url=https%3A//soundcloud.com/polonimusic/"
          "boom-back-ghetto-life-archive&auto_play=false")


def _load_null_sink() -> str | None:
    """Route this process' (and its children's) audio to a throwaway sink."""
    try:
        out = subprocess.run(["pactl", "load-module", "module-null-sink",
                              "sink_name=sclm_smoke_null",
                              "sink_properties=device.description=SCLM-smoke-test"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is None or out.returncode:
        print("note: pactl unavailable; smoke-test audio will be audible", flush=True)
        return None
    os.environ["PULSE_SINK"] = "sclm_smoke_null"
    return out.stdout.strip()


def _x_ancestors(xid: int) -> list[int]:
    """X window ids from ``xid``'s parent up to the root (XQueryTree)."""
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    display = ctypes.c_void_p(x11.XOpenDisplay(None))
    chain = []
    try:
        while True:
            root, parent = ctypes.c_ulong(), ctypes.c_ulong()
            children, count = ctypes.c_void_p(), ctypes.c_uint()
            if not x11.XQueryTree(display, ctypes.c_ulong(xid), ctypes.byref(root),
                                  ctypes.byref(parent), ctypes.byref(children),
                                  ctypes.byref(count)):
                break
            if children:
                x11.XFree(children)
            chain.append(parent.value)
            if parent.value in (0, root.value):
                break
            xid = parent.value
    finally:
        x11.XCloseDisplay(display)
    return chain


def run(network: bool = False, screenshot_dir: str | None = None) -> int:
    tmp_root = tempfile.TemporaryDirectory(prefix="sclm-playback-smoke-")
    tmp = Path(tmp_root.name)
    os.environ["SCLM_HOME"] = str(tmp / "home")
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "xdg-config")     # QSettings stay isolated
    null_sink = _load_null_sink()
    shots = Path(screenshot_dir) if screenshot_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["sclm-playback-smoke"])
    app.setApplicationName("sclm-playback-smoke")
    from sclm.app import _shutdown, build_core
    from sclm.context import AppContext
    from sclm.core.models import LibraryFile, LibrarySource
    from sclm.core.paths import AppPaths
    from sclm.core.ffmpeg import resolve_ffmpeg
    from sclm.download.manager import DownloadManager
    from sclm.gui.bridge import DownloadBridge
    from sclm.gui.main_window import PAGE_DOWNLOAD, PAGE_LIBRARY, PAGE_LISTEN, MainWindow
    from sclm.gui.theme import apply_theme

    apply_theme(app)
    t0 = time.monotonic()

    def say(text: str) -> None:
        print(f"[{time.monotonic() - t0:6.2f}s] {text}", flush=True)

    def until(predicate, timeout_ms: int = 15000, what: str = "condition") -> None:
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        poll.start(40)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        poll.stop()
        if not predicate():
            raise AssertionError(f"timed out waiting for {what}")

    def wait(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def shot(window, name: str) -> None:
        if not shots:
            return
        if QGuiApplication.platformName() == "xcb":     # includes the embedded X window
            g = window.geometry()
            image = window.screen().grabWindow(0, g.x(), g.y(), g.width(), g.height())
        else:
            image = window.grab()
        image.save(str(shots / f"{name}.png"))

    ffmpeg = str(resolve_ffmpeg(None).path)
    media = tmp / "music"
    media.mkdir()
    cover = media / "cover.png"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=0xff5500:s=96x96",
                    "-frames:v", "1", str(cover)], check=True, timeout=30)
    songs = []
    for i, (freq, seconds) in enumerate(((440, 8), (550, 3))):
        out = media / f"smoke-{i}.mp3"
        args = [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                f"sine=frequency={freq}:duration={seconds}"]
        if i == 0:
            args += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "png",
                     "-disposition:v", "attached_pic", "-id3v2_version", "3"]
        subprocess.run(args + ["-c:a", "libmp3lame", "-b:a", "128k", str(out)], check=True,
                       timeout=30)
        songs.append(out)

    core = build_core(AppPaths.under(Path(os.environ["SCLM_HOME"])))
    bridge = DownloadBridge()
    manager = DownloadManager(core.storage, core.settings, core.paths, listener=bridge)
    for title, path in (("Smoke tone A", songs[0]), ("Missing file", media / "missing.mp3"),
                        ("Smoke tone B", songs[1])):
        core.storage.add_library_file(LibraryFile(path=path, source=LibrarySource.SCAN,
                                                  artist="SCLM", title=title))
    window = MainWindow(AppContext(core.paths, core.settings, core.storage, manager, bridge))
    window.resize(1280, 800)
    window.show()
    player = window.player
    player.set_muted(True)      # Qt's mute is in-process only; never a sound-server mute
    failures = 0
    try:
        # --- local playback ------------------------------------------------------------------
        window.show_page(PAGE_LIBRARY)
        table = window.library_view.table
        until(lambda: window.library_view.model.rowCount() == 3, what="library rows")
        row = next(r for r in range(3)
                   if window.library_view.model.file_at(r).title == "Smoke tone A")
        rect = table.visualRect(window.library_view.model.index(row, 1))
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        until(lambda: player.playing and player.position() > 300, what="local playback")
        bar = window.player_bar
        assert bar.title.text() == "Smoke tone A", bar.title.text()
        until(lambda: bar.art.pixmap() is not None and not bar.art.pixmap().isNull(),
              what="cover art")
        say(f"PASS local: double-click played '{bar.title.text()}', artwork shown, "
            f"position {player.position()} ms")
        shot(window, "1-library-playing")
        window.show_page(PAGE_DOWNLOAD)
        before = player.position()
        wait(1200)
        assert player.playing and player.position() > before + 600
        say(f"PASS local: kept playing on another page ({before} -> {player.position()} ms)")
        player.seek(5000)
        until(lambda: player.position() >= 5000, what="seek")
        player.pause()
        until(lambda: not player.playing, what="pause")
        player.play()
        until(lambda: player.playing, what="resume")
        bar.volume.setValue(35)                 # moving the volume unmutes
        assert player.volume == 35 and player.muted is False
        bar.mute_btn.click()
        assert player.muted and bar.mute_btn.accessibleName() == "Unmute"
        say("PASS local: seek to 5 s, pause/resume, volume and mute")
        messages = []
        player.message.connect(messages.append)
        by_title = {f.title: f for f in window.library_view.model.files()}
        queue = [by_title["Smoke tone A"], by_title["Missing file"], by_title["Smoke tone B"]]
        player.play_files(queue, 0)
        until(lambda: player.playing, what="restart of the queue")
        player.next()
        until(lambda: player.index == 2 and player.playing, 8000, "skip of the missing file")
        assert any("missing.mp3" in m for m in messages), messages
        say(f"PASS local: Next hit a missing file; reported ({messages[0]!r}) and skipped "
            "to the next track")
        until(lambda: not player.playing, 8000, "end of queue")
        say("PASS local: stopped at the end of the queue")

        # --- web player ----------------------------------------------------------------------
        listen = window.listening_view
        window.show_page(PAGE_LISTEN)
        until(lambda: listen.browser is not None and (
            getattr(listen.browser, "_ready", False) or listen.browser is None), 20000,
            "WebKit player start")
        browser = listen.browser
        process = browser.process
        exited: list[int] = []
        process.finished.connect(lambda code, _status: exited.append(code))
        until(lambda: browser.container is not None or not browser.embedded, 5000, "embedding")
        say(f"PASS web: player process pid {process.processId()} started "
            f"({'embedded' if browser.embedded else 'own window'})")
        if browser.embedded:
            # The helper window must stay inside the container. A window manager that adopts
            # it (override-redirect lost) reparents it into a decorated frame instead.
            wait(1500)
            chain = _x_ancestors(int(browser._window.winId()))
            assert int(window.winId()) in chain, \
                f"helper window ancestors {[hex(w) for w in chain]} miss the main window"
            say("PASS web: helper window embedded in the container, not managed by the WM")
        loaded: list[bool] = []
        browser.loaded.connect(loaded.append)
        browser.audio_changed.connect(lambda p: say(f"  website audio playing={p}"))
        player.playing_changed.connect(lambda p: say(f"  local playing={p}"))
        if network:
            listen.open_url(WIDGET)
            until(lambda: bool(loaded) and loaded[-1], 45000, "SoundCloud embed page")
            wait(2500)
            shot(window, "2-listen-loaded")
            if browser.embedded:
                x11 = ctypes.CDLL("libX11.so.6")
                xtst = ctypes.CDLL("libXtst.so.6")
                x11.XOpenDisplay.restype = ctypes.c_void_p
                display = ctypes.c_void_p(x11.XOpenDisplay(None))
                player.play_index(0)
                until(lambda: player.playing, what="local playback before the web test")
                window.raise_()
                window.activateWindow()
                wait(300)
                # The embed player's own Play button (anonymous: only valid before a pause,
                # after which SoundCloud shows an "explore more" overlay there).
                origin = browser.container.mapToGlobal(QPoint(0, 0))
                xtst.XTestFakeMotionEvent(display, -1, origin.x() + 197, origin.y() + 33, 0)
                xtst.XTestFakeButtonEvent(display, 1, 1, 0)
                xtst.XTestFakeButtonEvent(display, 1, 0, 0)
                x11.XFlush(display)
                until(lambda: browser.playing, 20000, "website audio after clicking Play")
                say("PASS web: SoundCloud AAC stream playing in the embedded player")
                until(lambda: not player.playing, 5000, "local pause when the website plays")
                say("PASS mix: website playback paused local playback")
                window.show_page(PAGE_LIBRARY)
                wait(4000)
                assert browser.playing and listen.browser is browser
                assert process.state() == process.ProcessState.Running
                window.show_page(PAGE_LISTEN)
                say("PASS web: kept playing on another page; same process, no reload")
                shot(window, "3-listen-playing")
                player.play()
                until(lambda: not browser.playing, 10000, "web pause when local starts")
                assert player.playing
                say("PASS mix: resuming local playback paused the website (MPRIS)")
                shot(window, "4-local-after-web")
            else:
                say("SKIP web audio click: not an X11 session")
        else:
            say("SKIP web network checks (pass --network)")
        listen.stop()
        until(lambda: bool(exited), 5000, "player process exit")
        say("PASS web: Stop listening ended the player process")
    except AssertionError as exc:
        failures += 1
        say(f"FAIL: {exc}")
        shot(window, "failure")
    finally:
        window.close()
        app.processEvents()
        _shutdown(manager, core.storage)
        tmp_root.cleanup()
        if null_sink:
            subprocess.run(["pactl", "unload-module", null_sink], check=False)
    say("RESULT: " + ("PASS" if not failures else "FAIL"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    shots_arg = sys.argv[sys.argv.index("--screenshot-dir") + 1] \
        if "--screenshot-dir" in sys.argv else None
    sys.exit(run(network="--network" in sys.argv, screenshot_dir=shots_arg))
