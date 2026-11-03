"""Download queue: a table model fed by bridge signals, a progress-bar delegate and actions.

Progress events arrive at up to 4 per second per job. The model never touches the view per
event: it stores the newest event per job and applies the batch from a single-shot timer
(``FLUSH_INTERVAL_MS``), emitting ``dataChanged`` only for rows that actually changed.
"""

from __future__ import annotations

import dataclasses
import logging

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRectF,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sclm.context import AppContext
from sclm.core.models import DownloadJob, JobStatus, ProgressEvent
from sclm.gui import theme
from sclm.gui.common import STATUS_LABELS, format_duration, format_speed, set_primary

log = logging.getLogger(__name__)

FLUSH_INTERVAL_MS = 100          # at most ~10 model updates per second
COL_TRACK, COL_STATUS, COL_PROGRESS, COL_MESSAGE = range(4)
_HEADERS = ("Track", "Status", "Progress", "Details")
FRACTION_ROLE = Qt.ItemDataRole.UserRole + 1
JOB_ROLE = Qt.ItemDataRole.UserRole + 2


def bitrate_note(actual_kbps: int | None, target_kbps: int | None = None) -> str:
    """`` · 128 kbps (original MP3 kept)`` when below the target, else `` · 320 kbps``."""
    if not actual_kbps:
        return ""
    if target_kbps and actual_kbps < target_kbps:
        return f" · {actual_kbps} kbps (original MP3 kept)"
    return f" · {actual_kbps} kbps"


def _progress_text(job: DownloadJob, event: ProgressEvent | None,
                   target_kbps: int | None = None) -> str:
    if job.message:
        return job.message
    if event is not None and job.status == JobStatus.DOWNLOADING:
        parts = [format_speed(event.speed_bps)]
        if event.eta_s is not None:
            parts.append(f"ETA {format_duration(event.eta_s)}")
        return "  ·  ".join(p for p in parts if p)
    if job.error is not None:
        return job.error.user_message
    if job.status == JobStatus.SKIPPED_DUPLICATE and job.duplicate_of is not None:
        return f"Already in library: {job.duplicate_of.name}"
    if job.status == JobStatus.COMPLETED and job.final_path is not None:
        return job.final_path.name + bitrate_note(job.actual_bitrate_kbps, target_kbps)
    return ""


class QueueModel(QAbstractTableModel):
    """Rows are ``DownloadJob`` snapshots in enqueue order.

    ``on_job_changed`` / ``on_progress`` are the slots to connect to the bridge signals:
    the model lives in the GUI thread, so Qt queues emissions from worker threads.
    """

    summary_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.target_kbps: int | None = None   # configured bitrate, set by the view
        self._jobs: list[DownloadJob] = []
        self._row_of: dict[int, int] = {}
        self._events: dict[int, ProgressEvent] = {}      # last applied event per job (details)
        self._pending_jobs: dict[int, DownloadJob] = {}  # newest job snapshot per id
        self._pending_progress: dict[int, ProgressEvent] = {}
        # Ids removed by "Clear finished". The manager announces a final state before it
        # reports the job finished, so that event can still be queued when the job is
        # cleared; it must not bring the row back (job ids are never reused).
        self._forgotten: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self.flush)

    # --- Qt model API ---------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._jobs)

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
        job = self._jobs[index.row()]
        col = index.column()
        if role == JOB_ROLE:
            return job
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_TRACK:
                return job.track.display_name
            if col == COL_STATUS:
                return STATUS_LABELS[job.status]
            if col == COL_PROGRESS:
                return f"{round(job.fraction * 100)}%" if job.fraction is not None else ""
            return _progress_text(job, self._events.get(job.job_id), self.target_kbps)
        if role == FRACTION_ROLE and col == COL_PROGRESS:
            if job.status == JobStatus.COMPLETED:
                return 1.0
            return job.fraction
        if role == Qt.ItemDataRole.TextAlignmentRole and col == COL_PROGRESS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == COL_STATUS:
            return QColor(theme.STATUS_COLORS[job.status])
        if role == Qt.ItemDataRole.ToolTipRole:
            if job.error is not None:
                return (f"{job.error.user_message}\n\n{job.error.detail}"
                        if job.error.detail else job.error.user_message)
            return job.track.url
        return None

    # --- public API -----------------------------------------------------------------

    def job_at(self, row: int) -> DownloadJob | None:
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    def job(self, job_id: int) -> DownloadJob | None:
        row = self._row_of.get(job_id)
        return self._jobs[row] if row is not None else None

    def jobs(self) -> list[DownloadJob]:
        return list(self._jobs)

    def set_jobs(self, jobs: list[DownloadJob]) -> None:
        """Replace everything (initial load from ``manager.jobs()``)."""
        self.beginResetModel()
        self._jobs = list(jobs)
        self._row_of = {j.job_id: i for i, j in enumerate(self._jobs)}
        self._forgotten.difference_update(self._row_of)
        self._events.clear()
        self._pending_jobs.clear()
        self._pending_progress.clear()
        self.endResetModel()
        self.summary_changed.emit()

    def remove_jobs(self, job_ids: list[int]) -> None:
        gone = set(job_ids)
        self._forgotten.update(gone)
        if not gone & self._row_of.keys():
            return
        self.beginResetModel()
        self._jobs = [j for j in self._jobs if j.job_id not in gone]
        self._row_of = {j.job_id: i for i, j in enumerate(self._jobs)}
        for table in (self._events, self._pending_jobs, self._pending_progress):
            for jid in gone:
                table.pop(jid, None)
        self.endResetModel()
        self.summary_changed.emit()

    def summary(self) -> dict[str, int]:
        """Counts for the status bar (includes not-yet-flushed changes)."""
        counts = {"active": 0, "queued": 0, "done": 0, "failed": 0, "skipped": 0,
                  "cancelled": 0}
        for job in self._jobs:
            s = job.status
            if s.is_active:
                counts["active"] += 1
            elif s == JobStatus.QUEUED:
                counts["queued"] += 1
            elif s == JobStatus.COMPLETED:
                counts["done"] += 1
            elif s == JobStatus.FAILED:
                counts["failed"] += 1
            elif s == JobStatus.SKIPPED_DUPLICATE:
                counts["skipped"] += 1
            elif s == JobStatus.CANCELLED:
                counts["cancelled"] += 1
        return counts

    # --- bridge slots (GUI thread) -----------------------------------------------------

    @Slot(object)
    def on_job_changed(self, job: DownloadJob) -> None:
        if job.job_id in self._forgotten:       # late event for a cleared job
            return
        self._pending_jobs[job.job_id] = job
        # A progress event older than this snapshot must not overwrite its fraction.
        self._pending_progress.pop(job.job_id, None)
        self._schedule()

    @Slot(object)
    def on_progress(self, event: ProgressEvent) -> None:
        self._pending_progress[event.job_id] = event
        self._schedule()

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    # --- coalesced application ---------------------------------------------------------

    @Slot()
    def flush(self) -> None:
        """Apply pending changes now (normally called by the timer)."""
        self._timer.stop()
        pending_jobs, self._pending_jobs = self._pending_jobs, {}
        pending_progress, self._pending_progress = self._pending_progress, {}
        if not pending_jobs and not pending_progress:
            return

        changed_rows: set[int] = set()
        new_jobs: list[DownloadJob] = []
        for job_id, job in pending_jobs.items():
            row = self._row_of.get(job_id)
            if row is None:
                new_jobs.append(job)
            elif self._jobs[row] != job:
                self._jobs[row] = job
                changed_rows.add(row)
            if job.status.is_terminal or job.status == JobStatus.QUEUED:
                self._events.pop(job_id, None)

        for job_id, event in pending_progress.items():
            row = self._row_of.get(job_id)
            if row is None:
                continue
            job = self._jobs[row]
            if job.status.is_terminal:          # late progress after the final state
                continue
            self._events[job_id] = event
            fraction = event.fraction if event.fraction is not None else job.fraction
            if fraction is not None:
                fraction = max(0.0, min(1.0, fraction))
            status = job.status
            if status.is_active and event.status != status and event.status.is_active:
                status = event.status           # CONVERTING arrives before its job_changed
            if (status != job.status or not _same_fraction(fraction, job.fraction)
                    or event.speed_bps or event.eta_s is not None):
                self._jobs[row] = dataclasses.replace(job, fraction=fraction, status=status)
                changed_rows.add(row)

        for first, last in _ranges(sorted(changed_rows)):
            self.dataChanged.emit(self.index(first, 0), self.index(last, len(_HEADERS) - 1))
        if new_jobs:
            start = len(self._jobs)
            self.beginInsertRows(QModelIndex(), start, start + len(new_jobs) - 1)
            for job in new_jobs:
                self._row_of[job.job_id] = len(self._jobs)
                self._jobs.append(job)
            self.endInsertRows()
        self.summary_changed.emit()


def _same_fraction(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < 0.005


def _ranges(rows: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for row in rows:
        if out and out[-1][1] == row - 1:
            out[-1] = (out[-1][0], row)
        else:
            out.append((row, row))
    return out


class ProgressDelegate(QStyledItemDelegate):
    """Draws a thin rounded progress bar plus percentage; no per-row widgets."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        fraction = index.data(FRACTION_ROLE)
        job = index.data(JOB_ROLE)
        super().paint(painter, option, index)  # background / selection
        if fraction is None or job is None:
            return
        rect = QRectF(option.rect).adjusted(8, 0, -52, 0)
        bar_h = 6.0
        bar = QRectF(rect.left(), rect.center().y() - bar_h / 2, rect.width(), bar_h)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(bar, 3, 3)
        color = theme.STATUS_COLORS.get(job.status, theme.ACCENT)
        painter.setBrush(QColor(color))
        fill = QRectF(bar.left(), bar.top(), bar.width() * max(0.0, min(1.0, fraction)), bar_h)
        painter.drawRoundedRect(fill, 3, 3)
        painter.restore()


class QueueView(QWidget):
    """The Queue page: table + Cancel / Retry / Keep both / Cancel all / Clear finished."""

    message = Signal(str)   # transient status-bar text
    listen_requested = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.model = QueueModel(self)
        self.model.target_kbps = ctx.settings.get().bitrate_kbps
        ctx.settings.subscribe(lambda st: setattr(self.model, 'target_kbps', st.bitrate_kbps))

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Download queue")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(COL_TRACK, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_MESSAGE, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_STATUS, 150)
        self.table.setColumnWidth(COL_PROGRESS, 220)
        self.table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.table))

        self.listen_btn = QPushButton("Listen on SoundCloud")
        self.listen_btn.setAccessibleName("Listen to the selected track on SoundCloud")
        self.listen_btn.clicked.connect(self.listen_selected)
        self.cancel_btn = QPushButton("Cancel")
        self.retry_btn = QPushButton("Retry")
        self.keep_both_btn = QPushButton("Keep both")
        self.cancel_all_btn = QPushButton("Cancel all")
        self.clear_btn = QPushButton("Clear finished")
        self.cancel_btn.setAccessibleName("Cancel selected downloads")
        self.retry_btn.setAccessibleName("Retry selected downloads")
        self.keep_both_btn.setAccessibleName("Keep both copies of the selected duplicates")
        self.keep_both_btn.setToolTip("Download the track again even though it is already "
                                      "in the library (the existing file is never replaced)")
        self.cancel_all_btn.setAccessibleName("Cancel all downloads")
        self.clear_btn.setAccessibleName("Clear finished downloads from the list")
        self.cancel_btn.setShortcut("Ctrl+Backspace")
        self.clear_btn.setToolTip("Remove finished jobs from this list (history is kept)")
        set_primary(self.retry_btn)
        for btn, slot in ((self.cancel_btn, self.cancel_selected),
                          (self.retry_btn, self.retry_selected),
                          (self.keep_both_btn, self.keep_both_selected),
                          (self.cancel_all_btn, self.cancel_all),
                          (self.clear_btn, self.clear_finished)):
            btn.clicked.connect(slot)

        bar = QHBoxLayout()
        for btn in (self.cancel_btn, self.retry_btn, self.keep_both_btn, self.listen_btn):
            bar.addWidget(btn)
        bar.addStretch(1)
        bar.addWidget(self.cancel_all_btn)
        bar.addWidget(self.clear_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(10)
        layout.addLayout(bar)
        layout.addWidget(self.table, 1)

        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        self.model.modelReset.connect(self._update_buttons)
        self.model.dataChanged.connect(self._update_buttons)
        self.model.rowsInserted.connect(self._update_buttons)

        ctx.bridge.job_changed.connect(self.model.on_job_changed)
        ctx.bridge.progress.connect(self.model.on_progress)
        self.reload()
        self._update_buttons()

    # --- loading -----------------------------------------------------------------

    def reload(self) -> None:
        """Initial load of the in-memory queue from the manager."""
        try:
            self.model.set_jobs(self._ctx.manager.jobs())
        except NotImplementedError:
            self.model.set_jobs([])

    # --- actions -----------------------------------------------------------------

    def selected_jobs(self) -> list[DownloadJob]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [j for r in rows if (j := self.model.job_at(r)) is not None]

    def listen_selected(self) -> None:
        selected = self.selected_jobs()
        if len(selected) == 1:
            self.listen_requested.emit(selected[0].track.url)

    def cancel_selected(self) -> None:
        count = 0
        for job in self.selected_jobs():
            if not job.status.is_terminal and self._call(self._ctx.manager.cancel, job.job_id):
                count += 1
        if count:
            self.message.emit(f"Cancelling {count} download(s)")

    def retry_selected(self) -> None:
        self._requeue(force=False,
                      allowed=(JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED_DUPLICATE))

    def keep_both_selected(self) -> None:
        self._requeue(force=True, allowed=(JobStatus.SKIPPED_DUPLICATE,))

    def _requeue(self, *, force: bool, allowed: tuple[JobStatus, ...]) -> None:
        count = 0
        for job in self.selected_jobs():
            if job.status not in allowed:
                continue
            new = self._call(self._ctx.manager.retry, job.job_id, force=force)
            if new is not None:
                self.model.on_job_changed(new)
                count += 1
        if count:
            self.message.emit(f"Re-queued {count} download(s)")

    def cancel_all(self) -> None:
        count = self._call(self._ctx.manager.cancel_all)
        if count:
            self.message.emit(f"Cancelling {count} download(s)")

    def clear_finished(self) -> None:
        ids = self._call(self._ctx.manager.clear_finished)
        if ids:
            self.model.flush()                      # make sure terminal states are applied
            self.model.remove_jobs(list(ids))
            self.message.emit(f"Cleared {len(ids)} finished download(s)")

    def _call(self, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return fn(*args, **kwargs)
        except NotImplementedError:
            self.message.emit("Downloading isn't available in this build yet")
        except Exception as exc:
            log.exception("Queue action failed")
            self.message.emit(f"Action failed: {exc}")
        return None

    def _update_buttons(self, *_args: object) -> None:
        selected = self.selected_jobs()
        self.listen_btn.setEnabled(len(selected) == 1)
        self.cancel_btn.setEnabled(any(not j.status.is_terminal for j in selected))
        self.retry_btn.setEnabled(any(j.status in (JobStatus.FAILED, JobStatus.CANCELLED,
                                                   JobStatus.SKIPPED_DUPLICATE)
                                      for j in selected))
        self.keep_both_btn.setEnabled(any(j.status == JobStatus.SKIPPED_DUPLICATE
                                          for j in selected))
        summary = self.model.summary()
        pending = summary["active"] + summary["queued"]
        self.cancel_all_btn.setEnabled(pending > 0)
        self.clear_btn.setEnabled(any(j.status.is_terminal for j in self.model.jobs()))
