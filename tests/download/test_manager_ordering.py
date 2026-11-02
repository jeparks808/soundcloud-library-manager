"""Stage 5: per-job ordering of in-memory state, DB writes and listener events.

Invariants (ARCHITECTURE.md §6 "Ordering"):

* Per job, the in-memory state, the ``downloads`` row and the listener see the same sequence
  of states, even when ``cancel`` / ``retry`` run on other threads than the worker.
* A terminal state becomes visible through ``get_job`` / ``jobs`` / ``is_idle`` /
  ``clear_finished`` / ``retry`` only after it has been persisted and announced.
* A terminal state is never overwritten by a stale non-terminal one (only ``retry`` reopens).

The races are made deterministic by blocking a DB write or a listener call on an event.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError

from sclm.core.events import RecordingListener
from sclm.core.models import DownloadJob, DownloadRequest, JobStatus
from sclm.core.storage import Storage
from sclm.download.manager import DownloadManager

S = JobStatus
GATE_TIMEOUT_S = 10.0


class RecordingStorage(Storage):
    """Real SQLite storage that records ``update_download`` calls in commit order and can
    block the next write of a given status until the test releases it."""

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.writes: list[tuple[int, JobStatus]] = []
        self._gate: tuple[JobStatus, threading.Event, threading.Event] | None = None

    def block_next(self, status: JobStatus) -> tuple[threading.Event, threading.Event]:
        entered, release = threading.Event(), threading.Event()
        self._gate = (status, entered, release)
        return entered, release

    def update_download(self, download_id: int, *, status: JobStatus, **kw: Any) -> None:
        gate = self._gate
        if gate is not None and gate[0] == status:
            self._gate = None
            gate[1].set()
            assert gate[2].wait(GATE_TIMEOUT_S), "test never released the DB gate"
        with self._lock:       # record inside the lock: list order == commit order
            super().update_download(download_id, status=status, **kw)
            self.writes.append((download_id, JobStatus(status)))

    def statuses(self, job_id: int) -> list[str]:
        with self._lock:
            return [str(s) for jid, s in self.writes if jid == job_id]


class GatedListener(RecordingListener):
    """RecordingListener that can block the next ``on_job_changed`` of a given status."""

    def __init__(self) -> None:
        super().__init__()
        self._gate: tuple[JobStatus, threading.Event, threading.Event] | None = None
        self.blocked: DownloadJob | None = None

    def block_next(self, status: JobStatus) -> tuple[threading.Event, threading.Event]:
        entered, release = threading.Event(), threading.Event()
        self._gate = (status, entered, release)
        return entered, release

    def on_job_changed(self, job: DownloadJob) -> None:
        gate = self._gate
        if gate is not None and job.status == gate[0]:
            self._gate = None
            self.blocked = job
            gate[1].set()
            assert gate[2].wait(GATE_TIMEOUT_S), "test never released the listener gate"
        super().on_job_changed(job)


def collapse(seq: list[str]) -> list[str]:
    """Drop consecutive repeats (a non-persisted change, e.g. the resolved track, re-emits
    the same status)."""
    return [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]


def net_error() -> DownloadError:
    return DownloadError("ERROR: [soundcloud] x: Unable to download JSON metadata: "
                         "Read timed out.", (TransportError, TransportError("Read timed out."),
                                             None))


@pytest.fixture
def rec_storage(app_paths) -> Iterator[RecordingStorage]:
    db = RecordingStorage(app_paths.db_file)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def gated() -> GatedListener:
    return GatedListener()


@pytest.fixture
def manager_factory(rec_storage, settings_store, app_paths, gated, fake_ydl, fake_finalize,
                    fake_find_duplicate) -> Iterator[Callable[..., DownloadManager]]:
    managers: list[DownloadManager] = []

    def make(**overrides: Any) -> DownloadManager:
        kwargs: dict[str, Any] = {
            "listener": gated, "finalize": fake_finalize,
            "find_duplicate": fake_find_duplicate, "ydl_factory": fake_ydl,
            "backoff_base_s": 0.01,
            "measure_bitrate": lambda _p: settings_store.get().bitrate_kbps,
        }
        kwargs.update(overrides)
        m = DownloadManager(rec_storage, settings_store, app_paths, **kwargs)
        managers.append(m)
        return m

    yield make
    for m in managers:
        m.shutdown(wait=True, timeout=10)


def run_in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    return t, out


# --- (a) terminal visibility ---------------------------------------------------------------

def test_terminal_state_is_published_only_after_the_listener_got_it(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until):
    track = make_track("1")
    fake_ydl.add(track)
    entered, release = gated.block_next(S.COMPLETED)
    m = manager_factory()
    [job] = m.enqueue([DownloadRequest(track)])
    try:
        assert entered.wait(GATE_TIMEOUT_S)
        # The worker is inside on_job_changed(COMPLETED): the DB already has the row ...
        assert rec_storage.get_download(job.job_id).status == S.COMPLETED
        # ... but observers must not see the job as finished yet.
        assert m.get_job(job.job_id).status != S.COMPLETED
        assert all(not j.status.is_terminal for j in m.jobs())
        assert not m.is_idle()
        assert m.clear_finished() == []
        # (retry() would wait for this in-flight transition: see the race test below)
    finally:
        release.set()
    wait_until(lambda: m.get_job(job.job_id).status.is_terminal)
    # Once visible, everything agrees.
    assert m.get_job(job.job_id).status == S.COMPLETED
    assert gated.last(job.job_id).status == S.COMPLETED
    assert m.is_idle()


def test_terminal_state_is_not_visible_while_its_db_write_is_in_flight(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until):
    track = make_track("1")
    fake_ydl.add(track)
    entered, release = rec_storage.block_next(S.COMPLETED)
    m = manager_factory()
    [job] = m.enqueue([DownloadRequest(track)])
    try:
        assert entered.wait(GATE_TIMEOUT_S)
        assert m.get_job(job.job_id).status != S.COMPLETED
        assert not m.is_idle()
        assert gated.last(job.job_id).status != S.COMPLETED      # persist happens first
    finally:
        release.set()
    wait_until(m.is_idle)
    assert rec_storage.get_download(job.job_id).status == S.COMPLETED
    assert gated.last(job.job_id).status == S.COMPLETED
    assert m.get_job(job.job_id).status == S.COMPLETED


# --- (b) concurrent transitions on one job -------------------------------------------------

def test_retry_racing_with_the_final_db_write_keeps_one_order(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until):
    """retry() from another thread while the worker is still recording FAILED: the DB and the
    listener must both see ... FAILED, QUEUED ..., never QUEUED followed by a stale FAILED."""
    track = make_track("1")
    hold = threading.Event()
    script = fake_ydl.add(track, extract_errors=[DownloadError("ERROR: HTTP Error 404: Not Found")])
    entered, release = rec_storage.block_next(S.FAILED)
    m = manager_factory()
    [job] = m.enqueue([DownloadRequest(track)])
    assert entered.wait(GATE_TIMEOUT_S)
    script.hold = hold                 # the retried attempt parks in RESOLVING
    retry_thread, result = run_in_thread(lambda: m.retry(job.job_id))
    retry_thread.join(0.3)             # old code: retry() went ahead here
    release.set()
    retry_thread.join(GATE_TIMEOUT_S)
    assert result and result[0] is not None and result[0].status == S.QUEUED
    wait_until(lambda: rec_storage.statuses(job.job_id)[-1:] == ["resolving"]
               and m.get_job(job.job_id).status == S.RESOLVING)
    db = collapse(rec_storage.statuses(job.job_id))
    emitted = collapse(gated.statuses(job.job_id))
    assert db == ["resolving", "failed", "queued", "resolving"]
    assert emitted == ["queued", *db]
    hold.set()
    wait_until(m.is_idle)
    assert rec_storage.get_download(job.job_id).status == m.get_job(job.job_id).status \
        == gated.last(job.job_id).status == S.COMPLETED


def test_cancel_racing_with_the_enqueue_event_keeps_one_order(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until):
    """cancel() from another thread while enqueue() is still announcing QUEUED: the listener
    must not end on the stale QUEUED snapshot."""
    track = make_track("1")
    fake_ydl.add(track, hold=threading.Event())        # never completes on its own
    entered, release = gated.block_next(S.QUEUED)
    m = manager_factory()
    enqueue_thread, _ = run_in_thread(lambda: m.enqueue([DownloadRequest(track)]))
    assert entered.wait(GATE_TIMEOUT_S)
    job_id = gated.blocked.job_id
    cancel_thread, result = run_in_thread(lambda: m.cancel(job_id))
    cancel_thread.join(0.3)            # old code: cancel() went ahead here
    release.set()
    enqueue_thread.join(GATE_TIMEOUT_S)
    cancel_thread.join(GATE_TIMEOUT_S)
    fake_ydl.scripts[track.url].hold.set()
    assert result == [True]
    wait_until(m.is_idle)
    assert gated.statuses(job_id)[0] == "queued"
    assert gated.last(job_id).status == S.CANCELLED
    assert rec_storage.get_download(job_id).status == S.CANCELLED
    assert m.get_job(job_id).status == S.CANCELLED


def test_terminal_state_is_never_overwritten_by_a_stale_change(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until):
    track = make_track("1")
    fake_ydl.add(track)
    m = manager_factory()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(m.is_idle)
    writes, events = len(rec_storage.writes), len(gated.jobs)
    # e.g. a late stage report from a worker that lost a race
    assert m._transition(job.job_id, status=S.DOWNLOADING, fraction=0.5) is None
    assert m.get_job(job.job_id).status == S.COMPLETED
    assert rec_storage.get_download(job.job_id).status == S.COMPLETED
    assert len(rec_storage.writes) == writes and len(gated.jobs) == events


def test_db_writes_and_events_match_under_concurrent_cancel_and_retry(
        manager_factory, gated, rec_storage, fake_ydl, make_track, wait_until, settings_store,
        settings):
    """Stress: 4 workers, network retries, plus two threads randomly cancelling and retrying.
    Per job, the persisted sequence must equal the announced sequence and the final states
    must agree."""
    settings_store.update(settings.replace(concurrency=4))
    rng = random.Random(1234)
    tracks = [make_track(str(i)) for i in range(24)]
    for i, t in enumerate(tracks):
        errors = [net_error()] if i % 3 == 0 else []
        fake_ydl.add(t, extract_errors=errors, progress_steps=rng.randint(1, 5))
    m = manager_factory()
    jobs = m.enqueue([DownloadRequest(t) for t in tracks])
    ids = [j.job_id for j in jobs]
    stop = threading.Event()

    def chaos(seed: int) -> None:
        r = random.Random(seed)
        for _ in range(300):
            if stop.is_set():
                return
            jid = r.choice(ids)
            if r.random() < 0.5:
                m.cancel(jid)
            else:
                m.retry(jid)

    threads = [threading.Thread(target=chaos, args=(s,)) for s in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    stop.set()
    wait_until(m.is_idle, timeout=30)

    for jid in ids:
        db = collapse(rec_storage.statuses(jid))
        emitted = collapse(gated.statuses(jid))
        assert emitted[0] == "queued", (jid, emitted)
        assert collapse(emitted[1:]) == db, (jid, emitted, db)
        final = m.get_job(jid)
        assert final.status.is_terminal
        assert rec_storage.get_download(jid).status == final.status
        assert gated.last(jid) == final
