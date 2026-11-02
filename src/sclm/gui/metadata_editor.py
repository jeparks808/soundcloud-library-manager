"""Metadata editor dialog: ``read_tags`` -> form -> explicit Save -> ``save_tags``.

Both calls touch files, so they run through ``run_in_background``. Nothing is written until
the user presses Save.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.errors import ErrorInfo
from sclm.core.models import LibraryFile, TagData
from sclm.gui.bridge import BackgroundTask, run_in_background
from sclm.gui.common import ErrorBanner, format_duration, set_primary, set_role

log = logging.getLogger(__name__)

MAX_COVER_BYTES = 10 * 1024 * 1024


def _read_cover(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_COVER_BYTES:
        raise ValueError(f"That image is too large ({size // (1024 * 1024)} MB; the limit is "
                         f"{MAX_COVER_BYTES // (1024 * 1024)} MB).")
    return path.read_bytes()


class MetadataEditorDialog(QDialog):
    """Edit the ID3 tags of one library file. Emits ``saved(LibraryFile)`` after a good save."""

    saved = Signal(object)

    def __init__(self, ctx: AppContext, path: Path, parent: QWidget | None = None, *,
                 read_fn: Callable[[Path], TagData] | None = None,
                 save_fn: Callable[..., LibraryFile] | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._path = Path(path)
        self._read_fn = read_fn
        self._save_fn = save_fn
        self._tags: TagData | None = None
        self._new_cover: bytes | None = None
        self._remove_cover = False
        self._task: BackgroundTask | None = None
        self._cover_task: BackgroundTask | None = None

        self.setWindowTitle(f"Edit tags: {self._path.name}")
        self.setModal(True)
        self.resize(640, 560)
        self.setAccessibleName("Metadata editor")

        self.title_edit = QLineEdit()
        self.artist_edit = QLineEdit()
        self.album_edit = QLineEdit()
        self.album_artist_edit = QLineEdit()
        self.genre_edit = QLineEdit()
        self.year_spin = QSpinBox()
        self.year_spin.setRange(0, 9999)
        self.year_spin.setSpecialValueText("—")
        self.track_spin = QSpinBox()
        self.track_spin.setRange(0, 9999)
        self.track_spin.setSpecialValueText("—")
        self.comment_edit = QPlainTextEdit()
        self.comment_edit.setFixedHeight(70)
        for widget, name in ((self.title_edit, "Title"), (self.artist_edit, "Artist"),
                             (self.album_edit, "Album"),
                             (self.album_artist_edit, "Album artist"),
                             (self.genre_edit, "Genre"), (self.year_spin, "Year"),
                             (self.track_spin, "Track number"),
                             (self.comment_edit, "Comment")):
            widget.setAccessibleName(name)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("&Title", self.title_edit)
        form.addRow("&Artist", self.artist_edit)
        form.addRow("A&lbum", self.album_edit)
        form.addRow("Album a&rtist", self.album_artist_edit)
        form.addRow("&Genre", self.genre_edit)
        form.addRow("&Year", self.year_spin)
        form.addRow("Track &#", self.track_spin)
        form.addRow("&Comment", self.comment_edit)

        self.cover_label = QLabel("No cover")
        self.cover_label.setFixedSize(120, 120)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setObjectName("card")
        self.cover_label.setAccessibleName("Cover image")
        self.cover_status = set_role(QLabel(""), "muted")
        self.cover_status.setWordWrap(True)
        self.choose_cover_btn = QPushButton("Choose image…")
        self.choose_cover_btn.setAccessibleName("Choose a cover image")
        self.remove_cover_btn = QPushButton("Remove cover")
        self.remove_cover_btn.setAccessibleName("Remove the cover image")
        self.choose_cover_btn.clicked.connect(self._choose_cover)
        self.remove_cover_btn.clicked.connect(self._mark_remove_cover)
        cover_buttons = QVBoxLayout()
        cover_buttons.addWidget(self.cover_status)
        cover_buttons.addWidget(self.choose_cover_btn)
        cover_buttons.addWidget(self.remove_cover_btn)
        cover_buttons.addStretch(1)
        cover_row = QHBoxLayout()
        cover_row.addWidget(self.cover_label)
        cover_row.addLayout(cover_buttons, 1)
        form.addRow("Cover", cover_row)

        self.ro_track_id = QLabel("")
        self.ro_url = QLabel("")
        self.ro_duration = QLabel("")
        self.ro_path = QLabel(str(self._path))
        for lab in (self.ro_track_id, self.ro_url, self.ro_duration, self.ro_path):
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            set_role(lab, "muted")
        form.addRow("SoundCloud ID", self.ro_track_id)
        form.addRow("Source URL", self.ro_url)
        form.addRow("Duration", self.ro_duration)
        form.addRow("File", self.ro_path)

        self.error_banner = ErrorBanner()
        self.status_label = set_role(QLabel("Loading tags…"), "muted")
        self.save_btn = QPushButton("Save")
        self.save_btn.setAccessibleName("Save tags")
        self.save_btn.setDefault(True)
        set_primary(self.save_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAccessibleName("Close without saving")
        self.save_btn.clicked.connect(self.save)
        self.cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.status_label, 1)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(self.error_banner)
        layout.addLayout(buttons)

        self._set_editable(False)
        self._load()

    # --- loading ---------------------------------------------------------------------

    def _set_editable(self, enabled: bool) -> None:
        for w in (self.title_edit, self.artist_edit, self.album_edit, self.album_artist_edit,
                  self.genre_edit, self.year_spin, self.track_spin, self.comment_edit,
                  self.choose_cover_btn, self.remove_cover_btn, self.save_btn):
            w.setEnabled(enabled)

    def _load(self) -> None:
        read_fn = self._read_fn
        if read_fn is None:
            from sclm.metadata import editor as _editor   # looked up late: test seam

            def read_fn(p: Path) -> TagData:  # type: ignore[misc]
                return _editor.read_tags(p)

        path = self._path
        self._task = run_in_background(lambda: read_fn(path), self._on_loaded, self._on_load_error,
                                       parent=self)

    def _on_loaded(self, tags: TagData) -> None:
        self._task = None
        self._tags = tags
        self.title_edit.setText(tags.title)
        self.artist_edit.setText(tags.artist)
        self.album_edit.setText(tags.album)
        self.album_artist_edit.setText(tags.album_artist)
        self.genre_edit.setText(tags.genre)
        self.year_spin.setValue(tags.year or 0)
        self.track_spin.setValue(tags.track_number or 0)
        self.comment_edit.setPlainText(tags.comment)
        self.ro_track_id.setText(tags.track_id or "—")
        self.ro_url.setText(tags.source_url or "—")
        self.ro_duration.setText(format_duration(tags.duration_s) or "—")
        self.cover_status.setText("Embedded cover present" if tags.has_artwork
                                  else "No embedded cover")
        self.remove_cover_btn.setVisible(tags.has_artwork)
        self.status_label.setText("")
        self._set_editable(True)
        self.remove_cover_btn.setEnabled(tags.has_artwork)
        self.title_edit.setFocus()

    def _on_load_error(self, error: ErrorInfo) -> None:
        self._task = None
        self.status_label.setText("")
        self.error_banner.show_error(error)

    # --- cover ------------------------------------------------------------------------

    def _choose_cover(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", str(self._path.parent),
            "Images (*.jpg *.jpeg *.png)")
        if name:
            self.set_cover_from_file(Path(name))

    def set_cover_from_file(self, image_path: Path) -> None:
        """Read the image off the GUI thread, then use it as the new cover."""
        self.status_label.setText("Reading image…")
        self._cover_task = run_in_background(
            lambda: _read_cover(image_path), self._on_cover_bytes, self._on_cover_error,
            parent=self)

    def _on_cover_bytes(self, data: bytes) -> None:
        self._cover_task = None
        self.status_label.setText("")
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.error_banner.show_error("That file isn't a readable JPEG or PNG image.")
            return
        self.error_banner.clear()
        self._new_cover = data
        self._remove_cover = False
        self.cover_label.setPixmap(pixmap.scaled(
            120, 120, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.cover_status.setText("New cover will be saved")

    def _on_cover_error(self, error: ErrorInfo) -> None:
        self._cover_task = None
        self.status_label.setText("")
        self.error_banner.show_error(error)

    def _mark_remove_cover(self) -> None:
        self._new_cover = None
        self._remove_cover = True
        self.cover_label.clear()
        self.cover_label.setText("No cover")
        self.cover_status.setText("Cover will be removed")

    # --- saving -----------------------------------------------------------------------

    def tags_from_form(self) -> TagData:
        """The edited tags (read-only fields are carried over unchanged)."""
        base = self._tags or TagData()
        return TagData(
            title=self.title_edit.text().strip(),
            artist=self.artist_edit.text().strip(),
            album=self.album_edit.text().strip(),
            album_artist=self.album_artist_edit.text().strip(),
            genre=self.genre_edit.text().strip(),
            year=self.year_spin.value() or None,
            track_number=self.track_spin.value() or None,
            comment=self.comment_edit.toPlainText().strip(),
            track_id=base.track_id, source_url=base.source_url,
            duration_s=base.duration_s, has_artwork=base.has_artwork)

    def save(self) -> None:
        """Write the tags (background). Only ever called from the Save button."""
        if self._tags is None or self._task is not None:
            return
        save_fn = self._save_fn
        if save_fn is None:
            from sclm.metadata import editor as _editor   # looked up late: test seam

            def save_fn(*a, **kw):  # type: ignore[misc]
                return _editor.save_tags(*a, **kw)

        tags = self.tags_from_form()
        artwork, remove = self._new_cover, self._remove_cover
        path, storage = self._path, self._ctx.storage
        self.error_banner.clear()
        self._set_editable(False)
        self.status_label.setText("Saving…")

        def work() -> LibraryFile:
            return save_fn(path, tags, storage, artwork=artwork, remove_artwork=remove)

        self._task = run_in_background(work, self._on_saved, self._on_save_error, parent=self)

    def _on_saved(self, library_file: LibraryFile) -> None:
        self._task = None
        self.status_label.setText("Saved")
        self.saved.emit(library_file)
        self.accept()

    def _on_save_error(self, error: ErrorInfo) -> None:
        self._task = None
        self.status_label.setText("")
        self._set_editable(True)
        self.remove_cover_btn.setEnabled(bool(self._tags and self._tags.has_artwork)
                                         or self._new_cover is not None)
        self.error_banner.show_error(error)
