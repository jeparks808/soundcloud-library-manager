"""SoundCloud website player process: system WebKitGTK + GStreamer (run by the system python3).

Never imported by the application. ``listening_browser.WebKitBrowser`` starts it as
``python3 -I webkit_player.py --data DIR --cache DIR [--standalone]`` because the PySide6
QtWebEngine has no AAC decoder and SoundCloud now streams AAC (HLS ``mp4a.40.2``) only. The
distribution's WebKitGTK decodes it through GStreamer (``gstreamer1.0-libav``).

Protocol: one JSON object per line. Commands arrive on stdin (``navigate``, ``back``,
``forward``, ``reload``, ``pause``, ``quit``); events leave on the saved stdout (``ready``,
``url``, ``loaded``, ``history``, ``audio``, ``notice``, ``external``, ``playback_error``,
``crashed``, ``unavailable``). Library output is redirected to stderr so it can't corrupt
the protocol.

Embedded mode (X11): the window is an override-redirect GTK window, mapped off screen
first (GTK paints only after its own MapNotify) and with compositor frame sync disabled
(GDK otherwise waits forever for _NET_WM_FRAME_DRAWN once Qt reparents the window).
Its XID is sent in ``ready``; Qt embeds it with ``QWindow.fromWinId``.

No cookie export, request interception, media saving or DRM workarounds. The only scripts
run in the page are read-only state checks and a click on SoundCloud's own pause button,
both in an isolated script world.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Keep the protocol channel private before any library can print to stdout.
_PROTO = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)


def emit(event: str, **fields: object) -> None:
    try:
        _PROTO.write(json.dumps({"event": event, **fields}) + "\n")
    except (BrokenPipeError, OSError):
        os._exit(0)


try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as exc:
    emit("unavailable", reason=type(exc).__name__)
    sys.exit(3)

# WebKit publishes the page's media session over MPRIS as org.mpris.MediaPlayer2.<prgname>.*;
# the desktop's sound applet and media keys use it, and so does the "pause" command.
PRGNAME = "sclm_soundcloud"
GLib.set_prgname(PRGNAME)
GLib.set_application_name("SoundCloud (Library Manager)")
MPRIS_PREFIX = "org.mpris.MediaPlayer2." + PRGNAME

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from listening_policy import valid_url  # noqa: E402

MAX_POPUPS = 4
ERROR_CHECK_MS = 3000
WORLD = "sclm"
# Read-only: SoundCloud shows this text when its player can't play a stream.
ERROR_CHECK_JS = ("document.body ? document.body.textContent.indexOf("
                  "'There was a problem playing') >= 0 : false")
# Fallback when no MPRIS session is found: SoundCloud's own pause control (site, widget).
PAUSE_JS = ("(() => { const b = document.querySelector('.playControls__play.playing, "
            ".playButton.playing'); if (b) { b.click(); return true; } return false; })()")


def _signin_host(uri: str) -> bool:
    """SoundCloud's own sign-in pages (secure.soundcloud.com, /signin)."""
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    return parts.hostname == "secure.soundcloud.com" or parts.path.startswith("/signin")


def _allowed(uri: str) -> bool:
    """Any HTTPS frame (SoundCloud embeds third-party frames), plus internal documents."""
    return uri.startswith(("https://", "about:", "blob:", "data:"))


class Player:
    def __init__(self, data_dir: str, cache_dir: str, standalone: bool) -> None:
        for d in (data_dir, cache_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
        manager = WebKit2.WebsiteDataManager(base_data_directory=data_dir,
                                             base_cache_directory=cache_dir)
        manager.get_cookie_manager().set_persistent_storage(
            os.path.join(data_dir, "cookies.sqlite"), WebKit2.CookiePersistentStorage.SQLITE)
        self.context = WebKit2.WebContext.new_with_website_data_manager(manager)
        self.context.set_cache_model(WebKit2.CacheModel.DOCUMENT_BROWSER)
        self.context.connect("download-started", self._download_started)
        self.popups: list[Gtk.Window] = []
        self.error_shown = False
        self.playing = False

        self.view = self._new_view()
        self.view.connect("notify::uri", self._uri_changed)
        self.view.connect("notify::is-playing-audio", self._audio_changed)
        self.view.connect("load-changed", self._load_changed)
        self.view.connect("load-failed", self._load_failed)
        self.view.connect("web-process-terminated", lambda *_a: emit("crashed"))

        if standalone:
            self.window = Gtk.Window(title="SoundCloud · SoundCloud Library Manager")
            self.window.set_default_size(1100, 760)
            self.window.connect("delete-event", lambda *_a: self.window.hide() or True)
            self.window.add(self.view)
            self.window.show_all()
            emit("ready")
        else:
            self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
            self.window.set_default_size(960, 640)
            self.window.add(self.view)
            self.window.realize()
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11

            GdkX11.X11Window.set_frame_sync_enabled(self.window.get_window(), False)
            self.window.move(-10000, -10000)
            self._announced = False
            self.window.connect("map-event", self._mapped)
            self.window.show_all()
        GLib.timeout_add(ERROR_CHECK_MS, self._check_error)

    def _mapped(self, *_args: object) -> bool:
        if not self._announced:
            self._announced = True
            emit("ready", xid=self.window.get_window().get_xid())
        return False

    def _new_view(self, related: WebKit2.WebView | None = None) -> WebKit2.WebView:
        view = (WebKit2.WebView(related_view=related) if related is not None
                else WebKit2.WebView(web_context=self.context))
        settings = view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_write_console_messages_to_stdout(False)
        settings.set_javascript_can_open_windows_automatically(False)
        # SoundCloud resumes a paused track from an async callback; requiring a user gesture
        # there made Play do nothing after the first pause.
        settings.set_media_playback_requires_user_gesture(False)
        settings.set_enable_mediasource(True)
        if os.environ.get("SCLM_WEBKIT_NO_GPU"):
            settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        view.connect("decide-policy", self._decide_policy)
        view.connect("create", self._create)
        view.connect("permission-request", lambda _v, request: request.deny() or True)
        return view

    # --- policy -----------------------------------------------------------------------------

    def _decide_policy(self, _view, decision, kind) -> bool:
        if kind == WebKit2.PolicyDecisionType.RESPONSE:
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            return False            # handled by "create"
        if (action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED
                and action.is_user_gesture() and uri.startswith("https://")
                and not valid_url(uri, identity=True)):
            decision.ignore()
            emit("external", url=uri)
            return True
        if not _allowed(uri):
            decision.ignore()
            emit("notice", code="blocked")
            return True
        return False

    def _create(self, view, action) -> WebKit2.WebView | None:
        """New windows: sign-in flows get a popup (they need window.opener); other
        SoundCloud links open in the player itself; anything else in the system browser."""
        uri = action.get_request().get_uri() or ""
        signin = (not uri or uri.startswith("about:") or _signin_host(uri)
                  or (valid_url(uri, identity=True) and not valid_url(uri)))
        if not signin:
            if valid_url(uri) and view is self.view:
                self.view.load_uri(uri)
            elif uri.startswith("https://"):
                emit("external", url=uri)
            return None
        if len(self.popups) >= MAX_POPUPS:
            return None
        popup = self._new_view(related=view)
        window = Gtk.Window(title="SoundCloud · Sign in")
        window.set_default_size(560, 720)
        window.add(popup)
        self.popups.append(window)

        def closed(*_args: object) -> None:
            if window in self.popups:
                self.popups.remove(window)
                window.destroy()

        popup.connect("ready-to-show", lambda *_a: window.show_all())
        popup.connect("close", closed)
        window.connect("destroy", closed)
        return popup

    def _download_started(self, _context, download) -> None:
        download.cancel()
        emit("notice", code="download")

    # --- state ------------------------------------------------------------------------------

    def _uri_changed(self, view, _pspec) -> None:
        emit("url", url=view.get_uri() or "")
        self._history()

    def _history(self) -> None:
        emit("history", back=self.view.can_go_back(), forward=self.view.can_go_forward())

    def _audio_changed(self, view, _pspec) -> None:
        playing = bool(view.is_playing_audio())
        if playing != self.playing:
            self.playing = playing
            if playing:
                self.error_shown = False
            emit("audio", playing=playing)

    def _load_changed(self, _view, event) -> None:
        if event == WebKit2.LoadEvent.FINISHED:
            emit("loaded", ok=True)
            self._history()

    def _load_failed(self, _view, _event, _uri, error) -> bool:
        cancelled = error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED)
        interrupted = error.matches(WebKit2.policy_error_quark(),
                                    WebKit2.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE)
        if not (cancelled or interrupted):
            emit("loaded", ok=False)
        return False

    def _check_error(self) -> bool:
        if not self.playing and self.view.get_uri():
            self.view.evaluate_javascript(ERROR_CHECK_JS, -1, WORLD, None, None,
                                          self._error_checked)
        return True

    def _error_checked(self, view, result) -> None:
        try:
            found = view.evaluate_javascript_finish(result).to_boolean()
        except GLib.Error:
            return
        if found and not self.error_shown:
            emit("playback_error")
        self.error_shown = found

    # --- commands ---------------------------------------------------------------------------

    def command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "navigate":
            url = str(msg.get("url", ""))
            if valid_url(url):
                self.error_shown = False
                self.view.load_uri(url)
        elif cmd == "back":
            self.view.go_back()
        elif cmd == "forward":
            self.view.go_forward()
        elif cmd == "reload":
            self.view.reload()
        elif cmd == "pause":
            if self.playing and not self._mpris_pause():
                self.view.evaluate_javascript(PAUSE_JS, -1, WORLD, None, None, self._paused)
        elif cmd == "show" and isinstance(self.window, Gtk.Window):
            self.window.present()
        elif cmd == "quit":
            Gtk.main_quit()

    def _mpris_pause(self) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                  "org.freedesktop.DBus", "ListNames", None,
                                  GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 500,
                                  None).unpack()[0]
        except GLib.Error:
            return False
        ours = [n for n in names if n == MPRIS_PREFIX or n.startswith(MPRIS_PREFIX + ".")]
        for name in ours:
            bus.call(name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "Pause",
                     None, None, Gio.DBusCallFlags.NONE, 500, None, None)
        return bool(ours)

    def _paused(self, view, result) -> None:
        try:
            ok = view.evaluate_javascript_finish(result).to_boolean()
        except GLib.Error:
            ok = False
        if not ok:
            emit("notice", code="pause_failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    player = Player(args.data, args.cache, args.standalone)
    buffer = bytearray()

    def readable(fd: int, condition: GLib.IOCondition) -> bool:
        chunk = os.read(fd, 65536) if condition & GLib.IOCondition.IN else b""
        if not chunk:
            Gtk.main_quit()          # the application went away
            return False
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer[:] = rest
            try:
                player.command(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                continue
        return True

    GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, 0,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                          readable)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, lambda: Gtk.main_quit() or False)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
