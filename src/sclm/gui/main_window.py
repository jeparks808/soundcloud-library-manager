"""Main application window: side navigation, page stack, status bar, close confirmation."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sclm import APP_DISPLAY_NAME, __version__
from sclm.context import AppContext
from sclm.gui import theme
from sclm.gui.history_view import HistoryView
from sclm.gui.listening_view import ListeningView
from sclm.gui.library_view import LibraryView
from sclm.gui.player import PlayerBar, PlayerController
from sclm.gui.preview_panel import PreviewPanel
from sclm.gui.queue_view import QueueView
from sclm.gui.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

PAGE_DOWNLOAD, PAGE_QUEUE, PAGE_LIBRARY, PAGE_HISTORY, PAGE_LISTEN = range(5)
PAGE_NAMES = ("Download", "Queue", "Library", "History", "Listen")
REFRESH_DEBOUNCE_MS = 800
STATUS_MESSAGE_MS = 6000


class MainWindow(QMainWindow):
    """Top-level window: Download / Queue / Library / History / Listen pages, the local
    player bar, and Settings.

    Receives everything through ``ctx``; constructs no services itself. The queue view
    connects ``ctx.bridge`` signals to its model. On close, asks for confirmation if
    ``not ctx.manager.is_idle()``; ``app.main`` performs the actual shutdown.
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self._ctx = ctx
        self.setWindowTitle(APP_DISPLAY_NAME)
        icon = theme.app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(1000, 640)
        self.resize(1280, 800)

        # --- pages ---
        self.preview_panel = PreviewPanel(ctx)
        self.queue_view = QueueView(ctx)
        self.library_view = LibraryView(ctx)
        self.history_view = HistoryView(ctx)
        self.listening_view = ListeningView(ctx.paths)
        self.player = PlayerController(self)
        self.player_bar = PlayerBar(self.player)
        self.queue_view.listen_requested.connect(self.listen)
        self.history_view.listen_requested.connect(self.listen)
        self.stack = QStackedWidget()
        for page in (self.preview_panel, self.queue_view, self.library_view, self.history_view, self.listening_view):
            self.stack.addWidget(page)

        # --- navigation ---
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setAccessibleName("Pages")
        self.nav.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        for i, name in enumerate(PAGE_NAMES):
            item = QListWidgetItem(name)
            item.setSizeHint(QSize(0, 40))
            item.setToolTip(f"{name} (Ctrl+{i + 1})")
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._on_page_selected)
        brand = QLabel("SoundCloud\nLibrary Manager")
        brand.setObjectName("brand")
        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.setAccessibleName("Open settings")
        self.settings_btn.setToolTip("Settings (Ctrl+,)")
        self.settings_btn.clicked.connect(self.open_settings)
        nav_frame = QFrame()
        nav_frame.setObjectName("nav")
        nav_frame.setFixedWidth(210)
        nav_layout = QVBoxLayout(nav_frame)
        nav_layout.setContentsMargins(0, 0, 0, 12)
        nav_layout.setSpacing(0)
        nav_layout.addWidget(brand)
        nav_layout.addWidget(self.nav, 1)
        side = QHBoxLayout()
        side.setContentsMargins(12, 0, 12, 0)
        side.addWidget(self.settings_btn)
        nav_layout.addLayout(side)

        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(nav_frame)
        row.addWidget(self.stack, 1)
        column.addLayout(row, 1)
        column.addWidget(self.player_bar)
        self.setCentralWidget(central)

        # --- status bar ---
        self.queue_label = QLabel("Queue: empty")
        self.queue_label.setAccessibleName("Queue summary")
        self.library_label = QLabel("")
        self.library_label.setAccessibleName("Library size")
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.addPermanentWidget(self.queue_label)
        bar.addPermanentWidget(self.library_label)

        self._build_menus()

        # --- wiring ---
        self.queue_view.model.summary_changed.connect(self._update_queue_summary)
        ctx.bridge.job_changed.connect(self._on_job_changed)
        for view in (self.preview_panel, self.queue_view, self.library_view, self.history_view):
            view.message.connect(self.show_message)
        self.preview_panel.enqueued.connect(self._on_enqueued)
        self.library_view.library_changed.connect(self.update_library_count)
        self.library_view.play_requested.connect(self.player.play_files)
        self.library_view.enqueue_requested.connect(self._enqueue)
        self.player.message.connect(self.show_message)
        # One audio source at a time: each player pauses the other (no session is closed).
        self.player.playing_changed.connect(
            lambda playing: self.listening_view.pause_web() if playing else None)
        self.listening_view.web_audio_changed.connect(
            lambda playing: self.player.pause() if playing else None)
        self.player_bar.install_shortcuts(self)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self._refresh_after_activity)

        self._restore_geometry()
        self._update_queue_summary()
        self.update_library_count()
        self.nav.setCurrentRow(PAGE_DOWNLOAD)

    # --- menus & shortcuts ----------------------------------------------------------------

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        go_menu = menubar.addMenu("&Go")
        focus_url = QAction("Paste &URL…", self)
        focus_url.setShortcut(QKeySequence("Ctrl+L"))
        focus_url.triggered.connect(self.focus_url)
        go_menu.addAction(focus_url)
        go_menu.addSeparator()
        for i, name in enumerate(PAGE_NAMES):
            action = QAction(name, self)
            action.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
            action.triggered.connect(lambda _checked=False, page=i: self.show_page(page))
            go_menu.addAction(action)

        help_menu = menubar.addMenu("&Help")
        self._update_ytdlp_action = QAction("Update &yt-dlp…", self)
        self._update_ytdlp_action.setStatusTip(
            "Download the latest yt-dlp from PyPI; used after the next restart")
        self._update_ytdlp_action.triggered.connect(
            lambda _checked=False: self._update_ytdlp())
        help_menu.addAction(self._update_ytdlp_action)
        help_menu.addSeparator()
        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _update_ytdlp(self, *, update_fn: Callable[[], object] | None = None) -> None:
        """Help → Update yt-dlp…: check PyPI and install in the background."""
        from sclm.gui.bridge import run_in_background

        action = self._update_ytdlp_action
        action.setEnabled(False)
        paths = self._ctx.paths

        def work() -> object:
            if update_fn is not None:
                return update_fn()
            from sclm.download.updater import update

            return update(paths)

        def done(result: object) -> None:
            action.setEnabled(True)
            updated = bool(getattr(result, "updated", False))
            version = getattr(result, "version", "?")
            if updated:
                QMessageBox.information(
                    self, "yt-dlp updated",
                    f"yt-dlp {version} was downloaded.\n"
                    f"Restart {APP_DISPLAY_NAME} to start using it.")
            else:
                QMessageBox.information(
                    self, "yt-dlp is up to date",
                    f"You already have the latest yt-dlp ({version}).")

        def failed(error: object) -> None:
            action.setEnabled(True)
            message = getattr(error, "user_message", None) or "Could not update yt-dlp."
            QMessageBox.warning(self, "yt-dlp update failed", str(message))

        run_in_background(work, done, failed, parent=self)

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_DISPLAY_NAME}",
            f"<b>{APP_DISPLAY_NAME}</b> {__version__}<br>Downloads tracks you are authorized "
            "to access, converts them to MP3, tags and organizes them.")

    # --- navigation -------------------------------------------------------------------------

    def show_page(self, page: int) -> None:
        self.nav.setCurrentRow(page)

    def listen(self, url: str) -> None:
        self.listening_view.open_url(url)
        self.show_page(PAGE_LISTEN)

    def _enqueue(self, files: list) -> None:
        added = self.player.enqueue(files)
        if added:
            self.show_message(f"Added {added} track(s) to the play queue")

    def current_page(self) -> int:
        return self.stack.currentIndex()

    def focus_url(self) -> None:
        self.show_page(PAGE_DOWNLOAD)
        self.preview_panel.focus_url()

    def _on_page_selected(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        self._refresh_page(row)
        if row == PAGE_LISTEN:
            self.listening_view.activate()

    def _refresh_page(self, row: int) -> None:
        if row == PAGE_LIBRARY:
            self.library_view.refresh()
        elif row == PAGE_HISTORY:
            self.history_view.refresh()

    def _on_enqueued(self, count: int) -> None:
        if count:
            self.show_page(PAGE_QUEUE)

    # --- status bar -----------------------------------------------------------------------

    def show_message(self, text: str) -> None:
        self.statusBar().showMessage(text, STATUS_MESSAGE_MS)

    def _update_queue_summary(self) -> None:
        s = self.queue_view.model.summary()
        parts = []
        if s["active"]:
            parts.append(f"{s['active']} active")
        if s["queued"]:
            parts.append(f"{s['queued']} queued")
        if s["done"]:
            parts.append(f"{s['done']} done")
        if s["skipped"]:
            parts.append(f"{s['skipped']} skipped")
        if s["failed"]:
            parts.append(f"{s['failed']} failed")
        self.queue_label.setText("Queue: " + (", ".join(parts) if parts else "empty"))
        self.queue_label.setToolTip("Downloads in the queue")
        self.queue_label.setContentsMargins(0, 0, 14, 0)

    def update_library_count(self) -> None:
        try:
            count = self._ctx.storage.count_library_files()
        except Exception as exc:
            log.warning("Could not count library files: %s", exc)
            return
        self.library_label.setText(f"Library: {count} file(s)")

    def _on_job_changed(self, job: object) -> None:
        status = getattr(job, "status", None)
        if status is not None and status.is_terminal:
            self._refresh_timer.start()

    def _refresh_after_activity(self) -> None:
        self.update_library_count()
        self._refresh_page(self.stack.currentIndex())

    # --- settings ---------------------------------------------------------------------------

    def open_settings(self) -> None:
        dialog = SettingsDialog(self._ctx, self)
        dialog.exec()
        dialog.deleteLater()

    # --- geometry & closing ------------------------------------------------------------------

    def _qsettings(self) -> QSettings:
        return QSettings()

    def _restore_geometry(self) -> None:
        try:
            geometry = self._qsettings().value("window/geometry")
            if geometry is not None:
                self.restoreGeometry(geometry)
        except Exception:
            log.debug("Could not restore window geometry", exc_info=True)

    def _is_idle(self) -> bool:
        try:
            return bool(self._ctx.manager.is_idle())
        except Exception:      # an unfinished download engine must not trap the user
            log.debug("manager.is_idle() failed", exc_info=True)
            return True

    def confirm_close(self) -> bool:
        """True if the window may close: idle, or the user agreed to cancel the downloads."""
        if self._is_idle():
            return True
        answer = QMessageBox.question(
            self, "Quit while downloading?",
            "Downloads are still in progress. Quitting will cancel them.\n\nQuit anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.confirm_close():
            event.ignore()
            return
        try:
            self._qsettings().setValue("window/geometry", self.saveGeometry())
        except Exception:
            log.debug("Could not save window geometry", exc_info=True)
        self.shutdown_players()
        event.accept()

    def shutdown_players(self) -> None:
        """Stop local audio and the website player process (idempotent)."""
        self.player.shutdown()
        self.listening_view.shutdown()
