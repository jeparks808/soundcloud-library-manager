"""Settings dialog: edits ``Settings``, validates with ``Settings.replace`` and saves through
``ctx.settings.update``. Validation errors (``ConfigError.user_message``) show inline and the
Save button stays disabled until the form is valid again."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.errors import ConfigError, ErrorInfo
from sclm.core.models import ConflictPolicy, OrganizeMode, TrackInfo
from sclm.core.settings import (
    BITRATES_KBPS,
    CONCURRENCY_MAX,
    CONCURRENCY_MIN,
    Settings,
)
from sclm.gui.bridge import run_in_background
from sclm.gui.common import ErrorBanner, set_primary, set_role

log = logging.getLogger(__name__)

TOOL_REFRESH_DEBOUNCE_MS = 500

SAMPLE_TRACK = TrackInfo(
    track_id="123456789", url="https://soundcloud.com/m83/midnight-city",
    title="Midnight City", artist="M83", genre="Electronic", album="Hurry Up, We're Dreaming",
    upload_date="2011-10-18", playlist_index=3)
SAMPLE_PLAYLIST = "Road Trip"

ORGANIZE_LABELS = {
    OrganizeMode.PLAYLIST: "By playlist",
    OrganizeMode.ARTIST: "By artist",
    OrganizeMode.ALBUM: "By artist / album",
    OrganizeMode.GENRE: "By genre",
}
CONFLICT_LABELS = {
    ConflictPolicy.SKIP: "Skip duplicates",
    ConflictPolicy.KEEP_BOTH: "Keep both (add a number)",
    ConflictPolicy.ASK: "Ask (same as skip for now)",
}


def probe_tools(configured: str | None) -> tuple[str, str]:
    """Blocking: resolve ffmpeg and read its version, plus the yt-dlp version.
    Returns ``(ffmpeg_text, ytdlp_text)``. Runs in the background."""
    from sclm.core.errors import SclmError
    from sclm.core.ffmpeg import ffmpeg_version, resolve_ffmpeg

    try:
        info = resolve_ffmpeg(configured)
        version = ffmpeg_version(info.path) or "version unknown"
        ffmpeg_text = f"{version} ({info.source.value})\n{info.path}"
    except SclmError as exc:
        ffmpeg_text = exc.user_message
    try:
        from yt_dlp.version import __version__ as ytdlp_version
        ytdlp_text = ytdlp_version
    except Exception:
        ytdlp_text = "not installed"
    return ffmpeg_text, ytdlp_text


class SettingsDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *,
                 render_fn: Callable[..., str] | None = None,
                 probe_fn: Callable[[str | None], tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._render_fn = render_fn
        self._probe_fn = probe_fn or probe_tools
        self._probe_seq = 0
        self._current = ctx.settings.get()
        self._valid = True

        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setAccessibleName("Settings")

        s = self._current

        # --- library ---
        self.library_edit = QLineEdit(s.library_dir)
        self.library_edit.setAccessibleName("Library folder")
        browse_lib = QPushButton("Browse…")
        browse_lib.setAccessibleName("Browse for the library folder")
        browse_lib.clicked.connect(lambda: self._browse_dir(self.library_edit))
        self.organize_combo = QComboBox()
        self.organize_combo.setAccessibleName("Library organization")
        for mode, label in ORGANIZE_LABELS.items():
            self.organize_combo.addItem(label, mode)
        self._select(self.organize_combo, s.organize_mode)
        self.template_edit = QLineEdit(s.filename_template)
        self.template_edit.setAccessibleName("Filename template")
        self.template_edit.setToolTip(
            "Fields: {artist} {title} {playlist} {index} {track_id} {genre} {year} {album}")
        self.template_preview = set_role(QLabel(""), "muted")
        self.template_preview.setAccessibleName("Filename preview")
        self.template_preview.setWordWrap(True)

        lib_box = QGroupBox("Library")
        lib_form = QFormLayout(lib_box)
        lib_form.addRow("Folder", self._with_button(self.library_edit, browse_lib))
        lib_form.addRow("Organize", self.organize_combo)
        lib_form.addRow("File name", self.template_edit)
        lib_form.addRow("Preview", self.template_preview)

        # --- conversion ---
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.setAccessibleName("MP3 bitrate")
        for b in BITRATES_KBPS:
            self.bitrate_combo.addItem(f"{b} kbps", b)
        self._select(self.bitrate_combo, s.bitrate_kbps)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(CONCURRENCY_MIN, CONCURRENCY_MAX)
        self.concurrency_spin.setValue(s.concurrency)
        self.concurrency_spin.setAccessibleName("Concurrent downloads")
        self.artwork_check = QCheckBox("Embed cover artwork")
        self.artwork_check.setChecked(s.embed_artwork)
        conv_box = QGroupBox("Conversion")
        conv_form = QFormLayout(conv_box)
        conv_form.addRow("Bitrate", self.bitrate_combo)
        conv_form.addRow("Parallel downloads", self.concurrency_spin)
        conv_form.addRow("", self.artwork_check)

        # --- behaviour ---
        self.conflict_combo = QComboBox()
        self.conflict_combo.setAccessibleName("Duplicate handling")
        for pol, label in CONFLICT_LABELS.items():
            self.conflict_combo.addItem(label, pol)
        self._select(self.conflict_combo, s.conflict_policy)
        self.only_new_check = QCheckBox("Pre-select only new tracks in a preview")
        self.only_new_check.setChecked(s.only_new_tracks)
        self.beets_check = QCheckBox("Import into Beets after tagging (if installed)")
        self.beets_check.setChecked(s.beets_enabled)
        beh_box = QGroupBox("Behavior")
        beh_form = QFormLayout(beh_box)
        beh_form.addRow("Duplicates", self.conflict_combo)
        beh_form.addRow("", self.only_new_check)
        beh_form.addRow("", self.beets_check)

        # --- tools ---
        self.ffmpeg_edit = QLineEdit(s.ffmpeg_path)
        self.ffmpeg_edit.setPlaceholderText("Automatic (PATH, then bundled)")
        self.ffmpeg_edit.setAccessibleName("FFmpeg path")
        browse_ff = QPushButton("Browse…")
        browse_ff.setAccessibleName("Browse for FFmpeg")
        browse_ff.clicked.connect(lambda: self._browse_file(self.ffmpeg_edit, "Choose FFmpeg"))
        self.ffmpeg_info = set_role(QLabel("Checking…"), "muted")
        self.ffmpeg_info.setAccessibleName("Resolved FFmpeg")
        self.ffmpeg_info.setWordWrap(True)
        self.ffmpeg_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cookies_edit = QLineEdit(s.cookies_file)
        self.cookies_edit.setPlaceholderText("Optional cookies.txt for your own content")
        self.cookies_edit.setAccessibleName("Cookies file")
        browse_ck = QPushButton("Browse…")
        browse_ck.setAccessibleName("Browse for the cookies file")
        browse_ck.clicked.connect(lambda: self._browse_file(self.cookies_edit,
                                                            "Choose cookies.txt"))
        self.ytdlp_info = set_role(QLabel("Checking…"), "muted")
        self.ytdlp_info.setAccessibleName("yt-dlp version")
        tools_box = QGroupBox("Tools and access")
        tools_form = QFormLayout(tools_box)
        tools_form.addRow("FFmpeg", self._with_button(self.ffmpeg_edit, browse_ff))
        tools_form.addRow("", self.ffmpeg_info)
        tools_form.addRow("Cookies", self._with_button(self.cookies_edit, browse_ck))
        tools_form.addRow("yt-dlp", self.ytdlp_info)

        # --- footer ---
        self.error_banner = ErrorBanner()
        self.save_btn = QPushButton("Save")
        self.save_btn.setAccessibleName("Save settings")
        self.save_btn.setDefault(True)
        set_primary(self.save_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAccessibleName("Close without saving")
        self.save_btn.clicked.connect(self.try_save)
        self.cancel_btn.clicked.connect(self.reject)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.save_btn)

        left = QVBoxLayout()
        left.addWidget(lib_box)
        left.addWidget(conv_box)
        left.addStretch(1)
        right = QVBoxLayout()
        right.addWidget(beh_box)
        right.addWidget(tools_box)
        right.addStretch(1)
        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(columns)
        layout.addWidget(self.error_banner)
        layout.addLayout(footer)
        self.resize(980, 560)

        # live validation + preview
        for edit in (self.library_edit, self.template_edit, self.ffmpeg_edit, self.cookies_edit):
            edit.textChanged.connect(self._revalidate)
        for combo in (self.organize_combo, self.bitrate_combo, self.conflict_combo):
            combo.currentIndexChanged.connect(self._revalidate)
        self.concurrency_spin.valueChanged.connect(self._revalidate)
        for check in (self.artwork_check, self.only_new_check, self.beets_check):
            check.toggled.connect(self._revalidate)

        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(TOOL_REFRESH_DEBOUNCE_MS)
        self._probe_timer.timeout.connect(self.refresh_tools)
        self.ffmpeg_edit.textChanged.connect(lambda _t: self._probe_timer.start())

        self._revalidate()
        self.refresh_tools()

    # --- small helpers -----------------------------------------------------------------

    @staticmethod
    def _with_button(edit: QLineEdit, button: QPushButton) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(button)
        return box

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _browse_dir(self, edit: QLineEdit) -> None:
        start = str(Path(edit.text()).expanduser()) if edit.text().strip() else str(Path.home())
        name = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if name:
            edit.setText(name)

    def _browse_file(self, edit: QLineEdit, title: str) -> None:
        name, _ = QFileDialog.getOpenFileName(self, title, edit.text() or str(Path.home()))
        if name:
            edit.setText(name)

    # --- form <-> Settings ---------------------------------------------------------------

    def build_settings(self) -> Settings:
        """Validated ``Settings`` from the form; raises ``ConfigError``."""
        return self._current.replace(
            library_dir=self.library_edit.text().strip(),
            organize_mode=self.organize_combo.currentData(),
            filename_template=self.template_edit.text(),
            bitrate_kbps=self.bitrate_combo.currentData(),
            concurrency=self.concurrency_spin.value(),
            embed_artwork=self.artwork_check.isChecked(),
            conflict_policy=self.conflict_combo.currentData(),
            only_new_tracks=self.only_new_check.isChecked(),
            beets_enabled=self.beets_check.isChecked(),
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            cookies_file=self.cookies_edit.text().strip(),
        )

    @property
    def is_valid(self) -> bool:
        return self._valid

    def _revalidate(self, *_args: object) -> None:
        try:
            new = self.build_settings()
        except ConfigError as exc:
            self._valid = False
            self.error_banner.show_error(exc.to_info())
            self.save_btn.setEnabled(False)
            self.template_edit.setProperty("invalid", "true")
            self._repolish(self.template_edit)
            self.template_preview.setText("")
            return
        self._valid = True
        self.error_banner.clear()
        self.save_btn.setEnabled(True)
        self.template_edit.setProperty("invalid", "false")
        self._repolish(self.template_edit)
        self._update_preview(new)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _update_preview(self, settings: Settings) -> None:
        render = self._render_fn
        if render is None:
            from sclm.metadata import naming as _naming   # looked up late: test seam

            def render(*a, **kw):  # type: ignore[misc]
                return _naming.render_filename(*a, **kw)

        try:
            name = render(settings.filename_template, SAMPLE_TRACK,
                          playlist_title=SAMPLE_PLAYLIST)
        except NotImplementedError:
            self.template_preview.setText("Preview not available in this build yet")
            return
        except Exception as exc:  # preview must never break the dialog
            log.debug("filename preview failed: %s", exc)
            self.template_preview.setText("Preview not available")
            return
        self.template_preview.setText(f"{name}.mp3")

    # --- tools (ffmpeg / yt-dlp), probed off the GUI thread ---------------------------------

    def refresh_tools(self) -> None:
        self._probe_seq += 1
        seq = self._probe_seq
        configured = self.ffmpeg_edit.text().strip() or None
        probe = self._probe_fn
        self.ffmpeg_info.setText("Checking…")
        run_in_background(lambda: probe(configured),
                          lambda result: self._on_probe(seq, result),
                          lambda err: self._on_probe_error(seq, err), parent=self)

    def _on_probe(self, seq: int, result: tuple[str, str]) -> None:
        if seq != self._probe_seq:
            return                      # a newer probe is in flight
        self.ffmpeg_info.setText(result[0])
        self.ytdlp_info.setText(result[1])

    def _on_probe_error(self, seq: int, error: ErrorInfo) -> None:
        if seq == self._probe_seq:
            self.ffmpeg_info.setText(error.user_message)
            self.ytdlp_info.setText("unknown")

    # --- saving -----------------------------------------------------------------------

    def try_save(self) -> bool:
        """Validate and persist. Returns True (and closes) on success."""
        try:
            new = self.build_settings()
        except ConfigError as exc:
            self._valid = False
            self.error_banner.show_error(exc.to_info())
            self.save_btn.setEnabled(False)
            return False
        try:
            self._ctx.settings.update(new)
        except Exception as exc:
            log.exception("Could not save settings")
            self.error_banner.show_error(f"Could not save the settings: {exc}")
            return False
        self._current = new
        self.accept()
        return True
