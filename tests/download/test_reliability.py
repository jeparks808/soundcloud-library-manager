"""Stage 5 reliability scenarios: crash recovery, network drops mid-download, playlist changes
between preview and download, and DB consistency under concurrent use.

(FFmpeg failures through real yt-dlp: ``test_ffmpeg_failures.py``. Ordering races between
threads: ``test_manager_ordering.py``. Qt shutdown: ``tests/gui/test_shutdown.py``.)
"""

from __future__ import annotations

import io
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ContentTooShortError, DownloadError, ExtractorError

from sclm.app import build_core
from sclm.core.errors import ErrorCategory
from sclm.core.events import RecordingListener
from sclm.core.models import DownloadRequest, JobStatus, LibrarySource, PlaylistInfo
from sclm.core.paths import AppPaths
from sclm.core.storage import INTERRUPTED_MESSAGE
from sclm.download.manager import DownloadManager
from sclm.metadata.scanner import scan_library

S = JobStatus


def terminal(manager, job_id):
    job = manager.get_job(job_id)
    return job is not None and job.status.is_terminal


def http_error(status: int, reason: str) -> HTTPError:
    return HTTPError(Response(io.BytesIO(b""), "https://api-v2.soundcloud.com/tracks/1",
                              {}, status=status, reason=reason))


def extractor_error(inner: BaseException, msg: str) -> DownloadError:
    """Shaped like yt-dlp: ExtractorError(cause=inner) wrapped by ``YoutubeDL.trouble``."""
    try:
        try:
            raise inner
        except BaseException:
            raise ExtractorError(msg, cause=inner, expected=True, ie="soundcloud",
                                 video_id="artist/track") from None
    except ExtractorError:
        return DownloadError(f"ERROR: [soundcloud] artist/track: {msg}", sys.exc_info())


def connection_dropped() -> DownloadError:
    """yt-dlp's HTTP downloader when the connection dies part-way through the file."""
    try:
        raise ContentTooShortError(1500, 3000)
    except ContentTooShortError:
        return DownloadError("ERROR: content too short (expected 3000 bytes and served 1500)",
                             sys.exc_info())


def connection_reset() -> DownloadError:
    try:
        raise TransportError("[Errno 104] Connection reset by peer")
    except TransportError:
        return DownloadError("ERROR: Unable to download: [Errno 104] Connection reset by peer",
                             sys.exc_info())


# --- 1. crash recovery ----------------------------------------------------------------------

CRASH_CHILD = textwrap.dedent('''
    import sys, threading, time
    from pathlib import Path
    from sclm.core.ffmpeg import FfmpegInfo, FfmpegSource
    from sclm.core.models import DownloadRequest, TrackInfo
    from sclm.core.paths import AppPaths
    from sclm.core.settings import Settings, SettingsStore
    from sclm.core.storage import Storage
    from sclm.download.manager import DownloadManager

    root, library, ready = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

    class Ydl:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def extract_info(self, url, download=False, **_):
            tid = url.rsplit("-", 1)[-1]
            return {"id": tid, "title": "Track " + tid, "uploader": "A", "duration": 60.0,
                    "webpage_url": url, "extractor_key": "Soundcloud",
                    "formats": [{"format_id": "http_mp3", "url": "https://x/y.mp3",
                                 "acodec": "mp3"}]}
        def process_ie_result(self, info, download=True):
            staging = Path(self.opts["outtmpl"]["default"]).parent
            (staging / (info["id"] + ".m4a.part")).write_bytes(b"half a download")
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000})
            ready.write_text("ready")
            threading.Event().wait()           # the "network" stalls until we are killed

    paths = AppPaths.under(root).ensure()
    store = SettingsStore(paths.settings_file,
                          initial=Settings(library_dir=str(library), concurrency=1))
    manager = DownloadManager(
        Storage(paths.db_file), store, paths, finalize=lambda *a, **k: None,
        find_duplicate=lambda s, t: None, ydl_factory=Ydl,
        ffmpeg_resolver=lambda _p: FfmpegInfo(Path("/bin/true"), FfmpegSource.PATH))
    manager.enqueue([DownloadRequest(TrackInfo(track_id=t, title="T" + t,
                                               url="https://soundcloud.com/a/track-" + t),
                                     playlist_id="pl") for t in ("1", "2")])
    time.sleep(3600)
''')


def test_crash_mid_download_is_reconciled_on_next_start(tmp_path, library_dir, fake_ydl,
                                                        fake_finalize, fake_find_duplicate,
                                                        make_track, wait_until):
    """The process is SIGKILLed mid-download. On the next start the left-over rows become
    FAILED ("Interrupted"), the half-written staging dir is removed, nothing leaked into the
    library, the tracks count as new again (the preview offers them) and re-downloading works.
    """
    root, ready = tmp_path / "app", tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", CRASH_CHILD, str(root), str(library_dir),
                              str(ready)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert child.poll() is None, child.stdout.read().decode()
            assert time.monotonic() < deadline, "child never reached the download"
            time.sleep(0.02)
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)

    paths = AppPaths.under(root)
    with sqlite3.connect(paths.db_file) as raw:            # what the crash left behind
        left = dict(raw.execute("SELECT track_id, status FROM downloads").fetchall())
    assert left == {"1": "downloading", "2": "queued"}
    staging = paths.job_staging_dir(1)
    assert (staging / "1.m4a.part").is_file()

    core = build_core(paths)                                 # next start
    try:
        assert core.storage._query("PRAGMA integrity_check")[0][0] == "ok"
        history = {r.track_id: r for r in core.storage.download_history()}
        for tid in ("1", "2"):
            assert history[tid].status == S.FAILED
            assert history[tid].error.user_message == INTERRUPTED_MESSAGE
            assert history[tid].file_path is None
        assert core.storage.existing_track_paths(["1", "2"]) == {}     # "new" again

        manager = DownloadManager(core.storage, core.settings, paths,
                                  finalize=fake_finalize, find_duplicate=fake_find_duplicate,
                                  ydl_factory=fake_ydl, measure_bitrate=lambda _p: 128)
        try:
            assert not staging.exists()                      # stale staging cleaned
            assert list(library_dir.rglob("*")) == []       # nothing partial in the library
            track = make_track("1", url="https://soundcloud.com/a/track-1")
            fake_ydl.add(track)
            [job] = manager.enqueue([DownloadRequest(track)])
            wait_until(lambda: terminal(manager, job.job_id))
            done = manager.get_job(job.job_id)
            assert done.status == S.COMPLETED
            assert core.storage.existing_track_paths(["1"]) == {"1": done.final_path}
            assert len(core.storage.download_history()) == 3   # the interrupted rows stay
        finally:
            manager.shutdown(wait=True, timeout=10)
    finally:
        core.storage.close()


# --- 3. network interruptions ----------------------------------------------------------------

class StagingSpy(RecordingListener):
    """Records what the job's staging dir contains whenever a status is announced."""

    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self.seen: list[tuple[str, list[str] | None]] = []

    def on_job_changed(self, job) -> None:
        staging = self.paths.job_staging_dir(job.job_id)
        contents = sorted(p.name for p in staging.iterdir()) if staging.is_dir() else None
        self.seen.append((str(job.status), contents))
        super().on_job_changed(job)


def test_connection_drop_mid_download_retries_in_a_fresh_staging_dir(
        make_manager, fake_ydl, make_track, app_paths, library_dir, storage, wait_until):
    track = make_track("1")
    fake_ydl.add(track, download_errors=[connection_dropped(), connection_reset()],
                 progress_steps=3)
    spy = StagingSpy(app_paths)
    m = make_manager(listener=spy)
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: terminal(m, job.job_id))

    done = m.get_job(job.job_id)
    assert done.status == S.COMPLETED and done.attempts == 3 and done.error is None
    assert len(fake_ydl.download_calls) == 3
    waits = [j for j in spy.jobs if j.status == S.WAITING_RETRY]
    assert [w.error.category for w in waits] == [ErrorCategory.NETWORK] * 2
    assert "content too short" in waits[0].error.detail
    assert "Connection reset" in waits[1].error.detail
    # the dropped attempt's .part file is gone before the retry and before each new download
    assert [c for s, c in spy.seen if s == "waiting_retry"] == [None, None]
    assert [c for s, c in spy.seen if s == "downloading"] == [[], [], []]
    assert not app_paths.job_staging_dir(job.job_id).exists()
    assert sorted(p.name for p in library_dir.rglob("*")) == [done.final_path.name]
    rec = storage.get_download(job.job_id)
    assert rec.status == S.COMPLETED and rec.attempts == 3 and rec.file_path == done.final_path


def test_connection_that_keeps_dropping_fails_without_leaks(
        make_manager, fake_ydl, make_track, app_paths, library_dir, storage, fake_finalize,
        wait_until):
    track = make_track("1")
    fake_ydl.add(track, download_errors=[connection_dropped() for _ in range(3)])
    m = make_manager()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: terminal(m, job.job_id))
    done = m.get_job(job.job_id)
    assert done.status == S.FAILED and done.error.category == ErrorCategory.NETWORK
    assert done.attempts == 3 and fake_finalize.calls == []
    assert not app_paths.job_staging_dir(job.job_id).exists()
    assert list(library_dir.rglob("*")) == []
    rec = storage.get_download(job.job_id)
    assert rec.status == S.FAILED and rec.attempts == 3 and rec.file_path is None


# --- 5. playlist changed between preview and download -----------------------------------------

def test_tracks_removed_or_restricted_after_preview_fail_cleanly(
        make_manager, fake_ydl, make_track, sc, storage, app_paths, library_dir, fake_finalize,
        wait_until):
    """The user previewed a set; before the download starts one track is deleted (404), one
    is made private (403), one moved behind Go+ (preview formats only). Those jobs fail with
    the matching category and are not retried; the rest of the playlist still downloads."""
    ok1, removed, private, gplus, ok2 = (make_track(str(i)) for i in range(1, 6))
    fake_ydl.add(ok1)
    fake_ydl.add(removed, extract_errors=[extractor_error(
        http_error(404, "Not Found"), "Unable to download JSON metadata: HTTP Error 404: "
                                      "Not Found")])
    fake_ydl.add(private, extract_errors=[extractor_error(
        http_error(403, "Forbidden"), "Unable to download JSON metadata: HTTP Error 403: "
                                      "Forbidden")])
    info = sc.preview_only("4")
    info.update(webpage_url=gplus.url, original_url=gplus.url)
    fake_ydl.add(gplus, info=info)
    fake_ydl.add(ok2)
    storage.sync_playlist(PlaylistInfo("pl", "https://soundcloud.com/a/sets/mix", "Mix",
                                       entries=(ok1, removed, private, gplus, ok2)))
    m = make_manager()
    jobs = m.enqueue([DownloadRequest(t, playlist_id="pl", playlist_title="Mix")
                      for t in (ok1, removed, private, gplus, ok2)])
    wait_until(m.is_idle)

    final = {m.get_job(j.job_id).track.track_id: m.get_job(j.job_id) for j in jobs}
    assert final["1"].status == final["5"].status == S.COMPLETED
    expected = {"2": ErrorCategory.UNAVAILABLE, "3": ErrorCategory.PRIVATE,
                "4": ErrorCategory.AUTH_REQUIRED}
    for tid, category in expected.items():
        job = final[tid]
        assert job.status == S.FAILED and job.error.category == category, job
        assert job.attempts == 1
        rec = storage.get_download(job.job_id)
        assert rec.status == S.FAILED and rec.error.category == category
        assert rec.file_path is None
    assert sorted(fake_ydl.extract_calls) == sorted(t.url for t in (ok1, removed, private,
                                                                     gplus, ok2))
    assert sorted(fake_ydl.download_calls) == sorted([ok1.url, ok2.url])
    assert len(fake_finalize.calls) == 2
    assert len(list(library_dir.rglob("*.mp3"))) == 2
    assert list(app_paths.staging_dir.iterdir()) == []


# --- 6. DB consistency under concurrency ------------------------------------------------------

N_TRACKS, N_OLD = 16, 3


@pytest.fixture(scope="module")
def distinct_mp3s(tmp_path_factory: pytest.TempPathFactory, ffmpeg_exe: Path) -> list[bytes]:
    """Short, valid MP3s with different audio (so no content-hash duplicates), made by one
    ffmpeg run."""
    out = tmp_path_factory.mktemp("distinct-mp3")
    n = N_TRACKS + N_OLD
    cmd = [str(ffmpeg_exe), "-hide_banner", "-loglevel", "error"]
    for i in range(n):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={300 + 40 * i}:duration=0.3"]
    for i in range(n):
        cmd += ["-map", str(i), "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
                str(out / f"{i}.mp3")]
    subprocess.run(cmd, check=True, timeout=60)
    return [(out / f"{i}.mp3").read_bytes() for i in range(n)]


def test_db_stays_consistent_with_workers_gui_reads_previews_and_scans_at_once(
        storage, settings_store, settings, app_paths, library_dir, fake_ydl, make_track,
        distinct_mp3s, wait_until):
    """4 workers (real finalize_track: tagging, hashing, organizing, DB writes), a "GUI"
    thread doing paged reads, a "preview" thread re-syncing the playlist with partial records,
    and scan_library looping over the same library, all on one Storage."""
    settings_store.update(settings.replace(concurrency=4, embed_artwork=False))
    for i, payload in enumerate(distinct_mp3s[N_TRACKS:]):    # files that were already there
        (library_dir / f"Old Artist - Old Song {i}.mp3").write_bytes(payload)
    tracks = [make_track(str(i), title=f"Song {i}") for i in range(N_TRACKS)]
    for t, payload in zip(tracks, distinct_mp3s, strict=False):
        fake_ydl.add(t, payload=payload, progress_steps=2)
    partial = PlaylistInfo("pl", "https://soundcloud.com/a/sets/mix", "Mix", entries=tuple(
        make_track(t.track_id, title="", artist="", duration_s=None, is_partial=True)
        for t in tracks))
    storage.sync_playlist(partial)
    m = DownloadManager(storage, settings_store, app_paths, ydl_factory=fake_ydl)
    stop = threading.Event()
    errors: list[BaseException] = []
    ids = [t.track_id for t in tracks]

    def loop(fn) -> None:
        try:
            while not stop.is_set():
                fn()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def gui_reads() -> None:
        storage.download_history(limit=200)
        storage.list_library_files(limit=500)
        storage.count_library_files()
        storage.existing_track_paths(ids)
        storage.list_playlists()
        storage.playlist_members("pl", include_removed=True)

    threads = [threading.Thread(target=loop, args=(fn,), daemon=True) for fn in (
        gui_reads, lambda: storage.sync_playlist(partial),
        lambda: scan_library(library_dir, storage))]
    try:
        for t in threads:
            t.start()
        jobs = m.enqueue([DownloadRequest(t, playlist_id="pl", playlist_title="Mix")
                          for t in tracks])
        wait_until(m.is_idle, timeout=60)
    finally:
        stop.set()
        for t in threads:
            t.join(30)
        m.shutdown(wait=True, timeout=30)
    assert errors == []
    scan_library(library_dir, storage)                       # settle

    conn = storage._conn
    with storage._lock:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    on_disk = sorted(library_dir.rglob("*.mp3"))
    assert len(on_disk) == N_TRACKS + N_OLD == storage.count_library_files()
    for job in jobs:
        done = m.get_job(job.job_id)
        assert done.status == S.COMPLETED, done.error
        assert done.final_path.is_file()
        rec = storage.get_download(job.job_id)
        assert rec.status == S.COMPLETED and rec.file_path == done.final_path
        lf = storage.get_library_file(done.final_path)
        assert lf.source == LibrarySource.DOWNLOAD and lf.track_id == done.track.track_id
        stored = storage.get_track(done.track.track_id)
        assert not stored.is_partial and stored.title == done.track.title   # never downgraded
    assert {p.track_id for p in storage.list_library_files() if p.track_id} == set(ids)
