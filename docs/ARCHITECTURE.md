# SoundCloud Library Manager: Architecture

This file is the source of truth for module boundaries and interfaces. When code and this
document disagree, fix one of them on purpose and say so in your stage report. Never let them
drift apart silently.

Target: Python 3.12, Linux Mint 22.x, MacBook Air 2015 (dual-core i5, 8 GB RAM, 1440×900).
Keep the app small and light on resources.

---

## 1. Principles

- **Legitimate access only.** We download only what yt-dlp can fetch for the current user
  (public tracks, or the user's own content via an optional cookies file). Unavailable,
  geo-blocked, private and sign-in-only tracks are reported as errors. We never retry them
  and never work around them. A 30-second Go+ preview is never saved as if it were the full
  track.
- **Never destroy user files.** Nothing in the library is ever overwritten, moved or deleted.
  Library placement uses atomic no-overwrite operations. Only the app's own staging and cache
  directories may be deleted.
- **The GUI thread does only GUI work.** Network access, ffmpeg, tagging, hashing and
  scanning run on worker threads.
- **Qt stays in `gui/`.** `core/`, `download/` and `metadata/` are plain Python and can be
  tested headless.
- **Lean.** No plugin systems and no speculative abstraction layers. The only seams are
  injectable callables for tests.

## 2. Package layout and dependency rules

```
src/sclm/
  __init__.py        __version__, APP_DISPLAY_NAME
  __main__.py        python -m sclm
  app.py             main(): composition root (the only place services are constructed)
  context.py         CoreServices, AppContext (containers handed to the GUI)
  core/              models, errors, normalize, settings, paths, logging_setup, ffmpeg,
                     events, storage
  download/          preview (flat extraction), manager (queue + workers), yt-dlp glue
  metadata/          pipeline (finalize_track), duplicates, naming, genre, scanner,
                     tagging/artwork/organizer/beets internals
  gui/               PySide6 only: main_window, bridge, theme, views, dialogs
```

Allowed imports (an arrow means "may import"):

```
gui ──► download, metadata, core, context
app ──► gui, download, core, context
download ──► metadata (ONLY pipeline.finalize_track and duplicates.find_duplicate), core
metadata ──► core
core ──► (stdlib, imageio_ffmpeg lazily)
```

- `metadata` never imports `download`. `core` never imports any other `sclm` package.
- No module outside `gui/` (and `app.py`) imports PySide6.
- `download` imports metadata functions lazily inside `DownloadManager.__init__`, so Stage 3
  can be built and tested with fakes before Stage 4 lands.

## 3. Components and responsibilities

| Component | Owner | Responsibility |
|---|---|---|
| `core.models` | Stage 1 | Frozen dataclasses and enums shared by every layer |
| `core.errors` | Stage 1 | Exception hierarchy, `ErrorInfo`, retry constants |
| `core.normalize` | Stage 1 | `match_key`, `is_soundcloud_url`, `normalize_whitespace`, `utc_now_iso` |
| `core.settings` | Stage 1 | `Settings` (validated), JSON load/save, `SettingsStore` |
| `core.paths` | Stage 1 | XDG directories, staging paths |
| `core.logging_setup` | Stage 1 | Rotating file log, excepthooks |
| `core.ffmpeg` | Stage 1 | ffmpeg lookup: settings → PATH → imageio-ffmpeg |
| `core.events` | Stage 1 | `DownloadListener` callback protocol, `NullListener`, `RecordingListener` |
| `core.storage` | Stage 1 | SQLite repository (thread-safe), migrations |
| `download.preview` | Stage 3 | `fetch_preview`: flat extraction, playlist sync, marks which tracks are new |
| `download.manager` | Stage 3 | `DownloadManager`: queue, worker pool, per-job state machine, retry, cancel. **Orchestrates the post-download pipeline and owns every DB write in the download flow.** |
| `download.*` internals | Stage 3 | yt-dlp option builder, info dict → `TrackInfo` mapping, flat-entry metadata lookup (`enrich`), error classification, yt-dlp logger adapter |
| `metadata.pipeline` | Stage 4 | `finalize_track`: tag → content-hash check → organize (no-overwrite move) → optional Beets. Never writes to the DB. |
| `metadata.duplicates` | Stage 4 | `find_duplicate` (pre-download lookup), `audio_sha256` |
| `metadata.naming` | Stage 4 | `render_filename`, `sanitize_component` |
| `metadata.genre` | Stage 4 | `normalize_genre` |
| `metadata.scanner` | Stage 4 | `scan_library`: index existing MP3s into `library_files` (owns those writes) |
| `metadata.editor` | Stage 4 | `read_tags` / `save_tags` for the GUI metadata editor (safe in-place tag write on explicit Save; updates its `library_files` row) |
| `metadata.*` internals | Stage 4 | Mutagen tagging, artwork fetch, organizer, Beets bridge |
| `gui.*` | Stage 2 | Presentation only, plus `bridge.py` (Qt signal bridge, background runner) |
| `app` | Stage 1 | Wires the pieces together and handles startup and shutdown |

## 4. Data flow: downloading a playlist

```
URL ─► fetch_preview ─► PlaylistPreview ─► user selection ─► DownloadManager.enqueue
                                                                       │
            ┌──────────────────────── worker thread (one per job) ─────┘
            ▼
 RESOLVING  full info (yt-dlp extract_info, download=False) → TrackInfo (full)
            find_duplicate(storage, track)          [skipped if job.force]
 DOWNLOADING bestaudio → <cache>/staging/job-<id>/  (progress hook: cancel check + progress)
 CONVERTING  FFmpegExtractAudio → <id>.mp3 at settings.bitrate_kbps
 TAGGING     ┐
 ORGANIZING  ┘ metadata.finalize_track(...) → FinalizeResult (PLACED | DUPLICATE)
            record: upsert_track, add_library_file, update_download(COMPLETED|SKIPPED_DUPLICATE)
            finally: remove the staging dir
```

Step by step:

1. **Paste URL.** The GUI calls `download.preview.fetch_preview(url, settings, storage,
   cancel=...)` through `gui.bridge.run_in_background`.
2. **Preview.** yt-dlp `extract_info(url, download=False)` with
   `extract_flat="in_playlist"` returns quickly. yt-dlp's flat *set* entries carry only
   id/url/album fields, so `download.enrich` batch-looks up title-less entries through
   yt-dlp's SoundCloud client (API v2 `tracks?ids=`, 50 ids per request, best effort; entries
   it can't resolve keep `title=""`). Entries become `TrackInfo(is_partial=True)`
   wherever fields are missing. For playlists, `storage.sync_playlist(info)` records
   membership: new tracks get `first_seen`, tracks that disappeared get `removed_at`.
   `storage.existing_track_paths(ids)` fills `PreviewEntry.existing_path`.
3. **Selection.** The GUI shows the entries. When `settings.only_new_tracks` is on, it
   pre-selects only entries where `is_new` is true. "New" means the track id has no existing
   file: neither a COMPLETED download nor a `library_files` row whose file is still on disk.
4. **Queue.** The GUI calls `manager.enqueue([DownloadRequest(track, playlist_id,
   playlist_title), ...])`. The manager inserts one `downloads` row per request
   (`job_id == downloads.id`), emits `on_job_changed(QUEUED)` and schedules the job. A track
   that already has a non-terminal job is ignored.
5. **Worker: resolve.** Fetch the full info for `track.url` with a new `YoutubeDL` instance
   (one per job; never shared between threads). Map it to a full `TrackInfo`. If the only
   formats available are previews (snipped Go+ tracks), raise `AuthRequiredError`.
6. **Worker: duplicate pre-check.** Unless `force` is set, call
   `metadata.duplicates.find_duplicate(storage, track)` and apply the policy table in §5. If
   the track is skipped, the job ends as SKIPPED_DUPLICATE without downloading anything.
7. **Worker: download and convert** into `paths.job_staging_dir(job_id)` (§13 lists the
   options). Progress events are throttled to 4 per second per job.
8. **Worker: finalize.** Call `metadata.pipeline.finalize_track(staged_mp3, track,
   playlist_title=..., settings=snapshot, storage=storage, force=job.force,
   on_stage=..., cancel=job_cancel_event)`. `on_stage` reports TAGGING and ORGANIZING.
9. **Worker: record** (manager only):
   - PLACED: `storage.upsert_track(track)`, then
     `storage.add_library_file(LibraryFile(final_path, LibrarySource.DOWNLOAD, track_id,
     artist, title, duration_s, audio_sha256, size_bytes, mtime))`, then
     `storage.update_download(job_id, status=COMPLETED, attempts=n, file_path=final_path,
     bitrate_kbps=b)`. Emit `on_job_changed(COMPLETED, final_path=...)`.
   - DUPLICATE: `update_download(job_id, status=SKIPPED_DUPLICATE,
     file_path=duplicate.path)`. Emit the change with `duplicate_of` set.
   - Exception: classify it (§7). Retry or end as FAILED or CANCELLED.
10. **Cleanup.** In `finally`, delete the per-job staging directory. It belongs to the app,
    so deleting it is safe. At startup the manager also wipes stale `staging/job-*` dirs and
    `build_core` marks leftover non-terminal `downloads` rows as FAILED ("Interrupted").

**Pipeline ownership (settled):** `DownloadManager` (download/) orchestrates the pipeline.
It calls the metadata functions `find_duplicate` and `finalize_track` and performs **all** DB
writes for the download flow. `metadata/` writes to the DB only in `scan_library` and `editor.save_tags` (both `library_files`). Neither
side builds the other's part.

Each job reads `settings_store.get()` once when it starts and uses that snapshot until it
finishes.

## 5. Duplicates and conflicts

Detection:

| Reason | Where | Rule |
|---|---|---|
| `TRACK_ID` | `find_duplicate` (before download) | `storage.completed_download_path(track_id)` returns a file that exists |
| `ARTIST_TITLE` | `find_duplicate` (before download) | same `match_key` in `library_files`, both durations known and within ±2 s, file exists |
| `CONTENT_HASH` | `finalize_track` (after tagging) | `storage.find_library_files(audio_sha256=...)` returns a file that exists. The hash covers only the audio payload, so re-tagging a file does not change it |
| `PATH_EXISTS` | `finalize_track` (organize step) | the destination file name is already taken |

Policy (`settings.conflict_policy`, default `skip`):

| Situation | `skip` | `ask` (v1) | `keep_both` | `force=True` job |
|---|---|---|---|---|
| TRACK_ID | skip | skip | **skip** (the same track is never downloaded twice) | download |
| ARTIST_TITLE / CONTENT_HASH | skip | skip | download | download |
| PATH_EXISTS | skip | skip | place with suffix ` (2)`, ` (3)`… | suffix |

"Skip" ends the job as SKIPPED_DUPLICATE with `duplicate_of` set. **`ask` behaves like `skip`
in v1.** Pausing a worker to wait for a GUI answer would complicate the state machine, so
instead the queue view offers **"Keep both"** on SKIPPED_DUPLICATE rows. That button calls
`manager.retry(job_id, force=True)`, which downloads the track again. The cost is one extra
download, which is acceptable for a rare action.

When workers race for the same path, the no-overwrite link (`os.link`, which fails with
`EEXIST`) guarantees exactly one wins. The loser applies the policy again: it skips, or it
tries the next suffix.

## 6. Threading model

| Thread | Runs |
|---|---|
| GUI thread | Qt widgets and models. Small paged SQLite reads (≤ 500 rows: history, library list, playlists) are allowed here because they take milliseconds. Nothing touches the network, ffmpeg or the filesystem tree here |
| Download workers | `concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="sclm-dl")`. The manager dispatches at most `settings.concurrency` jobs at once (default 2, range 1–4) from its own pending deque, so `set_concurrency` takes effect without rebuilding the pool |
| Background tasks | `QThreadPool` via `gui.bridge.run_in_background`: `fetch_preview`, `scan_library`, `resolve_ffmpeg` + `ffmpeg_version` for the settings dialog |

Rules:

- **Shared objects.** One `Storage` instance is shared by everyone. It uses a single SQLite
  connection behind an `RLock`, and every public method locks it, so calls are thread-safe.
  `SettingsStore.get()` is thread-safe. Models are frozen, so they can be passed between
  threads freely.
- **Callbacks.** Workers report through the `DownloadListener` protocol (`core.events`).
  `DownloadManager` never holds its internal lock while calling the listener and catches and
  logs any listener exception. A job's events come from the thread that currently owns the
  job. `cancel()` on a QUEUED job emits from the caller's thread.
- **Per-job ordering (Stage 5).** Every state change of a job runs as one step under that
  job's own reentrant lock (`_Entry.order`): apply → `update_download` → `on_job_changed` →
  publish. So, per job, the in-memory state, the `downloads` row and the listener see the same
  sequence, whichever thread makes the change (worker, `cancel()`, `retry()`, `enqueue()`,
  `shutdown()`). Rules that follow from it:
  - A **terminal** state is published to `get_job` / `jobs` / `is_idle` / `clear_finished` /
    `retry` only after it has been persisted and announced. A non-terminal state is
    published at once, so `is_idle()` can say "busy" a moment early but never "idle" early.
    The concurrency slot is freed after that, when the worker returns.
  - A change to a job that is already terminal is dropped; only `retry()` reopens one.
  - `retry()` on a job whose final step is still running waits for it, then re-queues.
    `cancel()` on a running job never waits (it only sets the event).
  - Lock order is `_Entry.order` → manager lock → `Storage` lock; the manager lock is never
    held while waiting for an `order` lock or calling the listener. The listener is called
    while that job's `order` lock is held. That is deadlock-free for the bridge, whose
    methods only emit Qt signals. A listener may call back into the manager for the *same*
    job (reentrant), but must not synchronously `cancel`/`retry`/`enqueue` *other* jobs from
    a worker thread.
- **Progress after termination.** An `on_progress` call can arrive after the terminal
  `on_job_changed` because of a thread race. Receivers ignore progress for jobs whose last
  known status is terminal.
- **Late events after "Clear finished".** The terminal event is queued to the GUI thread
  before the manager reports the job as finished, so `clear_finished()` can return a job
  whose event is still in the Qt queue. `QueueModel` ignores events for ids it has cleared
  (ids are never reused), so a cleared row never comes back.
- **Cooperative cancellation.** Each job has a `threading.Event`:
  - The yt-dlp progress hook checks it and raises `core.errors.JobCancelled`.
  - The worker also checks it between steps, and backoff waits use `event.wait(delay)`.
  - yt-dlp may wrap the exception in its own `DownloadError`. **Rule:** after any exception
    in a job, if the job's cancel event is set, the job is CANCELLED.
  - Cancellation can lag by up to the socket timeout (20 s) while resolving, or by the
    length of one ffmpeg conversion (a few seconds), because no hook runs during those steps.
- **Signals.** `app.main` routes SIGTERM (`kill`, session logout) and SIGINT (Ctrl+C) to
  `QApplication.quit()`, so they take the normal shutdown path below (exit code 0). The
  wakeup uses `signal.set_wakeup_fd` + a socketpair + `QSocketNotifier` (no polling timer).
  The handlers are restored when the loop ends, so a second signal during shutdown has its
  default effect.
- **Shutdown.** When the event loop ends, `app.main` calls `manager.shutdown(wait=True,
  timeout=15)`, which cancels all jobs and joins the workers, while the window and bridge
  still exist. Then it detaches the listener (`set_listener(None)`), so a worker that is still
  finishing can't emit into Qt objects being torn down. It closes the DB only if `shutdown`
  returned True (no worker left). A worker stuck in a resolve outlives the timeout. It still
  records its final CANCELLED, and Python joins it at exit. The main window asks for
  confirmation before closing while `not manager.is_idle()`.
- **Crash recovery.** A killed process leaves `downloads` rows non-terminal and a
  `staging/job-<id>` dir behind. On the next start `build_core` marks those rows FAILED
  ("Interrupted") and the manager removes stale staging dirs. Partial downloads only ever
  exist in staging, never in the library. The tracks count as new again, so the preview
  offers them for download.

### Bridge contract (Qt side)

`gui/bridge.py::DownloadBridge(QObject)` implements `DownloadListener`. Its methods only
emit signals:

```python
job_changed = Signal(object)   # DownloadJob
progress    = Signal(object)   # ProgressEvent
def on_job_changed(self, job: DownloadJob) -> None: self.job_changed.emit(job)
def on_progress(self, event: ProgressEvent) -> None: self.progress.emit(event)
```

Create the bridge on the GUI thread. Emitting from a worker then reaches GUI-thread
receivers as a queued connection. **Connect these signals to methods of QObjects that live
on the GUI thread** (for example, slots on your models or views), so delivery is always
queued to the GUI thread. A test in `tests/core/test_app_paths_logging.py` covers this.

## 7. Error model

All user-facing failures are `SclmError` subclasses (`core/errors.py`). An exception stays on
the thread that raised it. Anything that crosses threads, or is stored in the DB, is an
`ErrorInfo(category, user_message, detail)`. `error_info_from_exception(exc)` converts any
exception: `OSError` becomes FILESYSTEM, anything unrecognized becomes UNKNOWN.

| Category | Exception | Auto-retry | Typical sources (for `classify_ytdlp_error`) |
|---|---|---|---|
| `unavailable` | `TrackUnavailableError` | no | HTTP 404, "not available", "has been removed", no formats |
| `geo_blocked` | `GeoBlockedError` | no | yt-dlp `GeoRestrictedError`, "not available in your country" |
| `private` | `PrivateTrackError` | no | "private", 403 on a private set |
| `auth_required` | `AuthRequiredError` | no | "sign in", "login required", "Go+", only preview formats |
| `network` | `NetworkError` | **yes** | `URLError`, timeouts, connection reset, DNS failure, HTTP 429/5xx, `TransportError` |
| `ffmpeg` | `FFmpegError` / `FFmpegNotFoundError` | no | yt-dlp `PostProcessingError`; `detail` holds the last 15 stderr lines (kept by `ytdlp_opts.build_download_ydl`, §13) plus "FFmpeg used: <path> (from settings\|path\|bundled)" |
| `filesystem` | `FileSystemError` | no | ENOSPC, EACCES, bad library path, tag write failure |
| `cancelled` | `JobCancelled` | no | user cancel, shutdown |
| `invalid_url` | `InvalidUrlError` | no | not a SoundCloud URL (checked before any network access) |
| `config` | `ConfigError` | no | invalid settings |
| `storage` | `StorageError` | no | SQLite failure, DB from a newer version |
| `unknown` | other | no | logged with traceback |

**Retry.** `NETWORK_MAX_ATTEMPTS = 3` attempts in total. The delay before retry n is
`NETWORK_BACKOFF_BASE_S * 2**(n-1)`, which gives 2 s and then 4 s. While waiting, the job is
WAITING_RETRY, `error` is set, and `message` reads like "Retrying in 4 s (attempt 3 of 3)".
Each attempt restarts at RESOLVING in a fresh staging dir. yt-dlp's own `retries=3` handles
HTTP-level retries inside one attempt. A manual `retry()` keeps the job id, DB row and
attempt count, clears the error and the stored path (`downloads.file_path` → NULL until the
new run records one), and is always allowed for terminal jobs. Previews are never retried
automatically.

**Messages.** `user_message` is short, plain and has no traceback. `detail` holds the
technical text and appears behind a "Details" expander and in the log. Every FAILED job is
logged at WARNING level with its `detail`. UNKNOWN failures are logged with a traceback.

## 8. Public interfaces (exact signatures)

Everything in this section crosses a package boundary. Stub bodies raise
`NotImplementedError` until the owning stage fills them in. Stages may add private helpers
and keyword-only test seams. They must not change these signatures without updating this
document.

### 8.1 core (implemented)

```python
# core/models.py  (all @dataclass(frozen=True, slots=True); enums are StrEnum)
class JobStatus(StrEnum): QUEUED, RESOLVING, DOWNLOADING, CONVERTING, TAGGING, ORGANIZING,
                          WAITING_RETRY, COMPLETED, SKIPPED_DUPLICATE, FAILED, CANCELLED
    is_terminal: bool  # COMPLETED, SKIPPED_DUPLICATE, FAILED, CANCELLED
    is_active: bool    # RESOLVING..ORGANIZING, WAITING_RETRY
class OrganizeMode(StrEnum): PLAYLIST, ARTIST, ALBUM, GENRE
class ConflictPolicy(StrEnum): SKIP, KEEP_BOTH, ASK
class DuplicateReason(StrEnum): TRACK_ID, ARTIST_TITLE, CONTENT_HASH, PATH_EXISTS
class FinalizeOutcome(StrEnum): PLACED, DUPLICATE
class LibrarySource(StrEnum): DOWNLOAD, SCAN

TrackInfo(track_id: str, url: str, title: str = "", artist: str = "",
          duration_s: float | None = None, genre: str | None = None, album: str | None = None,
          upload_date: str | None = None, artwork_url: str | None = None,
          description: str | None = None, uploader_id: str | None = None,
          playlist_index: int | None = None, is_partial: bool = False)
    .year -> int | None ; .display_name -> str ; .match_key -> str
PlaylistInfo(playlist_id: str | None, url: str, title: str, uploader: str | None = None,
             entries: tuple[TrackInfo, ...] = ())            .is_single_track -> bool
PlaylistSyncResult(added: tuple[str, ...] = (), removed: tuple[str, ...] = ())
PreviewEntry(track: TrackInfo, existing_path: Path | None = None)   .is_new -> bool
PlaylistPreview(playlist: PlaylistInfo, entries: tuple[PreviewEntry, ...],
                sync: PlaylistSyncResult = PlaylistSyncResult())   .new_entries
DownloadRequest(track: TrackInfo, playlist_id: str | None = None,
                playlist_title: str | None = None, force: bool = False)
DownloadJob(job_id: int, track: TrackInfo, playlist_id: str | None = None,
            playlist_title: str | None = None, status: JobStatus = QUEUED, attempts: int = 0,
            fraction: float | None = None, message: str = "", error: ErrorInfo | None = None,
            final_path: Path | None = None, duplicate_of: Path | None = None, force: bool = False)
ProgressEvent(job_id: int, status: JobStatus, fraction: float | None = None,
              downloaded_bytes: int | None = None, total_bytes: int | None = None,
              speed_bps: float | None = None, eta_s: float | None = None)
DuplicateMatch(reason: DuplicateReason, path: Path, track_id: str | None = None)
FinalizeResult(outcome: FinalizeOutcome, final_path: Path | None = None,
               audio_sha256: str | None = None, duplicate: DuplicateMatch | None = None,
               warnings: tuple[str, ...] = ())
ScanResult(files_seen: int = 0, added: int = 0, updated: int = 0, removed_missing: int = 0,
           errors: tuple[str, ...] = ())
TagData(title="", artist="", album="", album_artist="", genre="", year: int | None = None,
        track_number: int | None = None, comment="",
        track_id: str | None = None, source_url: str | None = None,     # read-only
        duration_s: float | None = None, has_artwork: bool = False)     # read-only
LibraryFile(path: Path, source: LibrarySource, track_id: str | None = None, artist: str = "",
            title: str = "", duration_s: float | None = None, audio_sha256: str | None = None,
            size_bytes: int | None = None, mtime: float | None = None,
            added_at: str | None = None, id: int | None = None)     .match_key -> str
PlaylistRecord(playlist_id, url, title, uploader, first_seen, last_synced, track_count=0)
PlaylistMember(playlist_id, track: TrackInfo, position, first_seen, removed_at=None)
DownloadRecord(id, track_id, playlist_id, status: JobStatus, attempts, bitrate_kbps,
               file_path: Path | None, error: ErrorInfo | None, created_at, updated_at,
               completed_at, title="", artist="")
```

```python
# core/errors.py
class ErrorCategory(StrEnum): UNAVAILABLE, GEO_BLOCKED, PRIVATE, AUTH_REQUIRED, NETWORK,
    FFMPEG, FILESYSTEM, CANCELLED, INVALID_URL, CONFIG, STORAGE, UNKNOWN
ErrorInfo(category: ErrorCategory, user_message: str, detail: str = "")   .retryable -> bool
class SclmError(Exception):
    def __init__(self, user_message: str | None = None, *, detail: str = "") -> None
    category: ClassVar[ErrorCategory]; user_message: str; detail: str
    retryable -> bool; def to_info(self) -> ErrorInfo
DownloadError(SclmError) > TrackUnavailableError, GeoBlockedError, PrivateTrackError,
                           AuthRequiredError, NetworkError
InvalidUrlError, JobCancelled, FileSystemError, ConfigError, StorageError  (SclmError)
class FFmpegError(SclmError):
    def __init__(self, user_message: str | None = None, *, stderr: str = "", detail: str = "")
    stderr_tail: str
class FFmpegNotFoundError(FFmpegError)
def error_info_from_exception(exc: BaseException) -> ErrorInfo
def tail_lines(text: str, n: int = 15) -> str
NETWORK_MAX_ATTEMPTS = 3; NETWORK_BACKOFF_BASE_S = 2.0; FFMPEG_STDERR_TAIL_LINES = 15
```

```python
# core/normalize.py
def match_key(artist: str | None, title: str | None) -> str      # '' if title empty; persisted
def is_soundcloud_url(url: str | None) -> bool
def normalize_whitespace(text: str | None) -> str
def utc_now_iso() -> str
```

```python
# core/settings.py
@dataclass(frozen=True, slots=True) class Settings: (fields in §11)
    library_path -> Path ; ffmpeg_path_or_none -> str | None ; cookies_path -> Path | None
    def replace(self, **changes) -> Settings          # validated; raises ConfigError
    def to_dict(self) -> dict ; @classmethod def from_dict(cls, data) -> Settings  # lenient
def validate_filename_template(template: str) -> None                # raises ConfigError
def load_settings(path: Path) -> Settings
def save_settings(settings: Settings, path: Path) -> None            # atomic
class SettingsStore:
    def __init__(self, path: Path, initial: Settings | None = None) -> None
    path -> Path
    def get(self) -> Settings
    def update(self, new: Settings) -> None       # save, swap, notify listeners (old, new)
    def subscribe(self, listener: Callable[[Settings, Settings], None]) -> Callable[[], None]
BITRATES_KBPS = (128, 192, 256, 320); CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 4
FILENAME_FIELDS = {"artist","title","playlist","index","track_id","genre","year","album"}
```

```python
# core/paths.py
@dataclass(frozen=True, slots=True) class AppPaths(config_dir, data_dir, cache_dir, state_dir):
    @classmethod def from_env(cls, env: Mapping[str, str] | None = None) -> AppPaths
    @classmethod def under(cls, root: Path) -> AppPaths
    settings_file, db_file, log_dir, log_file, staging_dir -> Path
    def job_staging_dir(self, job_id: int) -> Path
    def ensure(self) -> AppPaths
```

```python
# core/ffmpeg.py
class FfmpegSource(StrEnum): SETTINGS, PATH, BUNDLED
FfmpegInfo(path: Path, source: FfmpegSource)
def resolve_ffmpeg(configured: str | os.PathLike | None = None, *,
                   which=shutil.which, bundled=_bundled_ffmpeg) -> FfmpegInfo
                   # raises FFmpegNotFoundError
def ffmpeg_version(path: Path, *, timeout: float = 10.0) -> str | None
```

```python
# core/logging_setup.py
def setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> Path
def install_excepthook() -> None
```

```python
# core/events.py
class DownloadListener(Protocol):
    def on_job_changed(self, job: DownloadJob) -> None
    def on_progress(self, event: ProgressEvent) -> None
class NullListener; class RecordingListener   # .jobs, .progress, .statuses(id), .last(id)
```

```python
# core/storage.py   (thread-safe; all methods may raise StorageError)
class Storage:
    def __init__(self, db_path: Path | str) -> None        # ":memory:" allowed; migrates
    def close(self) -> None ; __enter__/__exit__ ; schema_version -> int
    # tracks
    def upsert_track(self, track: TrackInfo) -> TrackInfo   # merge rule below
    def get_track(self, track_id: str) -> TrackInfo | None
    # playlists
    def sync_playlist(self, playlist: PlaylistInfo) -> PlaylistSyncResult  # ValueError if single
    def get_playlist(self, playlist_id: str) -> PlaylistRecord | None
    def list_playlists(self) -> list[PlaylistRecord]
    def playlist_members(self, playlist_id: str, *, include_removed: bool = False
                         ) -> list[PlaylistMember]
    # downloads
    def create_download(self, track: TrackInfo, *, playlist_id: str | None = None,
                        bitrate_kbps: int | None = None) -> int     # upserts track; QUEUED
    def update_download(self, download_id: int, *, status: JobStatus,
                        attempts: int | None = None,
                        file_path: Path | Literal[CLEAR] | None = None,  # None keeps, CLEAR -> NULL
                        bitrate_kbps: int | None = None, error: ErrorInfo | None = None) -> None
    def get_download(self, download_id: int) -> DownloadRecord | None
    def download_history(self, *, limit: int = 200, offset: int = 0,
                         statuses: Iterable[JobStatus] | None = None) -> list[DownloadRecord]
    def mark_interrupted_downloads(self) -> int
    def existing_track_paths(self, track_ids: Iterable[str]) -> dict[str, Path]  # disk-checked
    def completed_download_path(self, track_id: str) -> Path | None             # disk-checked
    # library files
    def add_library_file(self, f: LibraryFile) -> int        # upsert by path
    def get_library_file(self, path: Path) -> LibraryFile | None
    def find_library_files(self, *, track_id: str | None = None, match_key: str | None = None,
                           audio_sha256: str | None = None) -> list[LibraryFile]  # exactly one
    def list_library_files(self, *, search: str | None = None, limit: int = 500,
                           offset: int = 0) -> list[LibraryFile]
    def library_paths_under(self, root: Path) -> list[Path]
    def remove_library_file(self, path: Path) -> bool        # DB row only
    def count_library_files(self) -> int
```

**Track merge rule.** A non-empty incoming value replaces the stored one. Empty or `None`
values never erase known data. A partial record (`is_partial=True`, from a flat extraction)
only fills fields that are still empty and never downgrades a full record to partial.

```python
# context.py
CoreServices(paths: AppPaths, settings: SettingsStore, storage: Storage)
AppContext(paths, settings, storage, manager: DownloadManager, bridge: DownloadBridge)
# app.py
def build_core(paths: AppPaths) -> CoreServices
def main(argv: list[str] | None = None) -> int
```

### 8.2 download (Stage 3)

```python
# download/preview.py
def fetch_preview(url: str, settings: Settings, storage: Storage, *,
                  cancel: threading.Event | None = None) -> PlaylistPreview

# download/manager.py
class FinalizeFn(Protocol): ...           # == metadata.pipeline.finalize_track signature
FindDuplicateFn = Callable[[Storage, TrackInfo], DuplicateMatch | None]

class DownloadManager:
    def __init__(self, storage: Storage, settings: SettingsStore, paths: AppPaths,
                 listener: DownloadListener | None = None, *,
                 finalize: FinalizeFn | None = None,
                 find_duplicate: FindDuplicateFn | None = None) -> None
    def set_listener(self, listener: DownloadListener | None) -> None
    def enqueue(self, requests: Iterable[DownloadRequest]) -> list[DownloadJob]
    def cancel(self, job_id: int) -> bool
    def cancel_all(self) -> int
    def retry(self, job_id: int, *, force: bool = False) -> DownloadJob | None
    def clear_finished(self) -> list[int]
    def jobs(self) -> list[DownloadJob]
    def get_job(self, job_id: int) -> DownloadJob | None
    def is_idle(self) -> bool
    def set_concurrency(self, workers: int) -> None
    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool  # True: no worker left
```

The stub docstrings in `src/sclm/download/*.py` describe each method's semantics in detail.
Suggested internal modules (not binding): `ytdlp_opts.py` (option builder plus logger
adapter), `mapping.py` (`trackinfo_from_info(info: dict, *, playlist_index=None) ->
TrackInfo`), `errors.py` (`classify_ytdlp_error(exc: BaseException) -> SclmError`).
Artwork: choose the best `thumbnails` entry and prefer `t500x500` or `original`. You can
rewrite `-large.jpg` to `-t500x500.jpg`.

### 8.3 metadata (Stage 4)

```python
# metadata/pipeline.py
def finalize_track(staged_file: Path, track: TrackInfo, *, playlist_title: str | None,
                   settings: Settings, storage: Storage, force: bool = False,
                   on_stage: Callable[[JobStatus], None] | None = None,
                   cancel: threading.Event | None = None) -> FinalizeResult
# metadata/duplicates.py
DURATION_TOLERANCE_S = 2.0
def find_duplicate(storage: Storage, track: TrackInfo) -> DuplicateMatch | None
def audio_sha256(path: Path) -> str
# metadata/naming.py
MAX_COMPONENT_BYTES = 200
def sanitize_component(name: str, *, fallback: str = "Unknown") -> str
def render_filename(template: str, track: TrackInfo, *, playlist_title: str | None = None) -> str
# metadata/genre.py
UNKNOWN_GENRE = "Unknown"
def normalize_genre(raw: str | None) -> str
# metadata/scanner.py
def scan_library(root: Path, storage: Storage, *,
                 progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> ScanResult
# metadata/editor.py
def read_tags(path: Path) -> TagData
def save_tags(path: Path, tags: TagData, storage: Storage, *,
              artwork: bytes | None = None, remove_artwork: bool = False) -> LibraryFile
```

The stub docstrings in `src/sclm/metadata/*.py` are the detailed behavior spec. Suggested
internals (not binding): `tagger.py` (`write_tags(path, track, *, genre, album,
artwork: bytes | None) -> None`), `artwork.py` (`fetch_artwork(url, *, timeout=15) -> bytes |
None` via `urllib`, injectable in tests), `organizer.py` (`plan_destination(...)`,
`place_file(src, dest, policy) -> Path | None`), `beets_bridge.py` (`is_available() -> bool`,
`import_file(path) -> None`).

## 9. GUI modules (Stage 2)

| Module | Contents |
|---|---|
| `main_window.py` | `MainWindow(QMainWindow).__init__(self, ctx: AppContext)`. Holds the tabs or panels, status bar (library count, queue summary) and close confirmation |
| `bridge.py` | `DownloadBridge` (implemented, §6), `run_in_background(fn, on_done, on_error, *, parent=None)` |
| `theme.py` | Dark palette and QSS with SoundCloud-orange accents (`#ff5500`). `apply_theme(app: QApplication) -> None` |
| `preview_panel.py` | URL field, Fetch, preview table with checkboxes, "Only new" toggle, "Download selected", added/removed counts from `PlaylistPreview.sync` |
| `queue_view.py` | Table model fed by bridge signals: status, progress bar, message. Actions: Cancel, Retry, "Keep both" (`retry(force=True)`), Cancel all, Clear finished |
| `history_view.py` | `storage.download_history(...)`, paged, with filter by status and error details |
| `library_view.py` | `storage.list_library_files(search=...)`, "Scan library" (`scan_library` in the background), open the containing folder, "Edit tags…" |
| `metadata_editor.py` | Dialog: `read_tags` → form (title, artist, album, album artist, genre, year, track #, comment, cover image) → explicit Save → `save_tags` (both via `run_in_background`). Shows track id and source URL read-only |
| `settings_dialog.py` | Edits `Settings` and validates with `Settings.replace` (shows `ConfigError.user_message` inline). Shows the resolved ffmpeg path and version (in the background) and a live filename preview through `metadata.naming.render_filename`. Saves through `ctx.settings.update(...)` |

The GUI never constructs services. It reads `ctx.settings.get()` and the storage list
methods, and it changes state only through `ctx.manager`, `ctx.settings.update` and
background calls. Layout targets 1440×900 and must stay usable at 1280×800. Window geometry
may be stored with `QSettings`; it does not belong in `settings.json`.

## 10. SQLite schema

File: `<XDG_DATA_HOME>/soundcloud-library-manager/library.sqlite3`. WAL mode,
`foreign_keys=ON`. The version is stored in `PRAGMA user_version` (currently 1).
`core/storage.py::MIGRATIONS` maps each version to a SQL script. Scripts run in order inside
one transaction per version. A database newer than the app supports causes `StorageError`.
To add a migration, append `MIGRATIONS[n]`, bump `SCHEMA_VERSION` and add a test that
upgrades a v(n-1) file.

```sql
tracks(track_id TEXT PK, url, title, artist, duration_s REAL, genre_raw, album, upload_date,
       artwork_url, uploader_id, is_partial INT, match_key, first_seen, updated_at)
       INDEX(match_key)
playlists(playlist_id TEXT PK, url, title, uploader, first_seen, last_synced)
playlist_tracks(playlist_id FK→playlists ON DELETE CASCADE, track_id FK→tracks, position,
                first_seen, removed_at NULL=present, PK(playlist_id, track_id))
downloads(id INTEGER PK AUTOINCREMENT (= job_id), track_id FK→tracks, playlist_id,
          status (JobStatus value), attempts, bitrate_kbps, file_path,
          error_category, error_message, error_detail, created_at, updated_at, completed_at)
          INDEX(track_id, status), INDEX(created_at)
library_files(id PK, path TEXT UNIQUE (absolute), source 'download'|'scan', track_id NULL,
              artist, title, match_key, duration_s, audio_sha256, size_bytes, mtime, added_at)
              INDEX(track_id), INDEX(match_key), INDEX(audio_sha256)
```

Semantics:

- One `downloads` row per job. Retries update the same row, so `attempts` counts them.
- `file_path` holds the placed file for COMPLETED jobs and the existing file for
  SKIPPED_DUPLICATE jobs.
- `library_files` lists the files known to be in the library, whether SCLM placed them or the
  scanner found them. Upserting a path never erases a known `track_id` or `audio_sha256` and
  keeps `source='download'`.
- All timestamps are ISO-8601 UTC strings, for example `2026-11-02T10:00:00+00:00`.

## 11. Settings schema

File: `<XDG_CONFIG_HOME>/soundcloud-library-manager/settings.json`. It is a JSON object with
`"schema_version": 1` plus the fields below. Loading is lenient: unknown keys are ignored, an
invalid value falls back to its default with a log warning, and a corrupt file is moved to
`settings.json.bad`.

| Field | Type | Default | Validation |
|---|---|---|---|
| `library_dir` | str | `~/Music/SoundCloud` | non-empty; `~` expanded by `library_path` |
| `bitrate_kbps` | int | `320` | one of 128, 192, 256, 320 |
| `concurrency` | int | `2` | 1–4 |
| `organize_mode` | str | `playlist` | `playlist` \| `artist` \| `album` \| `genre` |
| `filename_template` | str | `{artist} - {title}` | fields from `FILENAME_FIELDS`, must contain `{title}`, no `/`, no `.mp3` |
| `ffmpeg_path` | str | `""` (auto) | file or directory; invalid → logged and skipped at resolve time |
| `beets_enabled` | bool | `false` | |
| `conflict_policy` | str | `skip` | `skip` \| `keep_both` \| `ask` (ask = skip in v1) |
| `only_new_tracks` | bool | `true` | preview pre-selects only new tracks |
| `embed_artwork` | bool | `true` | |
| `cookies_file` | str | `""` | optional Netscape cookies.txt for the user's own authorized content |

## 12. Files on disk

- **Paths** (`core.paths`): settings in `~/.config/soundcloud-library-manager/`, the DB in
  `~/.local/share/...`, staging in `~/.cache/.../staging/job-<id>/`, logs in
  `~/.local/state/.../logs/sclm.log`. Setting `SCLM_HOME=<dir>` moves everything under one
  directory (tests and dev runs).
- **Library layout** (`organize_mode`), with every folder component passed through
  `sanitize_component`:
  - `playlist`: `<lib>/<playlist title or "Singles">/<file>.mp3`
  - `artist`: `<lib>/<artist or "Unknown Artist">/<file>.mp3`
  - `album`: `<lib>/<artist>/<track.album or playlist title or "Singles">/<file>.mp3`
  - `genre`: `<lib>/<normalize_genre(genre)>/<file>.mp3`. Genre is used for folder names
    only in this mode.
- **Filename.** `render_filename(settings.filename_template, track,
  playlist_title=...) + ".mp3"`. A name conflict is handled by the policy in §5.
- **ID3 tags** (Mutagen, saved as **ID3v2.3** with `v2_version=3`, ID3v1 removed):
  - `TIT2` title, `TPE1` artist, `TPE2` album artist (= artist)
  - `TALB` = `track.album` or the playlist title (omitted otherwise)
  - `TCON` = normalized genre, omitted when it is "Unknown"
  - `TYER`/`TDRC` = year (Mutagen converts it for v2.3)
  - `TRCK` = `playlist_index` when the track came from a playlist
  - `WOAS` = SoundCloud URL
  - `TXXX:SOUNDCLOUD_TRACK_ID` = track id. The scanner reads this back.
  - `COMM` = description, truncated to 1000 characters
  - `APIC` type 3 (front cover), JPEG or PNG
- **Metadata editor** (`editor.save_tags`). This is the only code path that modifies a file
  already in the library, and it runs only on an explicit Save. It tags a temp copy in the
  same directory and then calls `os.replace` onto the original, so a crash never leaves a
  half-written file. It never renames the file.
- **Moves never overwrite.** On the same filesystem: `os.link(src, dest)` then
  `os.unlink(src)`. `EEXIST` means a conflict. On `EXDEV` (a different filesystem): copy to
  `dest.parent/.sclm-<rand>.part`, fsync, `os.link(part, dest)`, then unlink the part file.
  Never use `shutil.move` or `os.replace` onto a library path.

## 13. yt-dlp usage (Stage 3)

Always use the Python API (`yt_dlp.YoutubeDL`), never a subprocess. Create one instance per
job or preview, inside the worker thread.

```python
common = {
    "quiet": True, "no_warnings": False, "noprogress": True,
    "logger": <adapter to logging.getLogger("sclm.ytdlp")>,
    "socket_timeout": 20, "retries": 3, "fragment_retries": 3,
    "cachedir": str(paths.cache_dir / "yt-dlp"),
    # only if settings.cookies_path: "cookiefile": str(settings.cookies_path)
}
preview = common | {"extract_flat": "in_playlist", "skip_download": True}
download = common | {
    "format": "bestaudio/best",
    "noplaylist": True,
    "outtmpl": {"default": str(staging_dir / "%(id)s.%(ext)s")},
    "ffmpeg_location": str(resolve_ffmpeg(settings.ffmpeg_path_or_none).path),
    "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                        "preferredquality": str(settings.bitrate_kbps)}],
    "progress_hooks": [hook],             # checks cancel event; emits throttled progress
    "postprocessor_hooks": [pp_hook],     # status 'started' -> CONVERTING
}
```

Notes:

- **ffmpeg.** The bundled imageio binary has no ffprobe. yt-dlp falls back to parsing
  `ffmpeg -i` output, which is enough for ExtractAudio.
- **Bitrate.** SoundCloud sources are usually at most 160 kbps (Opus, AAC or 128k MP3), so
  the configured bitrate (default 320) never adds quality. When the source is already MP3,
  FFmpegExtractAudio stream-copies it and the setting is ignored: the original MP3 is kept to
  avoid a lossy re-encode (a deliberate decision; there is no "always re-encode" option). The
  setting applies only when a transcode is needed. After conversion the manager measures the
  real bitrate with mutagen and stores it in `downloads.bitrate_kbps`; the queue and history
  show that value, not the target.
- **Resolve once.** At download time, resolve with `extract_info(url, download=False)`, run
  the duplicate pre-check, then call `ydl.process_ie_result(info, download=True)` so the page
  is not fetched twice.
- **Offline tests** can download a local file through the real pipeline with
  `"enable_file_urls": True` and a `file://` URL. Do not enable this outside tests.
- **FFmpeg stderr (Stage 5).** yt-dlp puts only the last ffmpeg stderr line into its error.
  The manager's default factory, `ytdlp_opts.build_download_ydl(opts)`, creates the
  `YoutubeDL` and adds FFmpegExtractAudio itself (`ydl.add_post_processor`) as a small
  subclass that appends the last `FFMPEG_STDERR_TAIL_LINES` stderr lines to the error. It
  keeps the same `pp_key` ("ExtractAudio"), so hooks and logs are unchanged. `download_opts`
  still lists the postprocessor; fake factories in tests can ignore it.

## 14. Logging

`setup_logging(paths.log_dir)` attaches a 1 MB × 3 rotating file handler, plus stderr output,
to the `sclm` logger. Every module calls `logging.getLogger(__name__)`. yt-dlp output goes to
`sclm.ytdlp`: its debug messages at DEBUG, warnings at WARNING. `--debug` turns on DEBUG
level. `install_excepthook()` logs uncaught exceptions from the main thread, Qt slots and
worker threads. Logs must not contain cookie contents.

## 15. Test strategy

- **Run:** `.venv/bin/pytest -q`. It must pass offline, headless (`QT_QPA_PLATFORM=offscreen`
  is set in `tests/conftest.py`) and quickly (target < 30 s).
- **Network tests:** marked `@pytest.mark.network` and skipped unless
  `SCLM_NETWORK_TESTS=1`. Keep them to one or two public tracks.
- **Layout:** `tests/core/`, `tests/download/`, `tests/metadata/`, `tests/gui/`. Pytest runs
  with `--import-mode=importlib`, so test file names may repeat across directories. Shared
  fixtures live in `tests/conftest.py` (owned by Stage 1; do not edit). Put package-specific
  fixtures in `tests/<pkg>/conftest.py`.
- **Shared fixtures:**
  - `app_paths`, `library_dir`, `settings` (pointing at the tmp library), `settings_store`
  - `storage` (real SQLite in tmp)
  - `make_track(track_id, **overrides)`
  - `ffmpeg_exe`
  - `tiny_mp3` (a valid 1 s untagged MP3 copy)
  - an autouse fixture that isolates `HOME` and `SCLM_HOME`
- **core:** real SQLite and real files, no mocks.
- **download:**
  - Inject a fake `YoutubeDL` factory that calls the progress and postprocessor hooks. Use
    it to test the state machine, cancellation at each step, retry/backoff (make the backoff
    injectable or tiny), concurrency limits, enqueue dedupe and shutdown.
  - Inject fake `finalize` and `find_duplicate`.
  - Table-test `classify_ytdlp_error` against real yt-dlp error strings.
  - Run one offline conversion through real yt-dlp + ffmpeg via `file://`.
  - Assert the DB rows after each outcome.
- **metadata:**
  - Mutagen round trips on `tiny_mp3` (v2.3, APIC, TXXX id).
  - `audio_sha256` must not change after re-tagging.
  - No-overwrite guarantees: a pre-existing file stays byte-identical. Cover the `EXDEV`
    path by monkeypatching `os.link`. Cover the KEEP_BOTH suffixes.
  - Genre normalization table tests.
  - Scanner on a tmp tree: added, updated and missing files.
  - Editor round trip (`read_tags` → `save_tags` → `read_tags`) preserves the TXXX id and
    the audio hash.
  - Beets tests skip when beets is not importable.
- **gui:** pytest-qt with a fake manager that offers the `DownloadManager` public methods,
  plus real `Storage`. Smoke-construct `MainWindow` at 1440×900 and 1280×800. Check that the
  queue model updates from bridge signals and that no worker call happens on the GUI thread.
- **Integration (Stage 5):** fake yt-dlp producing a real MP3 → real `finalize_track` → DB
  assertions → a second run sees no new tracks.

## 16. Dependency management

- `pyproject.toml` declares lower bounds (`PySide6-Essentials`, `yt-dlp`, `mutagen`,
  `imageio-ffmpeg`), the optional extra `[beets]`, the dev extra `[dev]`, the entry point
  `sclm = sclm.app:main` and the pytest configuration.
- `requirements.txt` pins the tested runtime versions. `requirements-dev.txt` adds beets,
  pytest and pytest-qt.
- We depend on **PySide6-Essentials** (QtCore, QtGui, QtWidgets) rather than the full PySide6
  meta-package, which saves hundreds of MB of WebEngine and similar modules. Use only modules
  from Essentials.
- Install: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt &&
  .venv/bin/pip install -e .`
- ffmpeg needs no system package because imageio-ffmpeg bundles 7.0.2. A system ffmpeg on
  PATH, or the path in Settings, takes precedence.
- yt-dlp must be updated regularly (`pip install -U yt-dlp`) because SoundCloud changes break
  old versions. The About or Settings view should show the yt-dlp version.
- Beets is optional and is imported lazily. When `beets_enabled` is set but beets is missing,
  the user gets a warning, not a failure.

## 17. v1 limitations and deliberate omissions

- `conflict_policy = ask` behaves like `skip`. The "Keep both" action re-downloads the track.
- There is no pause/resume. Cancel and Retry restart the job from scratch.
- Cancellation can lag during network resolve (up to the 20 s socket timeout) and during an
  ffmpeg conversion. Quitting during such a resolve closes the window at once, but the
  process exits only when the stalled read returns.
- Sign-in is only possible through a user-supplied cookies file. There is no OAuth or login
  UI.
- Artist/title duplicate detection is exact on the normalized key with ±2 s on duration. It
  does not do fuzzy string matching.
- Playlist membership reflects what yt-dlp returned. A truncated extraction can mark tracks
  as removed, and those marks clear on the next full sync.
