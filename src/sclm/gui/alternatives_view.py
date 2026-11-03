"""Alternatives page: failed SoundCloud downloads next to YouTube uploads of the same song.

The user compares lengths, opens a video to check it, and decides per track whether to
download one candidate's audio (lossy, re-encoded to MP3, tagged with the SoundCloud
metadata and artwork). Candidates that are only offered DRM-protected are labelled and can't
be downloaded. Searches, format checks and the failed-track list run in the background;
nothing is downloaded until the user confirms.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import DownloadRequest
from sclm.download.alternatives import (
    Availability,
    Candidate,
    FailedTrack,
    MatchLevel,
    SourceCheck,
    check_source,
    failed_tracks,
    is_youtube_video_url,
    search_query,
    search_youtube,
)
from sclm.gui import theme
from sclm.gui.bridge import run_in_background
from sclm.gui.common import format_duration, set_primary, set_role

log = logging.getLogger(__name__)

INTRO = (
    "Tracks SoundCloud won't let SCLM download (DRM-protected, Go+, unavailable or "
    "region-locked). Pick a YouTube upload of the same recording to use its audio instead: "
    "SoundCloud's title, artist and artwork are kept. YouTube audio is lossy (about "
    "130–160 kbps) and is re-encoded to MP3, so it can sound worse than the original. "
    "Download only what you're allowed to. Videos offered only DRM-protected are marked "
    "“DRM only” and are never downloaded.")

REASONS = {
    ErrorCategory.DRM_PROTECTED: "DRM-protected",
    ErrorCategory.AUTH_REQUIRED: "Sign-in / Go+",
    ErrorCategory.UNAVAILABLE: "Unavailable",
    ErrorCategory.GEO_BLOCKED: "Not in your region",
}
MATCH_LABELS = {MatchLevel.MATCH: "Same length", MatchLevel.CLOSE: "Close",
                MatchLevel.DIFFERENT: "Different", MatchLevel.UNKNOWN: "Unknown"}
MATCH_COLORS = {MatchLevel.MATCH: theme.SUCCESS, MatchLevel.CLOSE: theme.WARNING,
                MatchLevel.DIFFERENT: theme.DANGER, MatchLevel.UNKNOWN: theme.TEXT_MUTED}

F_ARTIST, F_TITLE, F_LENGTH, F_REASON = range(4)
_F_HEADERS = ("Artist", "Title", "Length", "Reason")
C_TITLE, C_CHANNEL, C_LENGTH, C_DELTA, C_MATCH, C_VIEWS, C_SOURCE = range(7)
_C_HEADERS = ("YouTube video", "Channel", "Length", "Δ", "Match", "Views", "Audio")


def availability_label(check: SourceCheck | None) -> str:
    if check is None:
        return "Checking…"
    if check.availability == Availability.AUDIO:
        return f"OK · {check.note}" if check.note else "OK"
    if check.availability == Availability.DRM_ONLY:
        return "DRM only — can't download"
    return f"Unavailable: {check.note}" if check.note else "Unavailable"


def _availability_color(check: SourceCheck | None) -> str:
    if check is None:
        return theme.TEXT_MUTED
    return {Availability.AUDIO: theme.SUCCESS, Availability.DRM_ONLY: theme.DANGER}.get(
        check.availability, theme.TEXT_MUTED)


def _delta(delta_s: float | None) -> str:
    return "" if delta_s is None else f"{delta_s:+.1f} s"


def _views(count: int | None) -> str:
    if count is None:
        return ""
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= scale:
            return f"{count / scale:.1f}".rstrip("0").rstrip(".") + suffix
    return str(count)


class _Model(QAbstractTableModel):
    headers: tuple[str, ...] = ()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int):  # noqa: ANN201
        return self._rows[row] if 0 <= row < len(self._rows) else None


class FailedModel(_Model):
    headers = _F_HEADERS

    def data(self, index: QModelIndex | QPersistentModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        item: FailedTrack = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == F_ARTIST:
                return item.track.artist
            if col == F_TITLE:
                return item.track.title
            if col == F_LENGTH:
                return format_duration(item.track.duration_s)
            return REASONS.get(item.error.category, item.error.category.value)
        if role == Qt.ItemDataRole.ToolTipRole and col == F_REASON:
            return item.error.user_message
        if role == Qt.ItemDataRole.TextAlignmentRole and col == F_LENGTH:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class CandidateModel(_Model):
    headers = _C_HEADERS

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.checks: dict[str, SourceCheck] = {}      # by video URL; shared across tracks

    def set_check(self, url: str, check: SourceCheck) -> None:
        self.checks[url] = check
        for row, cand in enumerate(self._rows):
            if cand.url == url:
                idx = self.index(row, C_SOURCE)
                self.dataChanged.emit(idx, idx)

    def data(self, index: QModelIndex | QPersistentModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        cand: Candidate = self._rows[index.row()]
        col = index.column()
        check = self.checks.get(cand.url)
        if role == Qt.ItemDataRole.DisplayRole:
            return {
                C_TITLE: cand.title,
                C_CHANNEL: cand.channel + (" ✓" if cand.channel_verified else ""),
                C_LENGTH: format_duration(cand.duration_s),
                C_DELTA: _delta(cand.delta_s),
                C_MATCH: MATCH_LABELS[cand.match],
                C_VIEWS: _views(cand.view_count),
                C_SOURCE: availability_label(check),
            }[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == C_MATCH:
                return QColor(MATCH_COLORS[cand.match])
            if col == C_SOURCE:
                return QColor(_availability_color(check))
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == C_TITLE:
                return f"{cand.title}\n{cand.url}"
            if col == C_CHANNEL and cand.channel_verified:
                return "Verified channel"
            if col == C_DELTA:
                return "YouTube length minus SoundCloud length"
            if col == C_SOURCE:
                return availability_label(check)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (C_LENGTH, C_DELTA, C_VIEWS):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


def _table(model: QAbstractTableModel, name: str) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setAccessibleName(name)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(28)
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return table


def _widths(table: QTableView, stretch: tuple[int, ...], fixed: dict[int, int]) -> None:
    header = table.horizontalHeader()
    for col in stretch:
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
    for col, width in fixed.items():
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(col, width)


class AlternativesView(QWidget):
    message = Signal(str)
    listen_requested = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None, *,
                 search_fn: Callable[[str, float | None], list[Candidate]] | None = None,
                 check_fn: Callable[[str], SourceCheck] | None = None,
                 load_fn: Callable[[], list[FailedTrack]] | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        cache = ctx.paths.cache_dir
        self._search_fn = search_fn or (lambda q, ref: search_youtube(
            q, reference_s=ref, cache_dir=cache))
        self._check_fn = check_fn or (lambda url: check_source(url, cache_dir=cache))
        self._load_fn = load_fn or (lambda: failed_tracks(ctx.storage))
        self._searches: dict[str, tuple[str, list[Candidate]]] = {}   # track_id → (query, results)
        self._searching: set[str] = set()
        self._checking: set[str] = set()
        self._queued: set[str] = set()                                 # track_ids
        self._tasks: list = []
        self._loading = False
        self.failed_model = FailedModel(self)
        self.candidate_model = CandidateModel(self)

        title = set_role(QLabel("Alternatives"), "title")
        intro = set_role(QLabel(INTRO), "muted")
        intro.setWordWrap(True)
        self.count_label = set_role(QLabel(""), "muted")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setAccessibleName("Reload the failed downloads")
        self.refresh_btn.clicked.connect(self.refresh)
        self.listen_btn = QPushButton("Listen on SoundCloud")
        self.listen_btn.setAccessibleName("Listen to the selected failed track on SoundCloud")
        self.listen_btn.setEnabled(False)
        self.listen_btn.clicked.connect(self._listen)
        top = QHBoxLayout()
        top.addWidget(set_role(QLabel("Failed downloads"), "heading"))
        top.addWidget(self.count_label)
        top.addStretch(1)
        top.addWidget(self.listen_btn)
        top.addWidget(self.refresh_btn)

        self.failed_table = _table(self.failed_model, "Failed downloads")
        _widths(self.failed_table, (F_ARTIST, F_TITLE), {F_LENGTH: 70, F_REASON: 150})
        self.failed_table.selectionModel().selectionChanged.connect(self._track_selected)

        self.query = QLineEdit()
        self.query.setAccessibleName("YouTube search text")
        self.query.setPlaceholderText("Select a failed download, or type artist and title")
        self.query.returnPressed.connect(self.search)
        self.search_btn = QPushButton("Search YouTube")
        self.search_btn.setAccessibleName("Search YouTube for the selected track")
        self.search_btn.setEnabled(False)
        self.search_btn.clicked.connect(self.search)
        search_row = QHBoxLayout()
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search_btn)

        self.candidate_table = _table(self.candidate_model, "YouTube candidates")
        _widths(self.candidate_table, (C_TITLE, C_CHANNEL),
                {C_LENGTH: 64, C_DELTA: 72, C_MATCH: 100, C_VIEWS: 64, C_SOURCE: 220})
        self.candidate_table.selectionModel().selectionChanged.connect(self._update_buttons)
        self.candidate_table.doubleClicked.connect(lambda _i: self.open_video())

        self.status = set_role(QLabel(""), "muted")
        self.status.setWordWrap(True)
        self.open_btn = QPushButton("Open video")
        self.open_btn.setAccessibleName("Open the selected video in your web browser")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_video)
        self.download_btn = QPushButton("Download as MP3")
        self.download_btn.setAccessibleName("Download the selected video's audio as MP3")
        self.download_btn.setEnabled(False)
        set_primary(self.download_btn)
        self.download_btn.clicked.connect(self.download)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.open_btn)
        bottom.addWidget(self.download_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(top)
        layout.addWidget(self.failed_table, 3)
        layout.addWidget(set_role(QLabel("YouTube candidates"), "heading"))
        layout.addLayout(search_row)
        layout.addWidget(self.candidate_table, 2)
        layout.addLayout(bottom)

        ctx.bridge.job_changed.connect(self._on_job_changed)

    # --- background helper ---------------------------------------------------------------

    def _run(self, fn, done, failed) -> None:  # noqa: ANN001
        self._tasks = [t for t in self._tasks if not t.finished]
        self._tasks.append(run_in_background(fn, done, failed, parent=self))

    # --- failed tracks ---------------------------------------------------------------------

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.setEnabled(False)

        def done(items: list[FailedTrack]) -> None:
            self._loading = False
            self.refresh_btn.setEnabled(True)
            self._set_failed(items)

        def failed(info: ErrorInfo) -> None:
            self._loading = False
            self.refresh_btn.setEnabled(True)
            log.warning("Could not load failed downloads: %s", info.detail)
            self.message.emit(f"Could not load failed downloads: {info.user_message}")

        self._run(self._load_fn, done, failed)

    def _set_failed(self, items: list[FailedTrack]) -> None:
        selected = self.selected_failed()
        keep = selected.track.track_id if selected else None
        self.failed_model.set_rows(items)
        self.count_label.setText(f"{len(items)} track(s)" if items else "none")
        for row, item in enumerate(items):
            if item.track.track_id == keep:
                self.failed_table.selectRow(row)
                break
        else:
            if items:
                self.failed_table.selectRow(0)
            else:
                self._track_selected()

    def selected_failed(self) -> FailedTrack | None:
        rows = self.failed_table.selectionModel().selectedRows()
        return self.failed_model.row_at(rows[0].row()) if rows else None

    def _track_selected(self, *_args: object) -> None:
        item = self.selected_failed()
        self.listen_btn.setEnabled(item is not None)
        self.search_btn.setEnabled(item is not None)
        if item is None:
            self.query.clear()
            self.candidate_model.set_rows([])
            self.status.setText("No failed downloads need an alternative." if
                                not self.failed_model.rowCount() else "")
            self._update_buttons()
            return
        cached = self._searches.get(item.track.track_id)
        if cached is not None:
            self.query.setText(cached[0])
            self._show_candidates(item, cached[1])
        else:
            self.query.setText(search_query(item.track))
            self.candidate_model.set_rows([])
            self.search()

    # --- search & checks -------------------------------------------------------------------

    def search(self) -> None:
        item = self.selected_failed()
        query = self.query.text().strip()
        if item is None or not query:
            return
        track_id = item.track.track_id
        if track_id in self._searching:
            return
        self._searching.add(track_id)
        self.search_btn.setEnabled(False)
        self.status.setText("Searching YouTube…")
        reference = item.track.duration_s

        def done(found: list[Candidate]) -> None:
            self._searching.discard(track_id)
            self._searches[track_id] = (query, found)
            current = self.selected_failed()
            if current is not None and current.track.track_id == track_id:
                self.search_btn.setEnabled(True)
                self._show_candidates(current, found)

        def failed(info: ErrorInfo) -> None:
            self._searching.discard(track_id)
            current = self.selected_failed()
            if current is not None and current.track.track_id == track_id:
                self.search_btn.setEnabled(True)
                self.status.setText(f"YouTube search failed: {info.user_message}")

        self._run(lambda: self._search_fn(query, reference), done, failed)

    def _show_candidates(self, item: FailedTrack, found: list[Candidate]) -> None:
        self.candidate_model.set_rows(found)
        if not found:
            self.status.setText("No YouTube videos found. Edit the search text and try again.")
        else:
            length = format_duration(item.track.duration_s) or "unknown"
            self.status.setText(f"SoundCloud length {length}. Same length usually means the "
                                "same recording; open a video to check it before downloading.")
            self.candidate_table.selectRow(0)
        for cand in found:
            self._check(cand.url)
        self._update_buttons()

    def _check(self, url: str) -> None:
        if url in self.candidate_model.checks or url in self._checking:
            return
        self._checking.add(url)

        def done(check: SourceCheck) -> None:
            self._checking.discard(url)
            self.candidate_model.set_check(url, check)
            self._update_buttons()

        def failed(info: ErrorInfo) -> None:
            self._checking.discard(url)
            self.candidate_model.set_check(
                url, SourceCheck(Availability.UNAVAILABLE, info.user_message))
            self._update_buttons()

        self._run(lambda: self._check_fn(url), done, failed)

    # --- actions ---------------------------------------------------------------------------

    def selected_candidate(self) -> Candidate | None:
        rows = self.candidate_table.selectionModel().selectedRows()
        return self.candidate_model.row_at(rows[0].row()) if rows else None

    def _update_buttons(self, *_args: object) -> None:
        cand = self.selected_candidate()
        item = self.selected_failed()
        check = self.candidate_model.checks.get(cand.url) if cand else None
        self.open_btn.setEnabled(cand is not None)
        queued = item is not None and item.track.track_id in self._queued
        self.download_btn.setEnabled(bool(cand and item and check and check.downloadable)
                                     and not queued)
        if cand is not None and check is not None and not check.downloadable:
            self.download_btn.setToolTip(availability_label(check))
        elif queued:
            self.download_btn.setToolTip("This track is already in the download queue")
        else:
            self.download_btn.setToolTip("")

    def open_video(self) -> None:
        cand = self.selected_candidate()
        if cand is not None and is_youtube_video_url(cand.url):
            QDesktopServices.openUrl(QUrl(cand.url))

    def _listen(self) -> None:
        item = self.selected_failed()
        if item is not None and item.track.url:
            self.listen_requested.emit(item.track.url)

    def _confirm(self, item: FailedTrack, cand: Candidate, check: SourceCheck) -> bool:
        answer = QMessageBox.question(
            self, "Download from YouTube?",
            f"Use the audio of “{cand.title}” ({cand.channel or 'unknown channel'}, "
            f"{format_duration(cand.duration_s) or 'unknown length'}) for "
            f"{item.track.artist} – {item.track.title}?\n\n"
            f"Source: {check.note or 'audio'}. YouTube audio is lossy and is re-encoded to "
            "MP3, so the quality can be lower than SoundCloud's. The SoundCloud title, artist "
            "and artwork are kept, and the file's comment notes the YouTube source.\n\n"
            "Download only what you are allowed to.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def download(self) -> None:
        item, cand = self.selected_failed(), self.selected_candidate()
        if item is None or cand is None:
            return
        check = self.candidate_model.checks.get(cand.url)
        if check is None or not check.downloadable:     # DRM-only and unchecked: never
            return
        if not self._confirm(item, cand, check):
            return
        jobs = self._ctx.manager.enqueue([DownloadRequest(item.track,
                                                          audio_source_url=cand.url)])
        if jobs:
            self._queued.add(item.track.track_id)
            self.message.emit(f"Queued {item.track.title} from YouTube — see Queue")
        else:
            self.message.emit(f"{item.track.title} is already in the download queue")
        self._update_buttons()

    def _on_job_changed(self, job: object) -> None:
        status = getattr(job, "status", None)
        track = getattr(job, "track", None)
        if status is not None and status.is_terminal and track is not None \
                and track.track_id in self._queued:
            self._queued.discard(track.track_id)
            self._update_buttons()
