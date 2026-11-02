"""Callback protocol between the (Qt-free) DownloadManager and its observer (the GUI bridge).

Contract (see docs/ARCHITECTURE.md, "Threading model"):

* Methods are called from ANY thread: worker threads for running jobs, and the caller's
  thread for ``enqueue``/``cancel``/``retry`` on queued jobs.
* Implementations must return quickly and must not block. The GUI bridge only re-emits a
  Qt signal (AutoConnection -> queued delivery to the GUI thread).
* The manager never holds its internal lock while calling the listener, so a listener may
  call back into the manager, but should not need to.
* Exceptions raised by a listener are logged and swallowed by the manager.
* ``on_job_changed`` fires on every change of a job's status/attempts/message/error/paths,
  including creation (status QUEUED) and retry. Payloads are immutable snapshots.
* ``on_progress`` fires only while DOWNLOADING/CONVERTING, throttled to <= 4/s per job.
  It may arrive after a terminal ``on_job_changed`` (thread race): receivers must ignore
  progress for jobs whose last known status is terminal.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sclm.core.models import DownloadJob, ProgressEvent


@runtime_checkable
class DownloadListener(Protocol):
    def on_job_changed(self, job: DownloadJob) -> None: ...

    def on_progress(self, event: ProgressEvent) -> None: ...


class NullListener:
    """Listener that ignores everything (default; headless use and tests)."""

    def on_job_changed(self, job: DownloadJob) -> None:
        pass

    def on_progress(self, event: ProgressEvent) -> None:
        pass


class RecordingListener:
    """Thread-safe listener that records events. For tests in any package."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self.jobs: list[DownloadJob] = []
        self.progress: list[ProgressEvent] = []

    def on_job_changed(self, job: DownloadJob) -> None:
        with self._lock:
            self.jobs.append(job)

    def on_progress(self, event: ProgressEvent) -> None:
        with self._lock:
            self.progress.append(event)

    def statuses(self, job_id: int) -> list[str]:
        with self._lock:
            return [str(j.status) for j in self.jobs if j.job_id == job_id]

    def last(self, job_id: int) -> DownloadJob | None:
        with self._lock:
            for job in reversed(self.jobs):
                if job.job_id == job_id:
                    return job
        return None
