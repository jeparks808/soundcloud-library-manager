"""History page: paged ``storage.download_history`` with a status filter and error details."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.models import DownloadRecord, JobStatus
from sclm.gui import theme
from sclm.gui.common import STATUS_LABELS, open_containing_folder, set_role

log = logging.getLogger(__name__)

PAGE_SIZE = 200
COL_WHEN, COL_TRACK, COL_STATUS, COL_ATTEMPTS, COL_NOTE = range(5)
_HEADERS = ("When", "Track", "Status", "Tries", "Result")

FILTERS: list[tuple[str, tuple[JobStatus, ...] | None]] = [
    ("All", None),
    ("Completed", (JobStatus.COMPLETED,)),
    ("Failed", (JobStatus.FAILED,)),
    ("Duplicates skipped", (JobStatus.SKIPPED_DUPLICATE,)),
    ("Cancelled", (JobStatus.CANCELLED,)),
    ("Unfinished", (JobStatus.QUEUED, JobStatus.RESOLVING, JobStatus.DOWNLOADING,
                    JobStatus.CONVERTING, JobStatus.TAGGING, JobStatus.ORGANIZING,
                    JobStatus.WAITING_RETRY)),
]


def _when(record: DownloadRecord) -> str:
    # ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SS+00:00' -> 'YYYY-MM-DD HH:MM' (kept in UTC)
    return (record.updated_at or record.created_at or "")[:16].replace("T", " ")


def _note(record: DownloadRecord) -> str:
    if record.error is not None:
        return record.error.user_message
    if record.file_path is not None:
        return record.file_path.name
    return ""


class HistoryModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[DownloadRecord] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        rec = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_WHEN:
                return _when(rec)
            if col == COL_TRACK:
                if rec.artist and rec.title:
                    return f"{rec.artist} - {rec.title}"
                return rec.title or f"Track {rec.track_id}"
            if col == COL_STATUS:
                return STATUS_LABELS[rec.status]
            if col == COL_ATTEMPTS:
                return str(rec.attempts)
            return _note(rec)
        if role == Qt.ItemDataRole.ForegroundRole and col == COL_STATUS:
            return QColor(theme.STATUS_COLORS[rec.status])
        if role == Qt.ItemDataRole.TextAlignmentRole and col == COL_ATTEMPTS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == COL_NOTE:
            return _note(rec)
        return None

    def set_records(self, rows: list[DownloadRecord]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def record_at(self, row: int) -> DownloadRecord | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None


class HistoryView(QWidget):
    message = Signal(str)
    listen_requested = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._offset = 0
        self._has_more = False
        self.model = HistoryModel(self)

        title = set_role(QLabel("History"), "title")
        self.filter_combo = QComboBox()
        self.filter_combo.setAccessibleName("Filter history by status")
        for label, _statuses in FILTERS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setAccessibleName("Refresh history")
        self.refresh_btn.setShortcut("F5")
        self.refresh_btn.clicked.connect(self.refresh)
        self.prev_btn = QPushButton("Newer")
        self.next_btn = QPushButton("Older")
        self.prev_btn.setAccessibleName("Newer history page")
        self.next_btn.setAccessibleName("Older history page")
        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)
        self.page_label = set_role(QLabel(""), "muted")

        top = QHBoxLayout()
        top.addWidget(QLabel("Show:"))
        top.addWidget(self.filter_combo)
        top.addStretch(1)
        top.addWidget(self.page_label)
        top.addWidget(self.prev_btn)
        top.addWidget(self.next_btn)
        top.addWidget(self.refresh_btn)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Download history")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_WHEN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_TRACK, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_ATTEMPTS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_WHEN, 140)
        self.table.setColumnWidth(COL_STATUS, 160)
        self.table.setColumnWidth(COL_ATTEMPTS, 56)
        self.table.selectionModel().selectionChanged.connect(self._show_details)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(120)
        self.details.setAccessibleName("Selected download details")
        self.details.setPlaceholderText("Select an entry to see its result and error details.")
        self.open_folder_btn = QPushButton("Open folder")
        self.open_folder_btn.setAccessibleName("Open the folder of the selected file")
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self._open_folder)

        self.listen_btn = QPushButton("Listen on SoundCloud")
        self.listen_btn.setAccessibleName("Listen to the selected history track on SoundCloud")
        self.listen_btn.setEnabled(False)
        self.listen_btn.clicked.connect(self._listen)
        bottom = QHBoxLayout()
        bottom.addWidget(self.listen_btn, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(self.details, 1)
        bottom.addWidget(self.open_folder_btn, 0, Qt.AlignmentFlag.AlignTop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)

    # --- data -------------------------------------------------------------------------

    def _statuses(self) -> tuple[JobStatus, ...] | None:
        return FILTERS[self.filter_combo.currentIndex()][1]

    def refresh(self) -> None:
        """Reload the current page (a few ms of SQLite on the GUI thread)."""
        try:
            rows = self._ctx.storage.download_history(
                limit=PAGE_SIZE + 1, offset=self._offset, statuses=self._statuses())
        except Exception as exc:
            log.exception("Could not load history")
            self.message.emit(f"Could not load history: {exc}")
            return
        self._has_more = len(rows) > PAGE_SIZE
        self.model.set_records(rows[:PAGE_SIZE])
        first = self._offset + 1 if rows else 0
        self.page_label.setText(f"{first}–{self._offset + min(len(rows), PAGE_SIZE)}"
                                if rows else "No entries")
        self.prev_btn.setEnabled(self._offset > 0)
        self.next_btn.setEnabled(self._has_more)
        self._show_details()

    def _filter_changed(self) -> None:
        self._offset = 0
        self.refresh()

    def _prev(self) -> None:
        self._offset = max(0, self._offset - PAGE_SIZE)
        self.refresh()

    def _next(self) -> None:
        if self._has_more:
            self._offset += PAGE_SIZE
            self.refresh()

    def selected_record(self) -> DownloadRecord | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model.record_at(rows[0].row()) if rows else None

    def _show_details(self, *_args: object) -> None:
        rec = self.selected_record()
        self.listen_btn.setEnabled(rec is not None)
        self.open_folder_btn.setEnabled(rec is not None and rec.file_path is not None)
        if rec is None:
            self.details.clear()
            return
        lines = [f"{STATUS_LABELS[rec.status]} · attempts: {rec.attempts}"
                 + (f" · {rec.bitrate_kbps} kbps" if rec.bitrate_kbps else "")]
        if rec.file_path is not None:
            lines.append(f"File: {rec.file_path}")
        if rec.error is not None:
            lines.append(f"Error ({rec.error.category.value}): {rec.error.user_message}")
            if rec.error.detail:
                lines.append("")
                lines.append(rec.error.detail)
        self.details.setPlainText("\n".join(lines))

    def _listen(self) -> None:
        from sclm.gui.bridge import run_in_background
        rec = self.selected_record()
        if rec is None:
            return
        def done(track):
            if track and track.url:
                self.listen_requested.emit(track.url)
            else:
                self.message.emit("No SoundCloud URL is saved for this track. Paste its URL in Listen.")
        run_in_background(lambda: self._ctx.storage.get_track(rec.track_id), done,
                          lambda _: self.message.emit("Could not find the saved track URL."), parent=self)

    def _open_folder(self) -> None:
        rec = self.selected_record()
        if rec is not None and rec.file_path is not None:
            if not open_containing_folder(Path(rec.file_path)):
                self.message.emit("Could not open the folder")
