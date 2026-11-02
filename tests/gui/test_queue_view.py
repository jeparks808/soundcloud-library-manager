from __future__ import annotations

import threading

from PySide6.QtCore import Qt

from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import JobStatus, ProgressEvent
from sclm.gui.queue_view import (
    COL_MESSAGE,
    COL_PROGRESS,
    COL_STATUS,
    FRACTION_ROLE,
    QueueView,
)


def emit_from_worker(fn):
    t = threading.Thread(target=fn, name="fake-worker")
    t.start()
    t.join()


def test_model_updates_from_bridge_signals_emitted_on_worker_thread(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    model = view.model
    job = make_job(1)

    def worker():
        ctx.bridge.on_job_changed(job)
        ctx.bridge.on_job_changed(make_job(2, JobStatus.RESOLVING))
        ctx.bridge.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=0.25,
                                             speed_bps=2_000_000, eta_s=12))

    emit_from_worker(worker)
    qtbot.waitUntil(lambda: model.rowCount() == 2, timeout=3000)
    assert model.index(0, COL_STATUS).data() == "Queued"
    assert model.index(1, COL_STATUS).data() == "Resolving"

    def worker2():
        ctx.bridge.on_job_changed(make_job(1, JobStatus.DOWNLOADING))
        ctx.bridge.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=0.5,
                                             speed_bps=2_000_000, eta_s=12))

    emit_from_worker(worker2)
    qtbot.waitUntil(lambda: model.index(0, COL_PROGRESS).data(FRACTION_ROLE) == 0.5,
                    timeout=3000)
    assert model.index(0, COL_PROGRESS).data() == "50%"
    assert "ETA 0:12" in model.index(0, COL_MESSAGE).data()
    assert model.index(0, COL_STATUS).data() == "Downloading"
    assert model.summary()["active"] == 2


def test_progress_is_coalesced_and_only_changed_rows_repaint(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    model = view.model
    model.set_jobs([make_job(1, JobStatus.DOWNLOADING), make_job(2, JobStatus.DOWNLOADING)])
    changes: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda tl, br, roles=None: changes.append((tl.row(), br.row())))

    for i in range(1, 201):                       # a burst: 200 events for job 1
        ctx.bridge.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=i / 200))
    qtbot.wait(10)
    assert changes == []                          # nothing applied before the timer fires
    qtbot.waitUntil(lambda: bool(changes), timeout=2000)
    qtbot.wait(250)

    assert changes == [(0, 0)]                    # one update, only row 0
    assert model.job(1).fraction == 1.0           # the newest value won
    assert model.job(2).fraction is None          # untouched row not repainted


def test_unchanged_progress_does_not_emit(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    model = view.model
    model.set_jobs([make_job(1, JobStatus.DOWNLOADING, fraction=0.4)])
    changes = []
    model.dataChanged.connect(lambda *a: changes.append(1))
    ctx.bridge.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=0.401))
    model.flush()
    assert changes == []


def test_late_progress_after_terminal_state_is_ignored(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    model = view.model
    model.set_jobs([make_job(1, JobStatus.DOWNLOADING)])
    model.on_job_changed(make_job(1, JobStatus.COMPLETED, fraction=1.0))
    model.flush()
    model.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=0.3))   # races the end
    model.flush()
    assert model.job(1).status == JobStatus.COMPLETED
    assert model.job(1).fraction == 1.0


def test_job_snapshot_supersedes_older_pending_progress(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    model = view.model
    model.set_jobs([make_job(1, JobStatus.DOWNLOADING, fraction=0.9)])
    model.on_progress(ProgressEvent(1, JobStatus.DOWNLOADING, fraction=0.95))
    model.on_job_changed(make_job(1, JobStatus.QUEUED))       # retried before the flush
    model.flush()
    assert model.job(1).status == JobStatus.QUEUED
    assert model.job(1).fraction is None


def test_actions_call_manager(qtbot, ctx, manager, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    failed = make_job(1, JobStatus.FAILED, error=ErrorInfo(ErrorCategory.NETWORK, "Offline"))
    dup = make_job(2, JobStatus.SKIPPED_DUPLICATE)
    running = make_job(3, JobStatus.DOWNLOADING)
    manager._jobs = [failed, dup, running]
    view.reload()
    assert view.model.rowCount() == 3
    assert view.model.index(0, COL_MESSAGE).data() == "Offline"

    view.table.selectRow(2)
    assert view.cancel_btn.isEnabled() and not view.retry_btn.isEnabled()
    view.cancel_btn.click()
    assert ("cancel", 3) in manager.calls

    view.table.selectRow(1)
    assert view.keep_both_btn.isEnabled()
    view.keep_both_btn.click()
    assert ("retry", 2, True) in manager.calls
    view.model.flush()
    assert view.model.job(2).status == JobStatus.QUEUED

    view.table.selectRow(0)
    assert not view.keep_both_btn.isEnabled()
    view.retry_btn.click()
    assert ("retry", 1, False) in manager.calls

    manager._set(3, status=JobStatus.COMPLETED)
    view.model.set_jobs(manager.jobs())
    view.clear_btn.click()
    assert ("clear_finished",) in manager.calls
    assert view.model.job(3) is None


def test_unfinished_manager_never_crashes_the_view(qtbot, unfinished_ctx, make_job):
    view = QueueView(unfinished_ctx)
    qtbot.addWidget(view)
    messages = []
    view.message.connect(messages.append)
    view.model.set_jobs([make_job(1, JobStatus.FAILED)])
    view.table.selectRow(0)
    view.retry_btn.click()
    view.cancel_all_btn.setEnabled(True)
    view.cancel_all_btn.click()
    view.clear_btn.click()
    assert messages and all("isn't available" in m for m in messages)


def test_tooltip_carries_error_detail(qtbot, ctx, make_job):
    view = QueueView(ctx)
    qtbot.addWidget(view)
    err = ErrorInfo(ErrorCategory.FFMPEG, "Conversion failed", "line1\nline2")
    view.model.set_jobs([make_job(1, JobStatus.FAILED, error=err)])
    tip = view.model.index(0, COL_MESSAGE).data(Qt.ItemDataRole.ToolTipRole)
    assert "Conversion failed" in tip and "line2" in tip
