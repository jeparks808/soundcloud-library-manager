"""Offline player policy, lazy loading, navigation, queue/history routing and the
WebKit helper protocol (driven by a fake helper process; no WebKitGTK needed)."""
import sys
import textwrap

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget
from sclm.gui.listening_policy import valid_url
from sclm.gui.listening_view import ListeningView
from sclm.gui.main_window import MainWindow, PAGE_LISTEN, PAGE_QUEUE


class FakeBrowser(QObject):
    url_changed = Signal(str)
    loaded = Signal(bool)
    notice = Signal(str)
    audio_changed = Signal(bool)
    history_changed = Signal(bool, bool)
    playback_error = Signal()
    external_requested = Signal(str)
    failed = Signal(str)
    def __init__(self, paths, parent):
        super().__init__(parent)
        self.view = QWidget(parent)
        self.urls = []
        self.actions = []
        self.pauses = 0
        self.disposed = False
    def navigate(self, url):
        self.urls.append(url)
    def action(self, name):
        self.actions.append(name)
    def pause(self):
        self.pauses += 1
    def dispose(self, wait=False):
        self.disposed = True
        self.view.deleteLater()


@pytest.mark.parametrize("url", ["https://soundcloud.com/a/b", "https://on.soundcloud.com/abc", "https://m.soundcloud.com/", "https://soundcloud.com:443/"])
def test_allowed_urls(url):
    assert valid_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "http://soundcloud.com", "https://soundcloud.com.evil.test/", "https://soundcloud.com@evil.test/", "https://user:password@soundcloud.com/", "https://soundcloud.com:8000/", "https://soundcloud.com:bad/", "https://soundcloud.com/\\evil", "https://soundcloud.com/\n", "https://[broken/", "https://example.org/"])
def test_rejected_urls(url):
    assert not valid_url(url)


def test_identity_hosts_only_when_requested():
    assert not valid_url("https://accounts.google.com/")
    assert valid_url("https://accounts.google.com/", identity=True)
    assert not valid_url("https://accounts.google.com.evil.test/", identity=True)


def test_lazy_load_stop_reopen_and_failure(qtbot, app_paths):
    view = ListeningView(app_paths, browser_factory=FakeBrowser)
    qtbot.addWidget(view)
    assert view.browser is None
    assert not view.open_url("file:///etc/passwd")
    assert view.browser is None
    view.activate()
    browser = view.browser
    view.activate()
    assert len(browser.urls) == 1
    browser.loaded.emit(False)
    assert "could not load" in view.status.text()
    browser.url_changed.emit("https://soundcloud.com/test/song")
    assert view.address.text().endswith("test/song")
    browser.url_changed.emit("https://evil.test/")
    assert view.address.text().endswith("test/song")
    view.stop()
    assert browser.disposed and view.browser is None
    view.activate()
    assert view.browser is None
    view.go()
    assert view.browser is not browser
    view.stop()


def test_missing_engine_fallback(qtbot, app_paths):
    def missing(*_):
        raise ImportError("secret path")
    view = ListeningView(app_paths, browser_factory=missing)
    qtbot.addWidget(view)
    view.activate()
    assert "Open in browser" in view.status.text()
    assert "secret" not in view.status.text()
    assert view.browser is None


def test_external_uses_actual_page(qtbot, app_paths, monkeypatch):
    from sclm.gui.listening_view import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    view = ListeningView(app_paths, browser_factory=FakeBrowser)
    qtbot.addWidget(view)
    view.open_url("https://soundcloud.com/a/b")
    view.address.setText("file:///etc/passwd")
    view.open_external()
    assert opened == ["https://soundcloud.com/a/b"]
    view.stop()


def test_queue_routes_without_reloading_on_page_switch(qtbot, ctx, make_job):
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.listening_view.browser_factory = FakeBrowser
    job = make_job(1)
    window.queue_view.model.set_jobs([job])
    window.queue_view.table.selectRow(0)
    window.queue_view.listen_btn.click()
    assert window.current_page() == PAGE_LISTEN
    browser = window.listening_view.browser
    assert browser.urls == [job.track.url]
    window.show_page(PAGE_QUEUE)
    window.show_page(PAGE_LISTEN)
    assert browser.urls == [job.track.url] and not browser.disposed
    window.close()
    assert browser.disposed


def test_history_route(qtbot, ctx, make_track):
    from sclm.core.models import JobStatus
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.listening_view.browser_factory = FakeBrowser
    track = make_track("99")
    ctx.storage.upsert_track(track)
    # Model accepts the fields used by history without requiring a download job.
    from types import SimpleNamespace
    record = SimpleNamespace(track_id=track.track_id, status=JobStatus.FAILED,
        file_path=None, artist=track.artist, title=track.title, error=None,
        attempts=1, bitrate_kbps=None, updated_at="", created_at="")
    window.history_view.model.set_records([record])
    window.history_view.table.selectRow(0)
    window.history_view.listen_btn.click()
    qtbot.waitUntil(lambda: window.current_page() == PAGE_LISTEN)
    assert window.listening_view.browser.urls == [track.url]
    window.close()


def test_main_frame_identity_address_is_visible(qtbot, app_paths, monkeypatch):
    from sclm.gui.listening_view import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    view = ListeningView(app_paths, browser_factory=FakeBrowser)
    qtbot.addWidget(view)
    view.open_url("https://soundcloud.com/a/b")
    view.browser.url_changed.emit("https://accounts.google.com/signin")
    assert view.address.text() == "https://accounts.google.com/signin"
    view.open_external()
    assert opened == ["https://soundcloud.com/a/b"]
    view.stop()


def test_engine_signals_drive_the_page(qtbot, app_paths, monkeypatch):
    from sclm.gui.listening_view import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    view = ListeningView(app_paths, browser_factory=FakeBrowser)
    qtbot.addWidget(view)
    view.show()
    view.open_url("https://soundcloud.com/a/b")
    browser = view.browser
    view.back.click()
    view.reload.click()
    assert browser.actions == ["back", "reload"]
    browser.history_changed.emit(False, True)
    assert not view.back.isEnabled() and view.forward.isEnabled()
    audio = []
    view.web_audio_changed.connect(audio.append)
    browser.playback_error.emit()
    assert view.error_box.isVisible()
    browser.audio_changed.emit(True)
    assert audio == [True] and not view.error_box.isVisible()
    view.pause_web()
    assert browser.pauses == 1
    browser.external_requested.emit("https://instagram.example/artist")
    browser.external_requested.emit("http://insecure.example/")
    assert opened == ["https://instagram.example/artist"]
    browser.failed.emit("The built-in player stopped.")
    assert view.browser is None and browser.disposed
    assert audio == [True, False]
    assert "stopped" in view.status.text()
    view.stop()


def test_web_and_local_playback_pause_each_other(qtbot, ctx):
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.listening_view.browser_factory = FakeBrowser
    window.listen("https://soundcloud.com/a/b")
    browser = window.listening_view.browser
    paused = []
    window.player.pause = lambda: paused.append("local")
    browser.audio_changed.emit(True)
    assert paused == ["local"]
    window.player.playing_changed.emit(True)
    assert browser.pauses == 1
    window.close()
    assert browser.disposed


FAKE_HELPER = textwrap.dedent(r"""
    import json, sys
    def out(**m):
        sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
    out(event="ready")
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["cmd"] == "navigate":
            out(event="url", url=cmd["url"]); out(event="loaded", ok=True)
            out(event="history", back=True, forward=False)
            out(event="audio", playing=True); out(event="playback_error")
            out(event="notice", code="download"); out(event="notice", code="unknown-code")
            out(event="external", url="https://example.org/x")
        elif cmd["cmd"] == "pause":
            out(event="audio", playing=False)
        elif cmd["cmd"] == "quit":
            break
""")


def test_webkit_browser_protocol_with_fake_helper(qtbot, app_paths, tmp_path, monkeypatch):
    from sclm.gui import listening_browser
    monkeypatch.setattr(listening_browser, "embeddable", lambda: False)
    helper = tmp_path / "fake_helper.py"
    helper.write_text(FAKE_HELPER)
    parent = QWidget()
    qtbot.addWidget(parent)
    browser = listening_browser.WebKitBrowser(app_paths, parent, program=sys.executable,
                                              args=["-I", str(helper)])
    seen = {"url": [], "loaded": [], "notice": [], "audio": [], "history": [],
            "error": [], "external": []}
    browser.url_changed.connect(seen["url"].append)
    browser.loaded.connect(seen["loaded"].append)
    browser.notice.connect(seen["notice"].append)
    browser.audio_changed.connect(seen["audio"].append)
    browser.history_changed.connect(lambda b, f: seen["history"].append((b, f)))
    browser.playback_error.connect(lambda: seen["error"].append(True))
    browser.external_requested.connect(seen["external"].append)
    browser.navigate("https://soundcloud.com/a/b")     # queued until "ready"
    qtbot.waitUntil(lambda: bool(seen["external"]), timeout=10000)
    assert seen["url"] == ["https://soundcloud.com/a/b"] and seen["loaded"] == [True]
    assert seen["history"] == [(True, False)] and seen["error"] == [True]
    assert len(seen["notice"]) == 1 and "Download page" in seen["notice"][0]
    assert seen["external"] == ["https://example.org/x"] and browser.playing
    browser.pause()
    qtbot.waitUntil(lambda: seen["audio"] == [True, False], timeout=5000)
    process = browser.process
    browser.dispose(wait=True)
    assert process.state() == process.ProcessState.NotRunning
    assert process.exitCode() == 0


def test_webkit_browser_reports_unavailable_engine(qtbot, app_paths, tmp_path, monkeypatch):
    from sclm.gui import listening_browser
    monkeypatch.setattr(listening_browser, "embeddable", lambda: False)
    helper = tmp_path / "missing_webkit.py"
    helper.write_text('import sys; print(\'{"event": "unavailable", "reason": "ImportError"}\', '
                      'flush=True); sys.exit(3)\n')
    parent = QWidget()
    qtbot.addWidget(parent)
    browser = listening_browser.WebKitBrowser(app_paths, parent, program=sys.executable,
                                              args=["-I", str(helper)])
    failures = []
    browser.failed.connect(failures.append)
    qtbot.waitUntil(lambda: bool(failures), timeout=10000)
    assert "WebKitGTK" in failures[0] and "Open in browser" in failures[0]
    qtbot.waitUntil(lambda: browser.process.state() == browser.process.ProcessState.NotRunning)
    assert len(failures) == 1          # exit code 3 is not reported a second time
    browser.dispose()
