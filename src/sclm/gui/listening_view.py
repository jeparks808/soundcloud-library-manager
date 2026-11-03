"""Listen page: the official SoundCloud website, played by the embedded WebKitGTK engine.

The engine starts lazily on first use and keeps running (and playing) while other pages
are shown. Download services never receive the website's sign-in.
"""
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sclm.gui.common import set_role
from sclm.gui.listening_policy import HOME, valid_url

PLAYBACK_ERROR_TEXT = (
    "SoundCloud couldn't play this track in the built-in player. Protected content (for "
    "example some SoundCloud Go+ tracks) may need a browser with DRM support.")


def create_browser(paths, parent):
    from sclm.gui.listening_browser import WebKitBrowser
    return WebKitBrowser(paths, parent)


class ListeningView(QWidget):
    web_audio_changed = Signal(bool)   # the website started/stopped playing audio

    def __init__(self, paths, parent=None, browser_factory=None):
        super().__init__(parent)
        self.paths = paths
        self.browser_factory = browser_factory or create_browser
        self.browser = None
        self.started = False
        self.current_url = HOME
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        nav = QHBoxLayout()
        self.back = self.button("Back", lambda: self.action("back"), nav)
        self.forward = self.button("Forward", lambda: self.action("forward"), nav)
        self.reload = self.button("Reload", lambda: self.action("reload"), nav)
        self.button("Home", lambda: self.open_url(HOME), nav)
        self.address = QLineEdit(HOME)
        self.address.setMinimumWidth(180)
        self.address.setAccessibleName("SoundCloud listening URL")
        self.address.returnPressed.connect(self.go)
        nav.addWidget(self.address, 1)
        self.button("Go", self.go, nav)
        self.button("Open in browser", self.open_external, nav)
        self.stop_button = self.button("Stop listening", self.stop, nav)
        layout.addLayout(nav)
        self.status = set_role(QLabel("Open Listen to load the player."), "muted")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Listening status")
        layout.addWidget(self.status)

        self.error_box = QFrame()
        self.error_box.setObjectName("errorBanner")
        error_row = QHBoxLayout(self.error_box)
        error_row.setContentsMargins(10, 6, 10, 6)
        self.error_label = QLabel(PLAYBACK_ERROR_TEXT)
        self.error_label.setWordWrap(True)
        error_row.addWidget(self.error_label, 1)
        self.button("Open this track in my browser", self.open_external, error_row)
        self.button("Dismiss", self.error_box.hide, error_row)
        self.error_box.hide()
        layout.addWidget(self.error_box)

        self.content = QVBoxLayout()
        layout.addLayout(self.content, 1)
        self.placeholder = QLabel(
            "Listen to SoundCloud here, including tracks you can't download. Sign in on the "
            "website to use your account and SoundCloud Go+. The sign-in stays on this "
            "computer and is never used for downloads. Music keeps playing when you switch "
            "pages.")
        self.placeholder.setWordWrap(True)
        self.content.addWidget(self.placeholder)
        self.content.addStretch(1)
        self.controls(False)

    def button(self, title, callback, layout):
        button = QPushButton(title)
        button.setAccessibleName(title)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def controls(self, enabled):
        for button in (self.back, self.forward, self.reload, self.stop_button):
            button.setEnabled(enabled)

    def activate(self):
        if not self.started:
            self.open_url(self.current_url)

    def go(self):
        self.open_url(self.address.text().strip())

    def open_url(self, url):
        if not valid_url(url):
            self.status.setText("Enter an HTTPS SoundCloud track, playlist, or profile URL.")
            return False
        self.started = True
        self.current_url = url
        self.address.setText(url)
        self.error_box.hide()
        if self.browser is None:
            try:
                self.browser = self.browser_factory(self.paths, self)
            except (ImportError, RuntimeError, OSError):
                self.status.setText("The built-in player is unavailable on this system. "
                                    "Use Open in browser.")
                return False
            self.content.insertWidget(0, self.browser.view, 1)
            self.browser.url_changed.connect(self.url_changed)
            self.browser.loaded.connect(self.loaded)
            self.browser.notice.connect(self.status.setText)
            for name, slot in (("audio_changed", self._audio_changed),
                               ("history_changed", self._history_changed),
                               ("playback_error", self._playback_error),
                               ("external_requested", self._open_external_url),
                               ("failed", self._failed)):
                signal = getattr(self.browser, name, None)
                if signal is not None:
                    signal.connect(slot)
            self.placeholder.hide()
            self.controls(True)
        self.status.setText("Loading SoundCloud…")
        self.browser.navigate(url)
        return True

    def url_changed(self, value):
        if valid_url(value, identity=True):
            self.address.setText(value)
        if valid_url(value):
            self.current_url = value

    def loaded(self, ok):
        self.status.setText("SoundCloud · Use the website’s play and sign-in controls." if ok else
                            "Page could not load. Check your connection or use Open in browser.")

    def action(self, name):
        if self.browser:
            self.browser.action(name)

    def pause_web(self):
        """Pause the website's player (used when local playback starts)."""
        if self.browser is not None and hasattr(self.browser, "pause"):
            self.browser.pause()

    def _audio_changed(self, playing):
        if playing:
            self.error_box.hide()
        self.web_audio_changed.emit(playing)

    def _history_changed(self, back, forward):
        self.back.setEnabled(back)
        self.forward.setEnabled(forward)

    def _playback_error(self):
        self.error_box.show()

    def _failed(self, text):
        self.status.setText(text)
        self._release_browser()
        self.placeholder.setText(text)
        self.placeholder.show()
        self.web_audio_changed.emit(False)

    def open_external(self):
        self._open_external_url(self.current_url)

    def _open_external_url(self, url):
        if not url.startswith("https://"):
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status.setText("Could not open your browser. Copy the URL into your browser manually.")

    def _release_browser(self, wait=False):
        if self.browser:
            browser, self.browser = self.browser, None
            self.content.removeWidget(browser.view)
            browser.dispose(wait) if wait else browser.dispose()
        self.controls(False)

    def stop(self, wait=False):
        """Close the player (stops website audio, frees its memory). Sign-in is kept."""
        was_running = self.browser is not None
        self.started = True
        self._release_browser(wait)
        self.error_box.hide()
        if was_running:
            self.web_audio_changed.emit(False)
        self.placeholder.setText("Player closed. Choose Go or Home to reopen it. "
                                 "Your SoundCloud sign-in is kept on this computer.")
        self.placeholder.show()
        self.status.setText("Player closed.")

    def shutdown(self):
        """App exit: stop the player process synchronously."""
        self._release_browser(wait=True)
