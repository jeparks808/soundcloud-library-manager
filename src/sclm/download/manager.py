"""DownloadManager: bounded worker pool running download jobs (ARCHITECTURE.md §4–§7).

Pure Python, no Qt. One ``YoutubeDL`` per job attempt, created inside the worker thread.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from sclm.core.errors import (
    NETWORK_BACKOFF_BASE_S,
    NETWORK_MAX_ATTEMPTS,
    ErrorCategory,
    ErrorInfo,
    FFmpegError,
    JobCancelled,
    SclmError,
    TrackUnavailableError,
)
from sclm.core.events import DownloadListener, NullListener
from sclm.core.ffmpeg import FfmpegInfo, resolve_ffmpeg
from sclm.core.models import (
    ConflictPolicy,
    DownloadJob,
    DownloadRequest,
    DuplicateMatch,
    DuplicateReason,
    FinalizeOutcome,
    FinalizeResult,
    JobStatus,
    LibraryFile,
    LibrarySource,
    ProgressEvent,
    TrackInfo,
)
from sclm.core.paths import AppPaths
from sclm.core.settings import CONCURRENCY_MAX, CONCURRENCY_MIN, Settings, SettingsStore
from sclm.core.storage import Storage
from sclm.download.errors import classify_ytdlp_error
from sclm.download.mapping import ensure_full_audio, merge_resolved, trackinfo_from_info
from sclm.download.ytdlp_opts import download_opts, prepare_cookiefile

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.25          # <= 4 progress events per second per job
_POOL_SIZE = CONCURRENCY_MAX

_STAGE_MESSAGES = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RESOLVING: "Fetching track info",
    JobStatus.DOWNLOADING: "Downloading",
    JobStatus.CONVERTING: "Converting to MP3",
    JobStatus.TAGGING: "Tagging",
    JobStatus.ORGANIZING: "Moving into the library",
    JobStatus.CANCELLED: "Cancelled",
}
_DUPLICATE_REASONS = {
    DuplicateReason.TRACK_ID: "same SoundCloud track",
    DuplicateReason.ARTIST_TITLE: "same artist, title and length",
    DuplicateReason.CONTENT_HASH: "identical audio",
    DuplicateReason.PATH_EXISTS: "file name already taken",
}


class FinalizeFn(Protocol):
    """Signature of :func:`sclm.metadata.pipeline.finalize_track`."""

    def __call__(self, staged_file: Path, track: TrackInfo, *, playlist_title: str | None,
                 settings: Settings, storage: Storage, force: bool = False,
                 on_stage: Callable[[JobStatus], None] | None = None,
                 cancel: threading.Event | None = None) -> FinalizeResult: ...


FindDuplicateFn = Callable[[Storage, TrackInfo], DuplicateMatch | None]
"""Signature of :func:`sclm.metadata.duplicates.find_duplicate`."""

YdlFactory = Callable[[dict[str, Any]], Any]
FfmpegResolver = Callable[[str | None], FfmpegInfo]


def _default_ydl_factory(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)  # type: ignore[arg-type]


def _rmtree(path: Path) -> None:
    """Remove an app-owned staging dir (never follows symlinks out of it)."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove staging dir %s: %s", path, exc)


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise JobCancelled()


def _skip_for(match: DuplicateMatch, policy: ConflictPolicy) -> bool:
    """Policy table of ARCHITECTURE.md §5 for the pre-download check."""
    if match.reason == DuplicateReason.TRACK_ID:
        return True                     # the same track is never downloaded twice
    return ConflictPolicy(policy) != ConflictPolicy.KEEP_BOTH


def _duplicate_message(match: DuplicateMatch | None) -> str:
    if match is None:
        return "Already in your library"
    reason = _DUPLICATE_REASONS.get(match.reason, str(match.reason))
    return f"Already in your library ({reason}): {match.path.name}"


def _locate_mp3(result: Any, staging: Path) -> Path:
    """The converted MP3 produced by yt-dlp inside ``staging``."""
    staging_real = staging.resolve()
    candidates: list[str] = []
    if isinstance(result, Mapping):
        for rd in result.get("requested_downloads") or []:
            if isinstance(rd, Mapping) and isinstance(rd.get("filepath"), str):
                candidates.append(rd["filepath"])
        if isinstance(result.get("filepath"), str):
            candidates.append(result["filepath"])
    for cand in candidates:
        p = Path(cand)
        if p.suffix.lower() == ".mp3" and p.is_file() \
                and p.resolve().is_relative_to(staging_real):
            return p
    mp3s = [p for p in staging.glob("*.mp3") if p.is_file() and not p.is_symlink()]
    if mp3s:
        return max(mp3s, key=lambda p: p.stat().st_size)
    present = sorted(p.name for p in staging.iterdir()) if staging.is_dir() else []
    raise FFmpegError("The converted MP3 file was not found.",
                      detail=f"staging dir contents: {present}")


@dataclass(slots=True)
class _Entry:
    job: DownloadJob
    cancel: threading.Event
    last_progress: float = 0.0


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

    Keyword-only test seams (the app passes none of them): ``ydl_factory`` (default
    ``yt_dlp.YoutubeDL``), ``ffmpeg_resolver`` (default ``core.ffmpeg.resolve_ffmpeg``),
    ``backoff_base_s`` (default ``NETWORK_BACKOFF_BASE_S``), ``max_attempts`` (default
    ``NETWORK_MAX_ATTEMPTS``), ``extra_ydl_opts`` (merged into the yt-dlp options, e.g.
    ``{"enable_file_urls": True}`` for offline tests).
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
        ydl_factory: YdlFactory | None = None,
        ffmpeg_resolver: FfmpegResolver | None = None,
        backoff_base_s: float = NETWORK_BACKOFF_BASE_S,
        max_attempts: int = NETWORK_MAX_ATTEMPTS,
        extra_ydl_opts: Mapping[str, Any] | None = None,
    ) -> None:
        if finalize is None:
            from sclm.metadata.pipeline import finalize_track

            finalize = finalize_track
        if find_duplicate is None:
            from sclm.metadata.duplicates import find_duplicate as _find_duplicate

            find_duplicate = _find_duplicate
        self._storage = storage
        self._settings = settings
        self._paths = paths
        self._listener: DownloadListener = listener or NullListener()
        self._finalize = finalize
        self._find_duplicate = find_duplicate
        self._ydl_factory = ydl_factory or _default_ydl_factory
        self._ffmpeg_resolver = ffmpeg_resolver or resolve_ffmpeg
        self._backoff_base_s = float(backoff_base_s)
        self._max_attempts = max(1, int(max_attempts))
        self._extra_ydl_opts = dict(extra_ydl_opts or {})

        self._lock = threading.Lock()
        self._entries: dict[int, _Entry] = {}
        self._pending: deque[int] = deque()
        self._running: set[int] = set()
        self._futures: set[Future[None]] = set()
        self._concurrency = self._clamp(settings.get().concurrency)
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="sclm-dl")

        self._clean_stale_dirs()
        self._unsubscribe: Callable[[], None] | None = settings.subscribe(
            self._on_settings_changed)

    # --- public API ----------------------------------------------------------------

    def set_listener(self, listener: DownloadListener | None) -> None:
        """Replace the listener (None -> NullListener)."""
        self._listener = listener or NullListener()

    def enqueue(self, requests: Iterable[DownloadRequest]) -> list[DownloadJob]:
        """Create a QUEUED job (and ``downloads`` row) per request and schedule it.

        Requests whose track_id already has a non-terminal job are ignored. Returns the jobs
        actually created, in order. Emits ``on_job_changed`` for each. Non-blocking (only a
        quick DB insert per job); callable from the GUI thread.
        """
        bitrate = self._settings.get().bitrate_kbps
        created: list[DownloadJob] = []
        with self._lock:
            if self._closed:
                log.warning("enqueue() after shutdown ignored")
                return []
            active = {e.job.track.track_id for e in self._entries.values()
                      if not e.job.status.is_terminal}
            for req in requests:
                track_id = req.track.track_id
                if track_id in active:
                    log.info("Track %s already queued; ignoring duplicate request", track_id)
                    continue
                job_id = self._storage.create_download(
                    req.track, playlist_id=req.playlist_id, bitrate_kbps=bitrate)
                job = DownloadJob(job_id=job_id, track=req.track, playlist_id=req.playlist_id,
                                  playlist_title=req.playlist_title, force=req.force,
                                  message=_STAGE_MESSAGES[JobStatus.QUEUED])
                self._entries[job_id] = _Entry(job=job, cancel=threading.Event())
                active.add(track_id)
                created.append(job)
        for job in created:
            self._emit_job(job)
        with self._lock:
            for job in created:
                entry = self._entries.get(job.job_id)
                if entry is not None and entry.job.status == JobStatus.QUEUED:
                    self._pending.append(job.job_id)
        self._dispatch()
        return created

    def cancel(self, job_id: int) -> bool:
        """Request cancellation. QUEUED jobs become CANCELLED immediately; running jobs stop
        at the next cooperative check (progress hook, between steps, backoff wait). Returns
        False if the job is unknown or already terminal. Never blocks."""
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is None or entry.job.status.is_terminal:
                return False
            entry.cancel.set()
            if entry.job.status != JobStatus.QUEUED or job_id in self._running:
                return True
            try:
                self._pending.remove(job_id)
            except ValueError:
                pass
            entry.job = replace(entry.job, status=JobStatus.CANCELLED, fraction=None,
                                message=_STAGE_MESSAGES[JobStatus.CANCELLED])
            job = entry.job
        self._persist(job, strict=False)
        self._emit_job(job)
        return True

    def cancel_all(self) -> int:
        """Cancel every non-terminal job. Returns how many were signalled."""
        with self._lock:
            ids = [jid for jid, e in self._entries.items() if not e.job.status.is_terminal]
        return sum(1 for jid in ids if self.cancel(jid))

    def retry(self, job_id: int, *, force: bool = False) -> DownloadJob | None:
        """Re-queue a FAILED, CANCELLED or SKIPPED_DUPLICATE job (same job_id / DB row).

        ``force=True`` skips duplicate checks and uses KEEP_BOTH for path conflicts (the
        GUI's "Keep both" action for duplicates). Returns the re-queued job, or None if the
        job is unknown or not terminal (or another active job already handles the track).
        A job that was forced once stays forced.
        """
        with self._lock:
            entry = self._entries.get(job_id)
            if self._closed or entry is None or not entry.job.status.is_terminal:
                return None
            track_id = entry.job.track.track_id
            if any(e.job.track.track_id == track_id and not e.job.status.is_terminal
                   for jid, e in self._entries.items() if jid != job_id):
                log.info("Not retrying job %s: track %s is already queued", job_id, track_id)
                return None
            entry.cancel = threading.Event()
            entry.last_progress = 0.0
            entry.job = replace(entry.job, status=JobStatus.QUEUED, error=None, fraction=None,
                                message=_STAGE_MESSAGES[JobStatus.QUEUED], final_path=None,
                                duplicate_of=None, force=entry.job.force or force)
            job = entry.job
        self._persist(job, strict=False)
        self._emit_job(job)
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is not None and entry.job.status == JobStatus.QUEUED \
                    and job_id not in self._pending:
                self._pending.append(job_id)
        self._dispatch()
        return job

    def clear_finished(self) -> list[int]:
        """Forget terminal jobs from the in-memory queue (DB history is kept). Returns their ids."""
        with self._lock:
            ids = [jid for jid, e in self._entries.items()
                   if e.job.status.is_terminal and jid not in self._running]
            for jid in ids:
                del self._entries[jid]
        return ids

    def jobs(self) -> list[DownloadJob]:
        """Snapshot of all jobs in the in-memory queue, in enqueue order."""
        with self._lock:
            return [e.job for e in self._entries.values()]

    def get_job(self, job_id: int) -> DownloadJob | None:
        with self._lock:
            entry = self._entries.get(job_id)
            return entry.job if entry is not None else None

    def is_idle(self) -> bool:
        """True when no job is QUEUED or active."""
        with self._lock:
            return not any(e.job.status == JobStatus.QUEUED or e.job.status.is_active
                           for e in self._entries.values())

    def set_concurrency(self, workers: int) -> None:
        """Change the number of jobs that may run at once (1..4). Takes effect as running
        jobs finish; never interrupts a running job. The manager also subscribes to
        SettingsStore and calls this when ``Settings.concurrency`` changes."""
        with self._lock:
            self._concurrency = self._clamp(workers)
        self._dispatch()

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Cancel all jobs and stop the pool. With ``wait``, block up to ``timeout`` seconds
        for workers to exit and staging dirs to be removed. Idempotent. Called by app.main
        after the Qt event loop ends (never from a worker)."""
        with self._lock:
            first = not self._closed
            self._closed = True
        if first:
            if self._unsubscribe is not None:
                try:
                    self._unsubscribe()
                except Exception:
                    log.exception("Settings unsubscribe failed")
                self._unsubscribe = None
            self.cancel_all()
        with self._lock:
            futures = list(self._futures)
        not_done: set[Future[None]] = set()
        if wait and futures:
            _done, not_done = wait_futures(futures, timeout=timeout)
            if not_done:
                log.warning("%d download worker(s) still running after shutdown timeout",
                            len(not_done))
        # Idle pool threads exit at once; only join them when no job is still running.
        self._executor.shutdown(wait=wait and not not_done)
        with self._lock:
            all_done = not self._running
        if wait and all_done:
            self._clean_stale_dirs()

    # --- internals: scheduling --------------------------------------------------------

    @staticmethod
    def _clamp(workers: int) -> int:
        try:
            value = int(workers)
        except (TypeError, ValueError):
            value = CONCURRENCY_MIN
        clamped = max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, value))
        if clamped != value:
            log.warning("Concurrency %r out of range; using %d", workers, clamped)
        return clamped

    def _on_settings_changed(self, old: Settings, new: Settings) -> None:
        if old.concurrency != new.concurrency:
            self.set_concurrency(new.concurrency)

    def _dispatch(self) -> None:
        with self._lock:
            if self._closed:
                return
            keep: deque[int] = deque()
            while self._pending:
                job_id = self._pending.popleft()
                entry = self._entries.get(job_id)
                if entry is None or entry.job.status != JobStatus.QUEUED:
                    continue
                if job_id in self._running or len(self._running) >= self._concurrency:
                    # still owned by a finishing worker (fast retry), or no free slot
                    keep.append(job_id)
                    continue
                self._running.add(job_id)
                try:
                    future = self._executor.submit(self._worker, job_id)
                except RuntimeError:  # executor already shut down
                    self._running.discard(job_id)
                    keep.append(job_id)
                    continue
                self._futures.add(future)
                future.add_done_callback(self._forget_future)
            self._pending = keep

    def _forget_future(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _clean_stale_dirs(self) -> None:
        staging_root = self._paths.staging_dir
        try:
            staging_root.mkdir(parents=True, exist_ok=True)
            for p in staging_root.glob("job-*"):
                _rmtree(p)
            for p in self._paths.cache_dir.glob("preview-*"):
                if p.is_dir() and not p.is_symlink():
                    _rmtree(p)
        except OSError as exc:
            log.warning("Could not clean staging dir %s: %s", staging_root, exc)

    # --- internals: state changes ------------------------------------------------------

    def _emit_job(self, job: DownloadJob) -> None:
        try:
            self._listener.on_job_changed(job)
        except Exception:
            log.exception("Download listener on_job_changed failed")

    def _emit_progress(self, event: ProgressEvent) -> None:
        try:
            self._listener.on_progress(event)
        except Exception:
            log.exception("Download listener on_progress failed")

    def _persist(self, job: DownloadJob, *, strict: bool, bitrate_kbps: int | None = None
                 ) -> None:
        try:
            self._storage.update_download(
                job.job_id, status=job.status, attempts=job.attempts,
                file_path=job.final_path or job.duplicate_of, bitrate_kbps=bitrate_kbps,
                error=job.error)
        except Exception:
            if strict:
                raise
            log.exception("Could not record status %s for download %s", job.status, job.job_id)

    def _transition(self, job_id: int, *, persist: bool = True, strict: bool = True,
                    bitrate_kbps: int | None = None, **changes: Any) -> DownloadJob | None:
        """Apply ``changes`` to the job, persist (status changes) and notify the listener."""
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is None:
                return None
            entry.job = replace(entry.job, **changes)
            job = entry.job
        failure: BaseException | None = None
        if persist:
            try:
                self._persist(job, strict=True, bitrate_kbps=bitrate_kbps)
            except Exception as exc:
                if strict:
                    failure = exc
                else:
                    log.exception("Could not record status %s for download %s",
                                  job.status, job_id)
        self._emit_job(job)
        if failure is not None:
            raise failure
        return job

    def _progress(self, job_id: int, status: JobStatus, *, fraction: float | None,
                  downloaded: int | None = None, total: int | None = None,
                  speed: float | None = None, eta: float | None = None,
                  final: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is None:
                return
            if not final and now - entry.last_progress < PROGRESS_INTERVAL_S:
                return
            entry.last_progress = now
            entry.job = replace(entry.job, fraction=fraction)
        self._emit_progress(ProgressEvent(
            job_id=job_id, status=status, fraction=fraction, downloaded_bytes=downloaded,
            total_bytes=total, speed_bps=speed, eta_s=eta))

    # --- internals: worker ---------------------------------------------------------------

    def _worker(self, job_id: int) -> None:
        try:
            self._run_job(job_id)
        except BaseException as exc:  # last-resort guard: never leave a job non-terminal
            log.exception("Unexpected error in download worker for job %s", job_id)
            job = self.get_job(job_id)
            if job is not None and not job.status.is_terminal:
                info = ErrorInfo(ErrorCategory.UNKNOWN, SclmError.default_message,
                                 f"{type(exc).__name__}: {exc}")
                self._transition(job_id, strict=False, status=JobStatus.FAILED, error=info,
                                 fraction=None, message=info.user_message)
            if not isinstance(exc, Exception):
                raise
        finally:
            with self._lock:
                self._running.discard(job_id)
            self._dispatch()

    def _run_job(self, job_id: int) -> None:
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is None:
                return
            cancel = entry.cancel
        settings = self._settings.get()        # one snapshot for the whole job
        staging = self._paths.job_staging_dir(job_id)
        final: dict[str, Any]
        attempt = 0
        cancelled = {"status": JobStatus.CANCELLED, "fraction": None,
                     "message": _STAGE_MESSAGES[JobStatus.CANCELLED]}
        try:
            while True:
                attempt += 1
                if cancel.is_set():
                    final = dict(cancelled)
                    break
                current = self.get_job(job_id)
                assert current is not None
                try:
                    self._transition(job_id, status=JobStatus.RESOLVING,
                                     attempts=current.attempts + 1, error=None, fraction=None,
                                     message=_STAGE_MESSAGES[JobStatus.RESOLVING])
                    final = self._attempt(job_id, settings, staging, cancel)
                    break
                except Exception as exc:
                    if cancel.is_set():
                        final = dict(cancelled)
                        break
                    err = exc if isinstance(exc, SclmError) else classify_ytdlp_error(exc)
                    if isinstance(err, JobCancelled):
                        final = dict(cancelled)
                        break
                    info = err.to_info()
                    with self._lock:
                        closed = self._closed
                    if err.retryable and attempt < self._max_attempts and not closed:
                        delay = self._backoff_base_s * 2 ** (attempt - 1)
                        _rmtree(staging)    # each attempt starts in a fresh staging dir
                        self._transition(
                            job_id, status=JobStatus.WAITING_RETRY, error=info, fraction=None,
                            message=f"Retrying in {delay:g} s "
                                    f"(attempt {attempt + 1} of {self._max_attempts})")
                        log.info("Job %s: network error (%s); retrying in %g s", job_id,
                                 info.detail, delay)
                        if cancel.wait(delay):
                            final = dict(cancelled)
                            break
                        continue
                    if info.category == ErrorCategory.UNKNOWN:
                        log.warning("Download %s failed: %s", job_id, info.detail,
                                    exc_info=exc)
                    else:
                        log.warning("Download %s failed (%s): %s | %s", job_id,
                                    info.category, info.user_message, info.detail)
                    final = {"status": JobStatus.FAILED, "error": info, "fraction": None,
                             "message": info.user_message}
                    break
        finally:
            _rmtree(staging)
        bitrate = final.pop("_bitrate", None)
        self._transition(job_id, strict=False, bitrate_kbps=bitrate, **final)

    def _attempt(self, job_id: int, settings: Settings, staging: Path,
                 cancel: threading.Event) -> dict[str, Any]:
        """One attempt: resolve -> duplicate pre-check -> download+convert -> finalize -> record.

        Returns the terminal changes for the job; raises on failure.
        """
        job = self.get_job(job_id)
        assert job is not None
        ffmpeg = self._ffmpeg_resolver(settings.ffmpeg_path_or_none).path
        _rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        cookiefile = prepare_cookiefile(settings, staging)

        def progress_hook(d: dict[str, Any]) -> None:
            if cancel.is_set():
                raise JobCancelled()
            status = d.get("status")
            downloaded = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if status == "downloading":
                fraction = None
                if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) \
                        and total > 0:
                    fraction = max(0.0, min(1.0, downloaded / total))
                else:
                    idx, count = d.get("fragment_index"), d.get("fragment_count")
                    if isinstance(idx, int) and isinstance(count, int) and count > 0:
                        fraction = max(0.0, min(1.0, idx / count))
                self._progress(job_id, JobStatus.DOWNLOADING, fraction=fraction,
                               downloaded=_int(downloaded), total=_int(total),
                               speed=_float(d.get("speed")), eta=_float(d.get("eta")))
            elif status == "finished":
                size = _int(total) or _int(downloaded)
                self._progress(job_id, JobStatus.DOWNLOADING, fraction=1.0,
                               downloaded=size, total=size, final=True)

        def postprocessor_hook(d: dict[str, Any]) -> None:
            if cancel.is_set():
                raise JobCancelled()
            if d.get("status") == "started" and d.get("postprocessor") == "ExtractAudio":
                current = self.get_job(job_id)
                if current is not None and current.status != JobStatus.CONVERTING:
                    self._transition(job_id, status=JobStatus.CONVERTING, fraction=None,
                                     message=_STAGE_MESSAGES[JobStatus.CONVERTING])
                    self._progress(job_id, JobStatus.CONVERTING, fraction=None, final=True)

        opts = download_opts(
            settings, cache_dir=self._paths.cache_dir, staging_dir=staging, ffmpeg_path=ffmpeg,
            progress_hook=progress_hook, postprocessor_hook=postprocessor_hook,
            cookiefile=cookiefile, extra=self._extra_ydl_opts)

        with self._ydl_factory(opts) as ydl:
            # Resolve without processing so the single process_ie_result(download=True) below
            # is the only processing pass (no second page fetch, no double field filling).
            info = ydl.extract_info(job.track.url, download=False, process=False)
            if isinstance(info, Mapping) and info.get("_type") in ("url", "url_transparent"):
                info = ydl.process_ie_result(dict(info), download=False)  # redirect / short link
            _check_cancel(cancel)
            if not isinstance(info, Mapping):
                raise TrackUnavailableError(detail="yt-dlp returned no information")
            if info.get("_type") in ("playlist", "multi_video"):
                raise TrackUnavailableError("This URL is a playlist, not a single track.",
                                            detail=job.track.url)
            ensure_full_audio(info)
            track = merge_resolved(job.track, trackinfo_from_info(info, partial=False))
            self._storage.upsert_track(track)
            self._transition(job_id, persist=False, track=track)

            if not job.force:
                match = self._find_duplicate(self._storage, track)
                if match is not None and _skip_for(match, settings.conflict_policy):
                    log.info("Job %s skipped before download: %s", job_id,
                             _duplicate_message(match))
                    return {"status": JobStatus.SKIPPED_DUPLICATE, "duplicate_of": match.path,
                            "fraction": None, "error": None,
                            "message": _duplicate_message(match)}
            _check_cancel(cancel)
            self._transition(job_id, status=JobStatus.DOWNLOADING, fraction=0.0,
                             message=_STAGE_MESSAGES[JobStatus.DOWNLOADING])
            result = ydl.process_ie_result(dict(info), download=True)
        _check_cancel(cancel)
        staged = _locate_mp3(result, staging)

        def on_stage(status: JobStatus) -> None:
            self._transition(job_id, status=JobStatus(status), fraction=None,
                             message=_STAGE_MESSAGES.get(JobStatus(status), str(status)))

        outcome = self._finalize(staged, track, playlist_title=job.playlist_title,
                                 settings=settings, storage=self._storage, force=job.force,
                                 on_stage=on_stage, cancel=cancel)
        return self._record(job_id, track, outcome, settings)

    def _record(self, job_id: int, track: TrackInfo, result: FinalizeResult,
                settings: Settings) -> dict[str, Any]:
        """Persist the finalize outcome (manager owns these DB writes). Never raises."""
        if result.outcome != FinalizeOutcome.PLACED:
            match = result.duplicate
            return {"status": JobStatus.SKIPPED_DUPLICATE,
                    "duplicate_of": match.path if match else None, "fraction": None,
                    "error": None, "message": _duplicate_message(match)}
        final_path = result.final_path
        if final_path is None:
            info = ErrorInfo(ErrorCategory.UNKNOWN, SclmError.default_message,
                             "finalize_track returned PLACED without final_path")
            return {"status": JobStatus.FAILED, "error": info, "fraction": None,
                    "message": info.user_message}
        for warning in result.warnings:
            log.warning("Download %s: %s", job_id, warning)
        try:
            size = mtime = None
            try:
                st = final_path.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                pass
            self._storage.upsert_track(track)
            self._storage.add_library_file(LibraryFile(
                path=final_path, source=LibrarySource.DOWNLOAD, track_id=track.track_id,
                artist=track.artist, title=track.title, duration_s=track.duration_s,
                audio_sha256=result.audio_sha256, size_bytes=size, mtime=mtime))
        except Exception as exc:
            # The file IS in the library; only the bookkeeping failed. Report it, keep path.
            info = error_info_for_record(exc, final_path)
            log.warning("Download %s placed %s but recording failed: %s", job_id, final_path,
                        info.detail)
            return {"status": JobStatus.FAILED, "error": info, "fraction": None,
                    "final_path": final_path, "message": info.user_message}
        message = "Done"
        if result.warnings:
            message += " (" + "; ".join(result.warnings) + ")"
        return {"status": JobStatus.COMPLETED, "final_path": final_path, "fraction": 1.0,
                "error": None, "message": message, "_bitrate": settings.bitrate_kbps}


def error_info_for_record(exc: BaseException, final_path: Path) -> ErrorInfo:
    category = exc.category if isinstance(exc, SclmError) else ErrorCategory.STORAGE
    return ErrorInfo(category,
                     f"Saved to {final_path.name}, but the library database could not be "
                     f"updated.", f"{type(exc).__name__}: {exc}")


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
