"""GUI test fixtures: a fake DownloadManager and an AppContext built on real Storage/Settings."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterable

import pytest

from sclm.context import AppContext
from sclm.core.models import DownloadJob, DownloadRequest, JobStatus
from sclm.gui.bridge import DownloadBridge
from sclm.gui.theme import apply_theme


class FakeManager:
    """Implements the documented ``DownloadManager`` public API with in-memory state."""

    def __init__(self) -> None:
        self._jobs: list[DownloadJob] = []
        self._next_id = 1
        self.calls: list[tuple] = []
        self.enqueue_threads: list[str] = []
        self.idle = True

    def set_listener(self, listener) -> None:  # noqa: ANN001
        self.listener = listener

    def enqueue(self, requests: Iterable[DownloadRequest]) -> list[DownloadJob]:
        self.enqueue_threads.append(threading.current_thread().name)
        created = []
        active_ids = {j.track.track_id for j in self._jobs if not j.status.is_terminal}
        for req in requests:
            if req.track.track_id in active_ids:
                continue
            job = DownloadJob(self._next_id, req.track, req.playlist_id, req.playlist_title,
                              force=req.force)
            self._next_id += 1
            self._jobs.append(job)
            created.append(job)
            active_ids.add(req.track.track_id)
        self.calls.append(("enqueue", list(requests)))
        return created

    def _set(self, job_id: int, **changes) -> DownloadJob | None:  # noqa: ANN003
        for i, j in enumerate(self._jobs):
            if j.job_id == job_id:
                self._jobs[i] = dataclasses.replace(j, **changes)
                return self._jobs[i]
        return None

    def cancel(self, job_id: int) -> bool:
        self.calls.append(("cancel", job_id))
        job = self.get_job(job_id)
        if job is None or job.status.is_terminal:
            return False
        self._set(job_id, status=JobStatus.CANCELLED)
        return True

    def cancel_all(self) -> int:
        self.calls.append(("cancel_all",))
        n = 0
        for j in list(self._jobs):
            if not j.status.is_terminal:
                self._set(j.job_id, status=JobStatus.CANCELLED)
                n += 1
        return n

    def retry(self, job_id: int, *, force: bool = False) -> DownloadJob | None:
        self.calls.append(("retry", job_id, force))
        job = self.get_job(job_id)
        if job is None or not job.status.is_terminal:
            return None
        return self._set(job_id, status=JobStatus.QUEUED, force=force, error=None,
                         fraction=None)

    def clear_finished(self) -> list[int]:
        self.calls.append(("clear_finished",))
        gone = [j.job_id for j in self._jobs if j.status.is_terminal]
        self._jobs = [j for j in self._jobs if not j.status.is_terminal]
        return gone

    def jobs(self) -> list[DownloadJob]:
        return list(self._jobs)

    def get_job(self, job_id: int) -> DownloadJob | None:
        return next((j for j in self._jobs if j.job_id == job_id), None)

    def is_idle(self) -> bool:
        return self.idle

    def set_concurrency(self, workers: int) -> None:
        self.calls.append(("set_concurrency", workers))

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        self.calls.append(("shutdown",))


class UnfinishedManager:
    """Every method raises NotImplementedError, like the Stage 3 stub."""

    def __getattr__(self, name: str):  # noqa: ANN204
        def method(*_a, **_kw):  # noqa: ANN002, ANN003
            raise NotImplementedError(name)
        return method


@pytest.fixture(scope="session", autouse=True)
def _themed(qapp) -> None:  # noqa: ANN001
    apply_theme(qapp)


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def ctx(app_paths, settings_store, storage, manager) -> AppContext:
    return AppContext(paths=app_paths, settings=settings_store, storage=storage,
                      manager=manager, bridge=DownloadBridge())


@pytest.fixture
def unfinished_ctx(ctx) -> AppContext:
    """Context whose manager behaves like the not-yet-implemented Stage 3 stub."""
    return dataclasses.replace(ctx, manager=UnfinishedManager())


@pytest.fixture
def make_job(make_track):
    def factory(job_id: int, status: JobStatus = JobStatus.QUEUED, **kw) -> DownloadJob:  # noqa: ANN003
        return DownloadJob(job_id, make_track(str(1000 + job_id)), status=status, **kw)
    return factory
