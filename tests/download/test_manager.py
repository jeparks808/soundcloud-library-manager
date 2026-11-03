"""DownloadManager state machine with a fake YoutubeDL (real hooks), fake metadata
functions and real SQLite."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import DownloadError

from sclm.core.errors import (
    ErrorCategory,
    FFmpegNotFoundError,
    FileSystemError,
)
from sclm.core.models import (
    ConflictPolicy,
    DownloadRequest,
    DuplicateMatch,
    DuplicateReason,
    FinalizeOutcome,
    JobStatus,
    LibrarySource,
)

S = JobStatus


def net_error() -> DownloadError:
    return DownloadError("ERROR: [soundcloud] x: Unable to download JSON metadata: "
                         "Read timed out.", (TransportError, TransportError("Read timed out."),
                                             None))


def terminal(manager, job_id):
    job = manager.get_job(job_id)
    return job is not None and job.status.is_terminal


@pytest.fixture
def run_one(make_manager, fake_ydl, wait_until):
    """Enqueue one track and wait until it is terminal. Returns (manager, job)."""
    def run(track, *, manager=None, request_kwargs=None, timeout=5.0, **script):
        if track.url not in fake_ydl.scripts:
            fake_ydl.add(track, **script)
        m = manager or make_manager()
        [job] = m.enqueue([DownloadRequest(track, **(request_kwargs or {}))])
        wait_until(lambda: terminal(m, job.job_id), timeout=timeout)
        return m, m.get_job(job.job_id)
    return run


# --- happy path --------------------------------------------------------------------------

def test_enqueue_to_completed(run_one, make_track, listener, storage, app_paths, fake_finalize,
                              fake_ydl, fake_find_duplicate, settings):
    track = make_track("1001", title="", artist="", duration_s=None, is_partial=True,
                       playlist_index=3)
    m, job = run_one(track, request_kwargs={"playlist_id": "pl1", "playlist_title": "Summer"})

    assert job.status == S.COMPLETED
    assert job.attempts == 1 and job.error is None and job.fraction == 1.0
    assert job.final_path is not None and job.final_path.is_file()
    assert job.track.title == "Track 1001" and not job.track.is_partial
    assert job.track.playlist_index == 3

    statuses = [s for i, s in enumerate(listener.statuses(job.job_id))
                if i == 0 or s != listener.statuses(job.job_id)[i - 1]]
    assert statuses == ["queued", "resolving", "downloading", "converting", "tagging",
                        "organizing", "completed"]

    # progress: downloading events, a final 100 %, then converting
    prog = [p for p in listener.progress if p.job_id == job.job_id]
    assert any(p.status == S.DOWNLOADING and p.fraction == 1.0 for p in prog)
    assert prog[-1].status == S.CONVERTING

    # metadata called with the resolved track and the job's context
    [call] = fake_finalize.calls
    assert call["track"].title == "Track 1001" and call["playlist_title"] == "Summer"
    assert call["force"] is False and call["staged"].name == "1001.mp3"
    assert fake_find_duplicate.calls == ["1001"]

    # DB: downloads row, tracks row, library_files row
    rec = storage.get_download(job.job_id)
    assert rec.status == S.COMPLETED and rec.attempts == 1
    assert rec.file_path == job.final_path and rec.bitrate_kbps == settings.bitrate_kbps
    assert rec.completed_at is not None and rec.error is None
    assert rec.playlist_id == "pl1" and rec.title == "Track 1001"
    stored = storage.get_track("1001")
    assert stored.title == "Track 1001" and stored.duration_s == pytest.approx(180.5)
    assert not stored.is_partial
    lf = storage.get_library_file(job.final_path)
    assert lf.source == LibrarySource.DOWNLOAD and lf.track_id == "1001"
    assert lf.audio_sha256 == "ab" * 32 and lf.size_bytes == job.final_path.stat().st_size
    assert storage.existing_track_paths(["1001"]) == {"1001": job.final_path}

    # staging cleaned
    assert not app_paths.job_staging_dir(job.job_id).exists()
    assert m.is_idle()


def test_placeholder_title_never_reaches_finalize(run_one, make_track, fake_ydl, fake_finalize,
                                                  storage, sc):
    """A preview entry that couldn't be looked up is queued bare (or with a "Track <id>"
    placeholder); the resolved full info overrides title/artist/duration before tagging and
    naming."""
    track = make_track("1001", title="Track 1001", artist="", duration_s=None,
                       is_partial=True, album="The EP")
    fake_ydl.add(track, info=sc.track("1001", webpage_url=track.url, original_url=track.url,
                                      title="Real Song", track="Real Song",
                                      uploader="Real Artist", duration=211.0))
    _, job = run_one(track)
    assert job.status == S.COMPLETED
    [call] = fake_finalize.calls
    assert call["track"].title == "Real Song" and call["track"].artist == "Real Artist"
    assert call["track"].duration_s == pytest.approx(211.0)
    assert call["track"].album == "The EP"
    assert job.final_path.name == "Real Artist - Real Song [1001].mp3"
    assert storage.get_track("1001").title == "Real Song"


def test_download_options(run_one, make_track, fake_ydl, settings_store, settings, ffmpeg_exe,
                          app_paths):
    settings_store.update(settings.replace(bitrate_kbps=192))
    m, job = run_one(make_track("1"))
    assert job.status == S.COMPLETED
    [opts] = fake_ydl.opts
    assert opts["format"] == "bestaudio/best" and opts["noplaylist"] is True
    assert opts["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": "192"}]
    assert opts["ffmpeg_location"] == str(ffmpeg_exe)
    assert opts["outtmpl"]["default"] == str(app_paths.job_staging_dir(job.job_id)
                                             / "%(id)s.%(ext)s")
    assert opts["socket_timeout"] == 20 and opts["retries"] == 3
    assert "enable_file_urls" not in opts and "cookiefile" not in opts
    assert storage_bitrate(m, job) == 192


def storage_bitrate(m, job):
    return m._storage.get_download(job.job_id).bitrate_kbps


def test_finalize_warnings_in_message(run_one, make_track, fake_finalize):
    fake_finalize.warnings = ("Artwork could not be downloaded",)
    _, job = run_one(make_track("1"))
    assert job.status == S.COMPLETED
    assert "Artwork could not be downloaded" in job.message


# --- dedupe ---------------------------------------------------------------------------------

def test_enqueue_ignores_tracks_with_active_jobs(make_manager, fake_ydl, make_track,
                                                 wait_until, storage):
    gate = threading.Event()
    t1, t2 = make_track("1"), make_track("2")
    fake_ydl.add(t1, gate=gate)
    fake_ydl.add(t2)
    m = make_manager()
    created = m.enqueue([DownloadRequest(t1), DownloadRequest(t1), DownloadRequest(t2)])
    assert [j.track.track_id for j in created] == ["1", "2"]
    assert m.enqueue([DownloadRequest(t1)]) == []
    gate.set()
    wait_until(m.is_idle)
    again = m.enqueue([DownloadRequest(t1)])     # terminal -> may be enqueued again
    assert len(again) == 1 and again[0].job_id not in {j.job_id for j in created}
    wait_until(m.is_idle)
    assert len(storage.download_history()) == 3


# --- failures ------------------------------------------------------------------------------

@pytest.mark.parametrize("exc,category", [
    (DownloadError("ERROR: [soundcloud] x: This track is private"), ErrorCategory.PRIVATE),
    (DownloadError("ERROR: [soundcloud] x: Unable to download JSON metadata: HTTP Error 404: "
                   "Not Found"), ErrorCategory.UNAVAILABLE),
    (DownloadError("ERROR: This video is not available from your location due to geo "
                   "restriction"), ErrorCategory.GEO_BLOCKED),
    (DownloadError("ERROR: [soundcloud] x: This video is only available for registered users"),
     ErrorCategory.AUTH_REQUIRED),
    (DownloadError("ERROR: [soundcloud] 109665566: This video is DRM protected"),
     ErrorCategory.DRM_PROTECTED),
])
def test_access_failures_are_not_retried(run_one, make_track, fake_ydl, exc, category, storage,
                                         app_paths):
    track = make_track("1")
    m, job = run_one(track, extract_errors=[exc, exc, exc])
    assert job.status == S.FAILED and job.error.category == category
    assert job.attempts == 1 and fake_ydl.extract_calls == [track.url]
    assert job.message == job.error.user_message
    rec = storage.get_download(job.job_id)
    assert rec.status == S.FAILED and rec.error.category == category and rec.attempts == 1
    assert rec.error.detail
    assert not app_paths.job_staging_dir(job.job_id).exists()


def test_preview_only_track_fails_auth_without_download(run_one, make_track, fake_ydl, sc,
                                                        fake_finalize):
    track = make_track("2001")
    info = sc.preview_only("2001")
    info.update(webpage_url=track.url, original_url=track.url)
    _, job = run_one(track, info=info)
    assert job.status == S.FAILED and job.error.category == ErrorCategory.AUTH_REQUIRED
    assert "preview" in job.error.user_message.lower()
    assert fake_ydl.download_calls == [] and fake_finalize.calls == []


def test_ffmpeg_failure(run_one, make_track):
    from yt_dlp.utils import PostProcessingError
    err = DownloadError("ERROR: Postprocessing: audio conversion failed: Invalid data",
                        (PostProcessingError, PostProcessingError("audio conversion failed"), None))
    _, job = run_one(make_track("1"), download_errors=[err])
    assert job.status == S.FAILED and job.error.category == ErrorCategory.FFMPEG


def test_ffmpeg_not_found(make_manager, run_one, make_track, fake_ydl):
    def missing(_configured):
        raise FFmpegNotFoundError()
    m = make_manager(ffmpeg_resolver=missing)
    _, job = run_one(make_track("1"), manager=m)
    assert job.status == S.FAILED and job.error.category == ErrorCategory.FFMPEG
    assert fake_ydl.extract_calls == []


def test_no_mp3_produced(run_one, make_track):
    _, job = run_one(make_track("1"), produce_mp3=False)
    assert job.status == S.FAILED and job.error.category == ErrorCategory.FFMPEG


def test_finalize_error_fails_and_cleans_staging(run_one, make_track, fake_finalize, app_paths,
                                                  storage):
    fake_finalize.error = FileSystemError("Could not write tags.")
    _, job = run_one(make_track("1"))
    assert job.status == S.FAILED and job.error.category == ErrorCategory.FILESYSTEM
    assert not app_paths.job_staging_dir(job.job_id).exists()
    assert storage.count_library_files() == 0


def test_unknown_error(run_one, make_track, fake_finalize):
    fake_finalize.error = RuntimeError("bug")
    _, job = run_one(make_track("1"))
    assert job.status == S.FAILED and job.error.category == ErrorCategory.UNKNOWN
    assert "RuntimeError" in job.error.detail


# --- network retry -------------------------------------------------------------------------

def test_network_retry_then_success(run_one, make_track, listener, storage, fake_ydl):
    track = make_track("1")
    _, job = run_one(track, extract_errors=[net_error(), net_error()])
    assert job.status == S.COMPLETED and job.attempts == 3 and job.error is None
    assert len(fake_ydl.extract_calls) == 3
    waits = [j for j in listener.jobs if j.job_id == job.job_id and j.status == S.WAITING_RETRY]
    assert [w.message for w in waits] == ["Retrying in 0.05 s (attempt 2 of 3)",
                                          "Retrying in 0.1 s (attempt 3 of 3)"]
    assert all(w.error.category == ErrorCategory.NETWORK for w in waits)
    rec = storage.get_download(job.job_id)
    assert rec.attempts == 3 and rec.status == S.COMPLETED and rec.error is None


def test_network_error_during_download_retries(run_one, make_track, fake_ydl):
    _, job = run_one(make_track("1"), download_errors=[net_error()])
    assert job.status == S.COMPLETED and job.attempts == 2
    assert len(fake_ydl.download_calls) == 2


def test_network_retries_exhausted(run_one, make_track, storage):
    _, job = run_one(make_track("1"), extract_errors=[net_error() for _ in range(5)])
    assert job.status == S.FAILED and job.error.category == ErrorCategory.NETWORK
    assert job.attempts == 3
    assert storage.get_download(job.job_id).attempts == 3


# --- cancellation --------------------------------------------------------------------------

def test_cancel_while_queued(make_manager, fake_ydl, make_track, wait_until, storage,
                             settings_store, settings, listener):
    settings_store.update(settings.replace(concurrency=1))
    gate = threading.Event()
    t1, t2 = make_track("1"), make_track("2")
    fake_ydl.add(t1, gate=gate)
    fake_ydl.add(t2)
    m = make_manager()
    j1, j2 = m.enqueue([DownloadRequest(t1), DownloadRequest(t2)])
    wait_until(lambda: m.get_job(j1.job_id).status == S.DOWNLOADING)
    assert m.get_job(j2.job_id).status == S.QUEUED
    assert m.cancel(j2.job_id) is True
    assert m.get_job(j2.job_id).status == S.CANCELLED          # immediately
    assert storage.get_download(j2.job_id).status == S.CANCELLED
    assert listener.last(j2.job_id).status == S.CANCELLED
    assert m.cancel(j2.job_id) is False                       # already terminal
    gate.set()
    wait_until(m.is_idle)
    assert m.get_job(j1.job_id).status == S.COMPLETED
    assert t2.url not in fake_ydl.extract_calls


@pytest.mark.parametrize("wrap", [False, True], ids=["raw", "wrapped-by-ytdlp"])
def test_cancel_while_downloading(make_manager, fake_ydl, make_track, wait_until, storage,
                                  app_paths, fake_finalize, wrap):
    track = make_track("1")
    fake_ydl.add(track, gate=threading.Event(), wrap_hook_errors=wrap)
    m = make_manager()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: m.get_job(job.job_id).status == S.DOWNLOADING)
    staging = app_paths.job_staging_dir(job.job_id)
    assert staging.is_dir()
    assert m.cancel(job.job_id) is True
    wait_until(lambda: terminal(m, job.job_id))
    final = m.get_job(job.job_id)
    assert final.status == S.CANCELLED and final.error is None
    assert storage.get_download(job.job_id).status == S.CANCELLED
    assert not staging.exists()
    assert fake_finalize.calls == []


def test_cancel_during_backoff(make_manager, fake_ydl, make_track, wait_until, storage):
    track = make_track("1")
    fake_ydl.add(track, extract_errors=[net_error()])
    m = make_manager(backoff_base_s=30)
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: m.get_job(job.job_id).status == S.WAITING_RETRY)
    t0 = time.monotonic()
    m.cancel(job.job_id)
    wait_until(lambda: terminal(m, job.job_id), timeout=2)
    assert time.monotonic() - t0 < 2
    assert m.get_job(job.job_id).status == S.CANCELLED
    assert storage.get_download(job.job_id).status == S.CANCELLED
    assert len(fake_ydl.extract_calls) == 1


def test_cancel_during_resolve(make_manager, fake_ydl, make_track, wait_until):
    track = make_track("1")
    hold = threading.Event()
    fake_ydl.add(track, hold=hold)
    m = make_manager()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: m.get_job(job.job_id).status == S.RESOLVING)
    m.cancel(job.job_id)
    hold.set()
    wait_until(lambda: terminal(m, job.job_id))
    assert m.get_job(job.job_id).status == S.CANCELLED
    assert fake_ydl.download_calls == []


def test_cancel_unknown_and_cancel_all(make_manager, fake_ydl, make_track, wait_until):
    m = make_manager()
    assert m.cancel(12345) is False
    gate = threading.Event()
    tracks = [make_track(str(i)) for i in range(3)]
    for t in tracks:
        fake_ydl.add(t, gate=gate)
    m.enqueue([DownloadRequest(t) for t in tracks])
    assert m.cancel_all() == 3
    wait_until(m.is_idle)
    assert {j.status for j in m.jobs()} == {S.CANCELLED}


# --- retry / force -------------------------------------------------------------------------

def test_manual_retry_same_row(run_one, make_track, fake_ydl, storage, wait_until, listener):
    track = make_track("1")
    m, job = run_one(track, extract_errors=[DownloadError("ERROR: HTTP Error 404: Not Found")])
    assert job.status == S.FAILED
    assert m.retry(job.job_id + 999) is None
    again = m.retry(job.job_id)
    assert again is not None and again.job_id == job.job_id and again.status == S.QUEUED
    assert again.error is None
    assert m.retry(job.job_id) is None or terminal(m, job.job_id)   # non-terminal -> None
    wait_until(lambda: terminal(m, job.job_id))
    final = m.get_job(job.job_id)
    assert final.status == S.COMPLETED and final.attempts == 2
    assert len(storage.download_history()) == 1
    rec = storage.get_download(job.job_id)
    assert rec.status == S.COMPLETED and rec.attempts == 2 and rec.error is None


def test_retry_rejected_for_running_job(make_manager, fake_ydl, make_track, wait_until):
    gate = threading.Event()
    track = make_track("1")
    fake_ydl.add(track, gate=gate)
    m = make_manager()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: m.get_job(job.job_id).status == S.DOWNLOADING)
    assert m.retry(job.job_id) is None
    gate.set()


def test_duplicate_precheck_skips_then_keep_both(run_one, make_track, fake_find_duplicate,
                                                 fake_ydl, fake_finalize, storage, library_dir,
                                                 wait_until):
    existing = library_dir / "existing.mp3"
    existing.write_bytes(b"x")
    fake_find_duplicate.matches["1"] = DuplicateMatch(DuplicateReason.TRACK_ID, existing, "1")
    m, job = run_one(make_track("1"))
    assert job.status == S.SKIPPED_DUPLICATE and job.duplicate_of == existing
    assert "existing.mp3" in job.message
    assert fake_ydl.download_calls == [] and fake_finalize.calls == []
    rec = storage.get_download(job.job_id)
    assert rec.status == S.SKIPPED_DUPLICATE and rec.file_path == existing

    forced = m.retry(job.job_id, force=True)
    assert forced.force is True and forced.duplicate_of is None
    wait_until(lambda: terminal(m, job.job_id))
    final = m.get_job(job.job_id)
    assert final.status == S.COMPLETED and final.final_path != existing
    assert fake_find_duplicate.calls == ["1"]           # not consulted when forced
    assert fake_finalize.calls[-1]["force"] is True
    assert existing.read_bytes() == b"x"


def test_retry_clears_the_stored_file_path(run_one, make_track, fake_find_duplicate, fake_ydl,
                                           storage, library_dir, wait_until):
    """Issue #2: a SKIPPED_DUPLICATE row stores the duplicate's path; once the job is retried
    (and here fails), the row must not keep pointing at that file."""
    existing = library_dir / "existing.mp3"
    existing.write_bytes(b"x")
    fake_find_duplicate.matches["1"] = DuplicateMatch(DuplicateReason.ARTIST_TITLE, existing,
                                                      None)
    track = make_track("1")
    m, job = run_one(track)
    assert storage.get_download(job.job_id).file_path == existing
    fake_ydl.scripts[track.url].extract_errors.append(
        DownloadError("ERROR: [soundcloud] x: HTTP Error 404: Not Found"))
    assert m.retry(job.job_id, force=True) is not None
    wait_until(lambda: terminal(m, job.job_id))
    assert m.get_job(job.job_id).status == S.FAILED
    rec = storage.get_download(job.job_id)
    assert rec.status == S.FAILED and rec.file_path is None


def test_force_request_skips_precheck(run_one, make_track, fake_find_duplicate, library_dir):
    fake_find_duplicate.matches["1"] = DuplicateMatch(DuplicateReason.TRACK_ID,
                                                      library_dir / "x.mp3", "1")
    _, job = run_one(make_track("1"), request_kwargs={"force": True})
    assert job.status == S.COMPLETED and fake_find_duplicate.calls == []


@pytest.mark.parametrize("policy,reason,skipped", [
    (ConflictPolicy.SKIP, DuplicateReason.ARTIST_TITLE, True),
    (ConflictPolicy.ASK, DuplicateReason.ARTIST_TITLE, True),
    (ConflictPolicy.KEEP_BOTH, DuplicateReason.ARTIST_TITLE, False),
    (ConflictPolicy.KEEP_BOTH, DuplicateReason.TRACK_ID, True),
])
def test_duplicate_policy_table(run_one, make_track, fake_find_duplicate, settings_store,
                                settings, library_dir, policy, reason, skipped):
    settings_store.update(settings.replace(conflict_policy=policy))
    fake_find_duplicate.matches["1"] = DuplicateMatch(reason, library_dir / "x.mp3", None)
    _, job = run_one(make_track("1"))
    assert job.status == (S.SKIPPED_DUPLICATE if skipped else S.COMPLETED)


def test_finalize_duplicate_outcome(run_one, make_track, fake_finalize, library_dir, storage,
                                    app_paths):
    other = library_dir / "same-audio.mp3"
    other.write_bytes(b"y")
    fake_finalize.outcome = FinalizeOutcome.DUPLICATE
    fake_finalize.duplicate = DuplicateMatch(DuplicateReason.CONTENT_HASH, other, None)
    _, job = run_one(make_track("1"))
    assert job.status == S.SKIPPED_DUPLICATE and job.duplicate_of == other
    assert "identical audio" in job.message
    assert storage.get_download(job.job_id).file_path == other
    assert storage.count_library_files() == 0
    assert not app_paths.job_staging_dir(job.job_id).exists()  # staged file discarded


# --- concurrency ----------------------------------------------------------------------------

def test_concurrency_limit_and_set_concurrency(make_manager, fake_ydl, make_track, wait_until):
    gate = threading.Event()
    tracks = [make_track(str(i)) for i in range(6)]
    for t in tracks:
        fake_ydl.add(t, gate=gate)
    m = make_manager()                      # settings.concurrency == 2
    m.enqueue([DownloadRequest(t) for t in tracks])
    wait_until(lambda: fake_ydl.active == 2)
    time.sleep(0.1)
    assert fake_ydl.active == 2 and fake_ydl.max_active == 2
    assert sum(1 for j in m.jobs() if j.status == S.QUEUED) == 4
    m.set_concurrency(3)
    wait_until(lambda: fake_ydl.active == 3)
    time.sleep(0.1)
    assert fake_ydl.max_active == 3
    m.set_concurrency(99)                   # clamped to 4
    wait_until(lambda: fake_ydl.active == 4)
    gate.set()
    wait_until(m.is_idle)
    assert fake_ydl.max_active == 4
    assert {j.status for j in m.jobs()} == {S.COMPLETED}


def test_settings_change_updates_concurrency(make_manager, fake_ydl, make_track, wait_until,
                                             settings_store, settings):
    gate = threading.Event()
    tracks = [make_track(str(i)) for i in range(4)]
    for t in tracks:
        fake_ydl.add(t, gate=gate)
    m = make_manager()
    settings_store.update(settings.replace(concurrency=1))
    m.enqueue([DownloadRequest(t) for t in tracks])
    wait_until(lambda: fake_ydl.active == 1)
    time.sleep(0.1)
    assert fake_ydl.max_active == 1
    settings_store.update(settings.replace(concurrency=4))
    wait_until(lambda: fake_ydl.active == 4)
    gate.set()
    wait_until(m.is_idle)


# --- shutdown / housekeeping ----------------------------------------------------------------

def test_shutdown_cancels_and_joins(make_manager, fake_ydl, make_track, wait_until, storage,
                                    settings_store, settings, app_paths):
    settings_store.update(settings.replace(concurrency=1))
    t1, t2 = make_track("1"), make_track("2")
    fake_ydl.add(t1, gate=threading.Event())
    fake_ydl.add(t2)
    before = set(threading.enumerate())
    m = make_manager()
    j1, j2 = m.enqueue([DownloadRequest(t1), DownloadRequest(t2)])
    wait_until(lambda: m.get_job(j1.job_id).status == S.DOWNLOADING)
    m.shutdown(wait=True, timeout=5)
    assert fake_ydl.active == 0
    assert m.get_job(j1.job_id).status == S.CANCELLED
    assert m.get_job(j2.job_id).status == S.CANCELLED
    assert storage.get_download(j1.job_id).status == S.CANCELLED
    assert storage.get_download(j2.job_id).status == S.CANCELLED
    assert list(app_paths.staging_dir.iterdir()) == []
    assert m.is_idle()
    assert m.enqueue([DownloadRequest(make_track("3"))]) == []
    assert m.retry(j1.job_id) is None
    m.shutdown(wait=True, timeout=1)        # idempotent
    assert not any(t.name.startswith("sclm-dl") and t.is_alive()
                   for t in set(threading.enumerate()) - before)


def test_stale_staging_dirs_removed_at_startup(make_manager, app_paths):
    stale = app_paths.job_staging_dir(99)
    stale.mkdir(parents=True)
    (stale / "half.m4a.part").write_bytes(b"x")
    other = app_paths.staging_dir / "keep-me"
    other.mkdir()
    make_manager()
    assert not stale.exists() and other.exists()


def test_clear_finished_and_jobs(make_manager, fake_ydl, make_track, wait_until, storage):
    gate = threading.Event()
    t1, t2 = make_track("1"), make_track("2")
    fake_ydl.add(t1)
    fake_ydl.add(t2, gate=gate)
    m = make_manager()
    j1, j2 = m.enqueue([DownloadRequest(t1), DownloadRequest(t2)])
    assert [j.job_id for j in m.jobs()] == [j1.job_id, j2.job_id]
    wait_until(lambda: terminal(m, j1.job_id))
    assert not m.is_idle()
    assert m.clear_finished() == [j1.job_id]
    assert m.get_job(j1.job_id) is None and [j.job_id for j in m.jobs()] == [j2.job_id]
    assert storage.get_download(j1.job_id).status == S.COMPLETED   # history kept
    gate.set()
    wait_until(m.is_idle)


def test_listener_exceptions_are_swallowed(make_manager, run_one, make_track):
    class Broken:
        def on_job_changed(self, job):
            raise RuntimeError("listener bug")

        def on_progress(self, event):
            raise RuntimeError("listener bug")

    m = make_manager(listener=Broken())
    _, job = run_one(make_track("1"), manager=m)
    assert job.status == S.COMPLETED


def test_set_listener(make_manager, run_one, make_track, listener):
    from sclm.core.events import RecordingListener
    m = make_manager()
    other = RecordingListener()
    m.set_listener(other)
    _, job = run_one(make_track("1"), manager=m)
    assert other.last(job.job_id).status == S.COMPLETED
    assert listener.last(job.job_id) is None
    m.set_listener(None)        # NullListener, no crash
    run_one(make_track("2"), manager=m)


def test_progress_is_throttled(run_one, make_track, listener):
    _, job = run_one(make_track("1"), progress_steps=200)
    dl = [p for p in listener.progress if p.job_id == job.job_id and p.status == S.DOWNLOADING]
    assert dl[-1].fraction == 1.0
    assert 2 <= len(dl) <= 6      # 200 hook calls within a few ms -> first + final (+ slack)
    assert all(p.total_bytes == 3000 for p in dl)


def test_cookies_copied_into_staging(run_one, make_track, fake_ydl, settings_store, settings,
                                     tmp_path, app_paths):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    settings_store.update(settings.replace(cookies_file=str(cookies)))
    _, job = run_one(make_track("1"))
    used = Path(fake_ydl.opts[0]["cookiefile"])
    assert used.parent == app_paths.job_staging_dir(job.job_id) and not used.exists()
    assert cookies.read_text() == "# Netscape HTTP Cookie File\n"


def test_missing_cookies_file_fails_config(run_one, make_track, settings_store, settings,
                                           tmp_path):
    settings_store.update(settings.replace(cookies_file=str(tmp_path / "gone.txt")))
    _, job = run_one(make_track("1"))
    assert job.status == S.FAILED and job.error.category == ErrorCategory.CONFIG


def test_history_rows_for_mixed_outcomes(make_manager, fake_ydl, make_track, wait_until,
                                         storage, fake_find_duplicate, library_dir):
    ok, bad, dup = make_track("1"), make_track("2"), make_track("3")
    fake_ydl.add(ok)
    fake_ydl.add(bad, extract_errors=[DownloadError("ERROR: This track is private")])
    fake_ydl.add(dup)
    fake_find_duplicate.matches["3"] = DuplicateMatch(DuplicateReason.TRACK_ID,
                                                      library_dir / "d.mp3", "3")
    m = make_manager()
    m.enqueue([DownloadRequest(t) for t in (ok, bad, dup)])
    wait_until(m.is_idle)
    hist = {r.track_id: r for r in storage.download_history()}
    assert hist["1"].status == S.COMPLETED and hist["1"].file_path is not None
    assert hist["2"].status == S.FAILED and hist["2"].error.category == ErrorCategory.PRIVATE
    assert hist["3"].status == S.SKIPPED_DUPLICATE and hist["3"].file_path == library_dir / "d.mp3"
    assert storage.download_history(statuses=[S.FAILED])[0].track_id == "2"
