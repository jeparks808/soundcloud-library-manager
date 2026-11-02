"""Qt bridge between Qt-free workers and the GUI thread.

``DownloadBridge`` is the contract and is implemented here: it is a ``DownloadListener``
whose methods only emit Qt signals. The bridge object lives in the GUI thread, so
emitting from a worker thread is delivered to connected GUI slots via a queued
connection (Qt AutoConnection). Slots therefore always run on the GUI thread.

``run_in_background`` (Stage 2 implements) runs blocking calls such as
``download.preview.fetch_preview`` and ``metadata.scanner.scan_library`` off the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Signal

from sclm.core.errors import ErrorInfo
from sclm.core.models import DownloadJob, ProgressEvent

T = TypeVar("T")


class DownloadBridge(QObject):
    """Re-emits ``DownloadListener`` callbacks as Qt signals. Create it on the GUI thread."""

    job_changed = Signal(object)   # DownloadJob
    progress = Signal(object)      # ProgressEvent

    def on_job_changed(self, job: DownloadJob) -> None:
        self.job_changed.emit(job)

    def on_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)


def run_in_background(
    fn: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[ErrorInfo], None],
    *,
    parent: QObject | None = None,
) -> Any:
    """Run ``fn()`` on a ``QThreadPool`` worker; deliver ``on_done(result)`` or
    ``on_error(error_info_from_exception(exc))`` on the GUI thread (via a signal-carrying
    QObject owned by the GUI thread). Returns a handle the caller keeps alive until done.
    Cancellation is the caller's job: pass a ``threading.Event`` into ``fn`` via a closure.
    """
    raise NotImplementedError
