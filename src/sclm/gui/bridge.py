"""Qt bridge between Qt-free workers and the GUI thread.

``DownloadBridge`` is the contract and is implemented here: it is a ``DownloadListener``
whose methods only emit Qt signals. The bridge object lives in the GUI thread, so
emitting from a worker thread is delivered to connected GUI slots via a queued
connection (Qt AutoConnection). Slots therefore always run on the GUI thread.

``run_in_background`` runs blocking calls such as
``download.preview.fetch_preview`` and ``metadata.scanner.scan_library`` off the GUI thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from sclm.core.errors import ErrorCategory, ErrorInfo, error_info_from_exception
from sclm.core.models import DownloadJob, ProgressEvent

log = logging.getLogger(__name__)

T = TypeVar("T")


class DownloadBridge(QObject):
    """Re-emits ``DownloadListener`` callbacks as Qt signals. Create it on the GUI thread."""

    job_changed = Signal(object)   # DownloadJob
    progress = Signal(object)      # ProgressEvent

    def on_job_changed(self, job: DownloadJob) -> None:
        self.job_changed.emit(job)

    def on_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)


NOT_AVAILABLE_MESSAGE = "This feature isn't available in this build yet."

# Handles stay referenced here until their result was delivered, so a caller that drops the
# returned handle can't lose the callbacks (or have the QObject collected mid-flight).
_ACTIVE: set[BackgroundTask] = set()


class BackgroundTask(QObject):
    """Handle for one ``run_in_background`` call. Lives in the GUI thread.

    The worker emits ``_finished_ok`` / ``_finished_err`` from the pool thread; both are
    connected to slots of this object, so Qt delivers them to the GUI thread as queued
    calls. ``discard()`` suppresses the callbacks (e.g. when the requesting view went away).
    """

    _finished_ok = Signal(object)
    _finished_err = Signal(object)

    def __init__(self, on_done: Callable[[Any], None], on_error: Callable[[ErrorInfo], None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._on_done: Callable[[Any], None] | None = on_done
        self._on_error: Callable[[ErrorInfo], None] | None = on_error
        self._finished = False
        self._finished_ok.connect(self._deliver_ok)
        self._finished_err.connect(self._deliver_err)

    @property
    def finished(self) -> bool:
        """True once a result (or error) was delivered or the task was discarded."""
        return self._finished

    def discard(self) -> None:
        """Drop the callbacks; the function may still run to completion in its thread."""
        self._on_done = self._on_error = None

    @Slot(object)
    def _deliver_ok(self, result: object) -> None:
        cb, self._on_done = self._on_done, None
        self._finish()
        if cb is not None:
            try:
                cb(result)
            except Exception:
                log.exception("Unhandled error in a background-task completion callback")

    @Slot(object)
    def _deliver_err(self, info: object) -> None:
        cb, self._on_error = self._on_error, None
        self._finish()
        if cb is not None:
            try:
                cb(info)  # type: ignore[arg-type]
            except Exception:
                log.exception("Unhandled error in a background-task error callback")

    def _finish(self) -> None:
        self._finished = True
        _ACTIVE.discard(self)


class _Runnable(QRunnable):
    def __init__(self, fn: Callable[[], Any], task: BackgroundTask) -> None:
        super().__init__()
        self._fn = fn
        self._task = task

    def run(self) -> None:
        try:
            result = self._fn()
        except NotImplementedError as exc:
            self._emit_error(ErrorInfo(ErrorCategory.UNKNOWN, NOT_AVAILABLE_MESSAGE,
                                       f"NotImplementedError: {exc}"))
        except BaseException as exc:  # noqa: BLE001 - everything must reach the GUI as an ErrorInfo
            if not isinstance(exc, Exception):
                log.warning("Background task ended with %r", exc)
            self._emit_error(error_info_from_exception(exc))
        else:
            self._emit(self._task._finished_ok, result)

    def _emit_error(self, info: ErrorInfo) -> None:
        self._emit(self._task._finished_err, info)

    def _emit(self, signal: Any, payload: object) -> None:
        try:
            signal.emit(payload)
        except RuntimeError:
            # The owning widget (and with it the handle) was destroyed while we ran.
            _ACTIVE.discard(self._task)


def run_in_background(
    fn: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[ErrorInfo], None],
    *,
    parent: QObject | None = None,
) -> BackgroundTask:
    """Run ``fn()`` on a ``QThreadPool`` worker; deliver ``on_done(result)`` or
    ``on_error(error_info_from_exception(exc))`` on the GUI thread (via a signal-carrying
    QObject owned by the GUI thread). Returns a handle the caller keeps alive until done.
    Cancellation is the caller's job: pass a ``threading.Event`` into ``fn`` via a closure.

    Must be called from the GUI thread. ``NotImplementedError`` (an unfinished stage) is
    reported as an ordinary error, never raised into the GUI.
    """
    task = BackgroundTask(on_done, on_error, parent)
    _ACTIVE.add(task)
    QThreadPool.globalInstance().start(_Runnable(fn, task))
    return task
