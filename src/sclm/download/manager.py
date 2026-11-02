"""DownloadManager: bounded worker pool running download jobs (Stage 3 implements).

STUB. Signatures and documented behavior are the contract (docs/ARCHITECTURE.md).
Stage 3 may add private helpers and extra keyword-only test seams (e.g. a YoutubeDL
factory), but must not change the public signatures below.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from sclm.core.events import DownloadListener
from sclm.core.models import (
    DownloadJob,
    DownloadRequest,
    DuplicateMatch,
    FinalizeResult,
    JobStatus,
    TrackInfo,
)
from sclm.core.paths import AppPaths
from sclm.core.settings import Settings, SettingsStore
from sclm.core.storage import Storage


class FinalizeFn(Protocol):
    """Signature of :func:`sclm.metadata.pipeline.finalize_track`."""

    def __call__(self, staged_file: Path, track: TrackInfo, *, playlist_title: str | None,
                 settings: Settings, storage: Storage, force: bool = False,
                 on_stage: Callable[[JobStatus], None] | None = None,
                 cancel: threading.Event | None = None) -> FinalizeResult: ...


FindDuplicateFn = Callable[[Storage, TrackInfo], DuplicateMatch | None]
"""Signature of :func:`sclm.metadata.duplicates.find_duplicate`."""


class DownloadManager:
    """Owns the job queue and a bounded thread pool. Pure Python, no Qt.

    Construction: cleans stale per-job staging dirs under ``paths.staging_dir`` and starts no
    work. ``finalize`` / ``find_duplicate`` default to the real metadata functions (imported
    lazily); tests inject fakes so Stage 3 can be built before Stage 4 lands.

    Job lifecycle (one worker thread per running job):
      QUEUED -> RESOLVING -> [duplicate pre-check] -> DOWNLOADING -> CONVERTING
      -> TAGGING -> ORGANIZING -> COMPLETED | SKIPPED_DUPLICATE
      any step -> FAILED | CANCELLED;  NETWORK errors -> WAITING_RETRY -> RESOLVING ...
    Every transition is persisted with ``storage.update_download`` and announced with
    ``listener.on_job_changed``. The manager is the ONLY writer of download-flow DB rows
    (downloads, tracks, library_files for placed files).
    """

    def __init__(
        self,
        storage: Storage,
        settings: SettingsStore,
        paths: AppPaths,
        listener: DownloadListener | None = None,
        *,
        finalize: FinalizeFn | None = None,
        find_duplicate: FindDuplicateFn | None = None,
    ) -> None:
        raise NotImplementedError

    def set_listener(self, listener: DownloadListener | None) -> None:
        """Replace the listener (None -> NullListener)."""
        raise NotImplementedError

    def enqueue(self, requests: Iterable[DownloadRequest]) -> list[DownloadJob]:
        """Create a QUEUED job (and ``downloads`` row) per request and schedule it.

        Requests whose track_id already has a non-terminal job are ignored. Returns the jobs
        actually created, in order. Emits ``on_job_changed`` for each. Non-blocking (only a
        quick DB insert per job); callable from the GUI thread.
        """
        raise NotImplementedError

    def cancel(self, job_id: int) -> bool:
        """Request cancellation. QUEUED jobs become CANCELLED immediately; running jobs stop
        at the next cooperative check (progress hook, between steps, backoff wait). Returns
        False if the job is unknown or already terminal. Never blocks."""
        raise NotImplementedError

    def cancel_all(self) -> int:
        """Cancel every non-terminal job. Returns how many were signalled."""
        raise NotImplementedError

    def retry(self, job_id: int, *, force: bool = False) -> DownloadJob | None:
        """Re-queue a FAILED, CANCELLED or SKIPPED_DUPLICATE job (same job_id / DB row).

        ``force=True`` skips duplicate checks and uses KEEP_BOTH for path conflicts (the
        GUI's "Keep both" action for duplicates). Returns the re-queued job, or None if the
        job is unknown or not terminal.
        """
        raise NotImplementedError

    def clear_finished(self) -> list[int]:
        """Forget terminal jobs from the in-memory queue (DB history is kept). Returns their ids."""
        raise NotImplementedError

    def jobs(self) -> list[DownloadJob]:
        """Snapshot of all jobs in the in-memory queue, in enqueue order."""
        raise NotImplementedError

    def get_job(self, job_id: int) -> DownloadJob | None:
        raise NotImplementedError

    def is_idle(self) -> bool:
        """True when no job is QUEUED or active."""
        raise NotImplementedError

    def set_concurrency(self, workers: int) -> None:
        """Change the number of jobs that may run at once (1..4). Takes effect as running
        jobs finish; never interrupts a running job. The manager also subscribes to
        SettingsStore and calls this when ``Settings.concurrency`` changes."""
        raise NotImplementedError

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Cancel all jobs and stop the pool. With ``wait``, block up to ``timeout`` seconds
        for workers to exit and staging dirs to be removed. Idempotent. Called by app.main
        after the Qt event loop ends (never from a worker)."""
        raise NotImplementedError
