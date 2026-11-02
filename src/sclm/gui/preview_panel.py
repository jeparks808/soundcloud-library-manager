"""Download page: URL field, Fetch, preview table with checkboxes, "Download selected"."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from html import escape

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.errors import ErrorInfo
from sclm.core.models import DownloadRequest, PlaylistPreview, PreviewEntry
from sclm.gui import theme
from sclm.gui.bridge import BackgroundTask, run_in_background
from sclm.gui.common import ErrorBanner, format_duration, set_primary, set_role

log = logging.getLogger(__name__)

COL_CHECK, COL_INDEX, COL_TITLE, COL_ARTIST, COL_DURATION, COL_STATE = range(6)
_HEADERS = ("", "#", "Title", "Artist", "Time", "Status")


class PreviewModel(QAbstractTableModel):
    """Checkable rows for a ``PlaylistPreview``. Selection state lives here."""

    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[PreviewEntry] = []
        self._checked: list[bool] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COL_CHECK:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex | QPersistentModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        entry = self._entries[row]
        track = entry.track
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_CHECK:
            return Qt.CheckState.Checked if self._checked[row] else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_INDEX:
                return track.playlist_index if track.playlist_index is not None else row + 1
            if col == COL_TITLE:
                return track.title or f"Track {track.track_id}"
            if col == COL_ARTIST:
                return track.artist
            if col == COL_DURATION:
                return format_duration(track.duration_s)
            if col == COL_STATE:
                return "New" if entry.is_new else "In library"
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_STATE and entry.is_new:
                return QColor(theme.SUCCESS)
            if not entry.is_new:
                return QColor(theme.TEXT_MUTED)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_INDEX, COL_DURATION):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if entry.existing_path is not None:
                return f"Already in library: {entry.existing_path}"
            return track.url
        return None

    def setData(self, index: QModelIndex | QPersistentModelIndex, value: object,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if index.isValid() and index.column() == COL_CHECK \
                and role == Qt.ItemDataRole.CheckStateRole:
            checked = Qt.CheckState(value) == Qt.CheckState.Checked \
                if not isinstance(value, bool) else value
            if self._checked[index.row()] != checked:
                self._checked[index.row()] = checked
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
                self.selection_changed.emit()
            return True
        return False

    # --- API ------------------------------------------------------------------------

    def set_preview(self, entries: tuple[PreviewEntry, ...], only_new: bool) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self._checked = [e.is_new if only_new else True for e in self._entries]
        self.endResetModel()
        self.selection_changed.emit()

    def clear(self) -> None:
        self.set_preview((), True)

    def apply_only_new(self, only_new: bool) -> None:
        """Re-select: only new entries (``only_new``) or every entry."""
        self.set_all([e.is_new if only_new else True for e in self._entries])

    def set_all(self, flags: list[bool] | bool) -> None:
        if isinstance(flags, bool):
            flags = [flags] * len(self._entries)
        self._checked = list(flags)
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_CHECK),
                                  self.index(len(self._entries) - 1, COL_CHECK),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()

    def entries(self) -> list[PreviewEntry]:
        return list(self._entries)

    def checked_entries(self) -> list[PreviewEntry]:
        return [e for e, c in zip(self._entries, self._checked, strict=True) if c]

    def checked_count(self) -> int:
        return sum(self._checked)


class PreviewPanel(QWidget):
    """Paste a URL, fetch a preview (in the background), choose tracks, queue them."""

    enqueued = Signal(int)   # number of jobs actually queued
    message = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *,
                 fetch_fn: Callable[..., PlaylistPreview] | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._fetch_fn = fetch_fn
        self._cancel: threading.Event | None = None
        self._task: BackgroundTask | None = None
        self._preview: PlaylistPreview | None = None
        self.model = PreviewModel(self)

        title = set_role(QLabel("Download"), "title")
        hint = set_role(QLabel("Paste a SoundCloud track, playlist, set or profile URL."),
                        "muted")

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://soundcloud.com/…")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setAccessibleName("SoundCloud URL")
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setAccessibleName("Fetch the track list")
        self.fetch_btn.setToolTip("Fetch the track list (Enter)")
        set_primary(self.fetch_btn)
        self.cancel_fetch_btn = QPushButton("Stop")
        self.cancel_fetch_btn.setAccessibleName("Stop fetching")
        self.cancel_fetch_btn.hide()
        self.url_edit.returnPressed.connect(self.fetch)
        self.fetch_btn.clicked.connect(self.fetch)
        self.cancel_fetch_btn.clicked.connect(self.cancel_fetch)
        self.url_edit.textChanged.connect(self._update_buttons)

        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.fetch_btn)
        url_row.addWidget(self.cancel_fetch_btn)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setFixedHeight(4)
        self.busy.hide()

        self.error_banner = ErrorBanner()

        self.summary_label = QLabel("")
        self.summary_label.setAccessibleName("Playlist summary")
        self.summary_label.setWordWrap(True)
        self.sync_label = set_role(QLabel(""), "muted")
        self.sync_label.setAccessibleName("Playlist changes since the last sync")

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Tracks found")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_INDEX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_DURATION, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_CHECK, 36)
        self.table.setColumnWidth(COL_INDEX, 48)
        self.table.setColumnWidth(COL_ARTIST, 240)
        self.table.setColumnWidth(COL_DURATION, 70)
        self.table.setColumnWidth(COL_STATE, 90)

        self.only_new_check = QCheckBox("Only new tracks")
        self.only_new_check.setAccessibleName("Only select new tracks")
        self.only_new_check.setToolTip("Select only tracks that are not in your library yet")
        self.only_new_check.setChecked(self._only_new_default())
        self.only_new_check.toggled.connect(self.model.apply_only_new)
        self.select_all_btn = QPushButton("Select all")
        self.select_none_btn = QPushButton("Select none")
        self.select_all_btn.setAccessibleName("Select all tracks")
        self.select_none_btn.setAccessibleName("Select no tracks")
        self.select_all_btn.clicked.connect(lambda: self._select_all(True))
        self.select_none_btn.clicked.connect(lambda: self._select_all(False))
        self.download_btn = QPushButton("Download selected")
        self.download_btn.setAccessibleName("Download selected tracks")
        self.download_btn.setToolTip("Add the checked tracks to the queue (Ctrl+Enter)")
        self.download_btn.setShortcut("Ctrl+Return")
        set_primary(self.download_btn)
        self.download_btn.clicked.connect(self.download_selected)
        self.model.selection_changed.connect(self._update_buttons)

        bottom = QHBoxLayout()
        bottom.addWidget(self.only_new_check)
        bottom.addSpacing(12)
        bottom.addWidget(self.select_all_btn)
        bottom.addWidget(self.select_none_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.download_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(4)
        layout.addLayout(url_row)
        layout.addWidget(self.busy)
        layout.addWidget(self.error_banner)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.sync_label)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        self._update_buttons()

    # --- helpers --------------------------------------------------------------------

    def _only_new_default(self) -> bool:
        try:
            return self._ctx.settings.get().only_new_tracks
        except Exception:
            return True

    def focus_url(self) -> None:
        self.url_edit.setFocus()
        self.url_edit.selectAll()

    @property
    def fetching(self) -> bool:
        return self._task is not None

    @property
    def preview(self) -> PlaylistPreview | None:
        return self._preview

    # --- fetching -------------------------------------------------------------------

    def fetch(self) -> None:
        """Start a background preview fetch for the URL in the field."""
        url = self.url_edit.text().strip()
        if not url or self._task is not None:
            return
        self.error_banner.clear()
        self.sync_label.setText("")
        self.summary_label.setText("Fetching…")
        self._cancel = threading.Event()
        cancel = self._cancel
        settings = self._ctx.settings.get()
        storage = self._ctx.storage
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            from sclm.download import preview as _preview   # looked up late: test seam

            def fetch_fn(*a, **kw):  # type: ignore[no-redef]
                return _preview.fetch_preview(*a, **kw)

        def work() -> PlaylistPreview:
            return fetch_fn(url, settings, storage, cancel=cancel)

        self._task = run_in_background(work, self._on_preview, self._on_fetch_error, parent=self)
        self._set_busy(True)

    def cancel_fetch(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        self.summary_label.setText("Stopping…")

    def _set_busy(self, busy: bool) -> None:
        self.busy.setVisible(busy)
        self.cancel_fetch_btn.setVisible(busy)
        self.url_edit.setEnabled(not busy)
        self._update_buttons()

    def _finish_fetch(self) -> None:
        self._task = None
        self._cancel = None
        self._set_busy(False)

    def _on_fetch_error(self, error: ErrorInfo) -> None:
        self._finish_fetch()
        self.summary_label.setText("")
        if error.category.value == "cancelled":
            self.summary_label.setText("Fetch cancelled.")
            return
        self.error_banner.show_error(error)

    def _on_preview(self, preview: PlaylistPreview) -> None:
        self._finish_fetch()
        self.show_preview(preview)

    def show_preview(self, preview: PlaylistPreview) -> None:
        """Populate the table from ``preview`` (also used directly by tests)."""
        self._preview = preview
        self.error_banner.clear()
        self.model.set_preview(preview.entries, self.only_new_check.isChecked())
        pl = preview.playlist
        total = len(preview.entries)
        new = len(preview.new_entries)
        if pl.is_single_track:
            self.summary_label.setText(f"<b>{_esc(preview.entries[0].track.display_name)}</b>"
                                       if total else "No tracks found.")
        else:
            by = f" by {_esc(pl.uploader)}" if pl.uploader else ""
            self.summary_label.setText(
                f"<b>{_esc(pl.title)}</b>{by} · {total} track(s), {new} new")
        if not pl.is_single_track and (preview.sync.added or preview.sync.removed):
            self.sync_label.setText(
                f"Since the last sync: {len(preview.sync.added)} added, "
                f"{len(preview.sync.removed)} removed")
        else:
            self.sync_label.setText("")
        if total == 0:
            self.summary_label.setText("No tracks found at that URL.")
        self._update_buttons()

    # --- selection / queueing --------------------------------------------------------

    def _select_all(self, value: bool) -> None:
        self.model.set_all(value)

    def selected_requests(self) -> list[DownloadRequest]:
        """One ``DownloadRequest`` per checked row, carrying the playlist context."""
        if self._preview is None:
            return []
        pl = self._preview.playlist
        playlist_id = pl.playlist_id
        playlist_title = None if pl.is_single_track else (pl.title or None)
        return [DownloadRequest(e.track, playlist_id=playlist_id, playlist_title=playlist_title)
                for e in self.model.checked_entries()]

    def download_selected(self) -> None:
        requests = self.selected_requests()
        if not requests:
            return
        try:
            jobs = self._ctx.manager.enqueue(requests)
        except NotImplementedError:
            self.error_banner.show_error(
                "Downloading isn't available in this build yet (the download engine is "
                "not implemented).")
            return
        except Exception as exc:
            log.exception("enqueue failed")
            self.error_banner.show_error(f"Could not queue the tracks: {exc}")
            return
        self.error_banner.clear()
        n = len(jobs)
        skipped = len(requests) - n
        text = f"Queued {n} track(s)"
        if skipped:
            text += f" ({skipped} already in the queue)"
        self.message.emit(text)
        self.enqueued.emit(n)

    def _update_buttons(self, *_args: object) -> None:
        busy = self._task is not None
        has_rows = self.model.rowCount() > 0
        count = self.model.checked_count()
        self.fetch_btn.setEnabled(not busy and bool(self.url_edit.text().strip()))
        self.download_btn.setEnabled(not busy and count > 0)
        self.download_btn.setText(f"Download selected ({count})" if count else "Download selected")
        self.select_all_btn.setEnabled(has_rows)
        self.select_none_btn.setEnabled(has_rows)
        self.only_new_check.setEnabled(has_rows)


def _esc(text: str | None) -> str:
    return escape(text or "")
