"""Library page: searchable, paged ``storage.list_library_files`` with scan and tag editing."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import LibraryFile, ScanResult
from sclm.gui.bridge import BackgroundTask, run_in_background
from sclm.gui.common import ErrorBanner, format_duration, open_containing_folder, set_role

log = logging.getLogger(__name__)

PAGE_SIZE = 500
SEARCH_DEBOUNCE_MS = 250
COL_ARTIST, COL_TITLE, COL_TIME, COL_SOURCE, COL_PATH = range(5)
_HEADERS = ("Artist", "Title", "Time", "Source", "Path")


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[LibraryFile] = []

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
        f = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ARTIST:
                return f.artist
            if col == COL_TITLE:
                return f.title or f.path.stem
            if col == COL_TIME:
                return format_duration(f.duration_s)
            if col == COL_SOURCE:
                return "Downloaded" if f.source.value == "download" else "Scanned"
            return str(f.path)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == COL_TIME:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(f.path)
        return None

    def set_files(self, rows: list[LibraryFile]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def file_at(self, row: int) -> LibraryFile | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None


class LibraryView(QWidget):
    message = Signal(str)
    library_changed = Signal()      # after a scan or tag edit: status bar count refresh
    _scan_progress = Signal(int, int)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *,
                 scan_fn: Callable[..., ScanResult] | None = None,
                 editor_factory: Callable[[Path], object] | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._scan_fn = scan_fn
        self._editor_factory = editor_factory
        self._offset = 0
        self._has_more = False
        self._scan_task: BackgroundTask | None = None
        self._scan_cancel: threading.Event | None = None
        self.model = LibraryModel(self)

        title = set_role(QLabel("Library"), "title")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search artist, title or path…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Search the library")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._search_changed)
        self.search_edit.textChanged.connect(lambda _t: self._search_timer.start())

        self.scan_btn = QPushButton("Scan library")
        self.scan_btn.setAccessibleName("Scan the library folder for MP3 files")
        self.scan_btn.setToolTip("Index the MP3 files in your library folder so duplicates "
                                 "can be detected. Files are never changed.")
        self.stop_scan_btn = QPushButton("Stop")
        self.stop_scan_btn.setAccessibleName("Stop scanning")
        self.stop_scan_btn.hide()
        self.edit_btn = QPushButton("Edit tags…")
        self.edit_btn.setAccessibleName("Edit tags of the selected file")
        self.edit_btn.setShortcut("Ctrl+E")
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.setAccessibleName("Open the folder of the selected file")
        self.prev_btn = QPushButton("Previous")
        self.next_btn = QPushButton("Next")
        self.prev_btn.setAccessibleName("Previous page of the library")
        self.next_btn.setAccessibleName("Next page of the library")
        self.count_label = set_role(QLabel(""), "muted")
        self.scan_btn.clicked.connect(self.scan)
        self.stop_scan_btn.clicked.connect(self.stop_scan)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.folder_btn.clicked.connect(self.open_selected_folder)
        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)

        top = QHBoxLayout()
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.scan_btn)
        top.addWidget(self.stop_scan_btn)

        self.scan_bar = QProgressBar()
        self.scan_bar.setTextVisible(False)
        self.scan_bar.setFixedHeight(4)
        self.scan_bar.hide()
        self.scan_label = set_role(QLabel(""), "muted")
        self.scan_label.setWordWrap(True)
        self.error_banner = ErrorBanner()

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Library files")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_ARTIST, 240)
        self.table.setColumnWidth(COL_TIME, 70)
        self.table.setColumnWidth(COL_SOURCE, 100)
        self.table.doubleClicked.connect(lambda _i: self.edit_selected())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)

        bottom = QHBoxLayout()
        bottom.addWidget(self.count_label)
        bottom.addStretch(1)
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.next_btn)
        bottom.addSpacing(16)
        bottom.addWidget(self.folder_btn)
        bottom.addWidget(self.edit_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addLayout(top)
        layout.addWidget(self.scan_bar)
        layout.addWidget(self.scan_label)
        layout.addWidget(self.error_banner)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)

        self._scan_progress.connect(self._on_scan_progress)
        self._update_buttons()

    # --- listing ----------------------------------------------------------------------

    def refresh(self) -> None:
        search = self.search_edit.text().strip() or None
        try:
            rows = self._ctx.storage.list_library_files(
                search=search, limit=PAGE_SIZE + 1, offset=self._offset)
            total = self._ctx.storage.count_library_files()
        except Exception as exc:
            log.exception("Could not list library files")
            self.message.emit(f"Could not load the library: {exc}")
            return
        self._has_more = len(rows) > PAGE_SIZE
        self.model.set_files(rows[:PAGE_SIZE])
        shown = min(len(rows), PAGE_SIZE)
        if rows:
            self.count_label.setText(
                f"{self._offset + 1}–{self._offset + shown} · {total} file(s) in library")
        else:
            self.count_label.setText("No matching files" if search else
                                     "The library is empty. Download tracks or scan your folder.")
        self.prev_btn.setEnabled(self._offset > 0)
        self.next_btn.setEnabled(self._has_more)
        self._update_buttons()

    def _search_changed(self) -> None:
        self._offset = 0
        self.refresh()

    def _prev(self) -> None:
        self._offset = max(0, self._offset - PAGE_SIZE)
        self.refresh()

    def _next(self) -> None:
        if self._has_more:
            self._offset += PAGE_SIZE
            self.refresh()

    def selected_file(self) -> LibraryFile | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model.file_at(rows[0].row()) if rows else None

    def _update_buttons(self, *_args: object) -> None:
        has = self.selected_file() is not None
        self.edit_btn.setEnabled(has)
        self.folder_btn.setEnabled(has)

    # --- scanning ---------------------------------------------------------------------

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None

    def scan(self) -> None:
        if self._scan_task is not None:
            return
        self.error_banner.clear()
        root = self._ctx.settings.get().library_path
        storage = self._ctx.storage
        cancel = threading.Event()
        self._scan_cancel = cancel
        scan_fn = self._scan_fn
        if scan_fn is None:
            from sclm.metadata import scanner as _scanner   # looked up late: test seam

            def scan_fn(*a, **kw):  # type: ignore[no-redef]
                return _scanner.scan_library(*a, **kw)

        # Called on the scanning thread; the signal hops to the GUI thread (queued).
        def progress(done: int, total: int) -> None:
            self._scan_progress.emit(done, total)

        def work() -> ScanResult:
            return scan_fn(root, storage, progress=progress, cancel=cancel)

        self._scan_task = run_in_background(work, self._on_scan_done, self._on_scan_error,
                                            parent=self)
        self.scan_btn.setEnabled(False)
        self.stop_scan_btn.show()
        self.scan_bar.setRange(0, 0)
        self.scan_bar.show()
        self.scan_label.setText(f"Scanning {root} …")

    def stop_scan(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            self.scan_label.setText("Stopping…")

    @Slot(int, int)
    def _on_scan_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.scan_bar.setRange(0, total)
            self.scan_bar.setValue(min(done, total))
            self.scan_label.setText(f"Scanning… {done} of {total} files")

    def _scan_finished(self) -> None:
        self._scan_task = None
        self._scan_cancel = None
        self.scan_btn.setEnabled(True)
        self.stop_scan_btn.hide()
        self.scan_bar.hide()

    def _on_scan_done(self, result: ScanResult) -> None:
        self._scan_finished()
        text = (f"Scan finished: {result.files_seen} file(s) seen, {result.added} added, "
                f"{result.updated} updated, {result.removed_missing} missing removed from "
                f"the index")
        if result.errors:
            text += f", {len(result.errors)} unreadable"
        self.scan_label.setText(text)
        if result.errors:
            self.scan_label.setToolTip("\n".join(result.errors[:50]))
        self.message.emit("Library scan finished")
        self.refresh()
        self.library_changed.emit()

    def _on_scan_error(self, error: ErrorInfo) -> None:
        self._scan_finished()
        if error.category == ErrorCategory.CANCELLED:
            self.scan_label.setText("Scan cancelled.")
            self.refresh()
            self.library_changed.emit()
            return
        self.scan_label.setText("")
        self.error_banner.show_error(error)

    # --- row actions ------------------------------------------------------------------

    def open_selected_folder(self) -> None:
        f = self.selected_file()
        if f is not None and not open_containing_folder(f.path):
            self.message.emit("Could not open the folder")

    def edit_selected(self) -> None:
        f = self.selected_file()
        if f is None:
            return
        if self._editor_factory is not None:
            self._editor_factory(f.path)
            return
        from sclm.gui.metadata_editor import MetadataEditorDialog

        dialog = MetadataEditorDialog(self._ctx, f.path, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.saved.connect(lambda _lf: (self.refresh(), self.library_changed.emit()))
        dialog.open()
