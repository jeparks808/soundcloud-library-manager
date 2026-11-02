"""Shared data models. All are immutable (frozen) so they can cross threads safely.

Update a model with ``dataclasses.replace(obj, field=value)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sclm.core.errors import ErrorInfo
from sclm.core.normalize import match_key as _match_key


# --- enums -------------------------------------------------------------------------

class JobStatus(StrEnum):
    QUEUED = "queued"                    # waiting for a worker slot
    RESOLVING = "resolving"              # fetching full track info
    DOWNLOADING = "downloading"
    CONVERTING = "converting"            # ffmpeg -> MP3
    TAGGING = "tagging"
    ORGANIZING = "organizing"            # moving into the library
    WAITING_RETRY = "waiting_retry"      # network error, backing off before the next attempt
    COMPLETED = "completed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """A worker is currently running this job (including retry backoff)."""
        return self in _ACTIVE


_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.SKIPPED_DUPLICATE,
                       JobStatus.FAILED, JobStatus.CANCELLED})
_ACTIVE = frozenset({JobStatus.RESOLVING, JobStatus.DOWNLOADING, JobStatus.CONVERTING,
                     JobStatus.TAGGING, JobStatus.ORGANIZING, JobStatus.WAITING_RETRY})


class OrganizeMode(StrEnum):
    PLAYLIST = "playlist"   # <library>/<playlist title>/<file>   (no playlist -> "Singles")
    ARTIST = "artist"       # <library>/<artist>/<file>
    ALBUM = "album"         # <library>/<artist>/<album or playlist title or "Singles">/<file>
    GENRE = "genre"         # <library>/<normalized genre or "Unknown">/<file>


class ConflictPolicy(StrEnum):
    SKIP = "skip"            # don't place the file, report as duplicate (default)
    KEEP_BOTH = "keep_both"  # place with a " (2)", " (3)" ... suffix
    ASK = "ask"              # v1: behaves like SKIP; the GUI offers "Keep both" afterwards


class DuplicateReason(StrEnum):
    TRACK_ID = "track_id"          # same SoundCloud track id already in the library
    ARTIST_TITLE = "artist_title"  # same match_key and duration within +-2 s
    CONTENT_HASH = "content_hash"  # same audio payload SHA-256
    PATH_EXISTS = "path_exists"    # destination file name already taken


class FinalizeOutcome(StrEnum):
    PLACED = "placed"              # file is now in the library at final_path
    DUPLICATE = "duplicate"        # not placed: duplicate_of holds the existing file


class LibrarySource(StrEnum):
    DOWNLOAD = "download"   # placed by SCLM
    SCAN = "scan"           # found by the library scanner


# --- tracks / playlists ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TrackInfo:
    """A SoundCloud track. Built by download/ from yt-dlp info dicts.

    ``is_partial`` is True for entries from a flat (fast) playlist extraction: only
    ``track_id`` and ``url`` are guaranteed, the rest may be empty/None.
    ``genre`` is raw, untrusted free text; use ``metadata.genre.normalize_genre``.
    """

    track_id: str
    url: str
    title: str = ""
    artist: str = ""
    duration_s: float | None = None
    genre: str | None = None
    album: str | None = None
    upload_date: str | None = None      # ISO 'YYYY-MM-DD'
    artwork_url: str | None = None      # best available artwork (prefer t500x500/original)
    description: str | None = None      # not persisted
    uploader_id: str | None = None
    playlist_index: int | None = None   # 1-based position in the source playlist, if any
    is_partial: bool = False

    @property
    def year(self) -> int | None:
        if self.upload_date and len(self.upload_date) >= 4 and self.upload_date[:4].isdigit():
            return int(self.upload_date[:4])
        return None

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or f"Track {self.track_id}"

    @property
    def match_key(self) -> str:
        return _match_key(self.artist, self.title)


@dataclass(frozen=True, slots=True)
class PlaylistInfo:
    """Result of resolving a URL. A single-track URL yields ``playlist_id=None`` and one entry."""

    playlist_id: str | None
    url: str
    title: str
    uploader: str | None = None
    entries: tuple[TrackInfo, ...] = ()

    @property
    def is_single_track(self) -> bool:
        return self.playlist_id is None


@dataclass(frozen=True, slots=True)
class PlaylistSyncResult:
    """Membership changes recorded by ``Storage.sync_playlist``."""

    added: tuple[str, ...] = ()     # track ids new (or re-added) to the playlist
    removed: tuple[str, ...] = ()   # track ids no longer in the playlist (removed_at set)


@dataclass(frozen=True, slots=True)
class PreviewEntry:
    track: TrackInfo
    existing_path: Path | None = None   # file already in the library for this track id

    @property
    def is_new(self) -> bool:
        return self.existing_path is None


@dataclass(frozen=True, slots=True)
class PlaylistPreview:
    """What the GUI shows after the user pastes a URL."""

    playlist: PlaylistInfo
    entries: tuple[PreviewEntry, ...]
    sync: PlaylistSyncResult = field(default_factory=PlaylistSyncResult)

    @property
    def new_entries(self) -> tuple[PreviewEntry, ...]:
        return tuple(e for e in self.entries if e.is_new)


# --- download queue -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Input to ``DownloadManager.enqueue``."""

    track: TrackInfo
    playlist_id: str | None = None
    playlist_title: str | None = None
    force: bool = False   # skip duplicate checks; path conflicts use KEEP_BOTH


@dataclass(frozen=True, slots=True)
class DownloadJob:
    """Snapshot of one queue entry. ``job_id`` equals the ``downloads.id`` DB row."""

    job_id: int
    track: TrackInfo
    playlist_id: str | None = None
    playlist_title: str | None = None
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0                   # attempts started so far (incl. retries)
    fraction: float | None = None       # last progress 0..1, None if unknown
    message: str = ""                   # short human status, e.g. "Retrying in 4 s"
    error: ErrorInfo | None = None      # set when FAILED (and while WAITING_RETRY)
    final_path: Path | None = None      # set when COMPLETED
    duplicate_of: Path | None = None    # set when SKIPPED_DUPLICATE
    force: bool = False


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """High-frequency progress (throttled to <= 4 events/s per job, final 100% always sent)."""

    job_id: int
    status: JobStatus                   # DOWNLOADING or CONVERTING
    fraction: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_s: float | None = None


# --- metadata results -----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    reason: DuplicateReason
    path: Path                          # existing file (verified to exist when returned)
    track_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    """Returned by ``metadata.pipeline.finalize_track``."""

    outcome: FinalizeOutcome
    final_path: Path | None = None      # set when PLACED
    audio_sha256: str | None = None     # hash of the audio payload (tags excluded)
    duplicate: DuplicateMatch | None = None   # set when DUPLICATE
    warnings: tuple[str, ...] = ()      # non-fatal, e.g. "Artwork could not be downloaded"


@dataclass(frozen=True, slots=True)
class TagData:
    """Editable ID3 fields of a library MP3 (metadata editor). '' / None = absent.

    ``track_id``, ``source_url``, ``duration_s`` and ``has_artwork`` are informational when
    reading and ignored when saving (artwork is changed via explicit save arguments).
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: int | None = None
    track_number: int | None = None
    comment: str = ""
    track_id: str | None = None         # TXXX:SOUNDCLOUD_TRACK_ID (read-only)
    source_url: str | None = None       # WOAS (read-only)
    duration_s: float | None = None     # from the audio stream (read-only)
    has_artwork: bool = False           # APIC present (read-only)


@dataclass(frozen=True, slots=True)
class ScanResult:
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    removed_missing: int = 0            # DB rows dropped because the file is gone
    errors: tuple[str, ...] = ()


# --- storage records ------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LibraryFile:
    path: Path
    source: LibrarySource
    track_id: str | None = None
    artist: str = ""
    title: str = ""
    duration_s: float | None = None
    audio_sha256: str | None = None
    size_bytes: int | None = None
    mtime: float | None = None
    added_at: str | None = None         # set by storage
    id: int | None = None               # set by storage

    @property
    def match_key(self) -> str:
        return _match_key(self.artist, self.title)


@dataclass(frozen=True, slots=True)
class PlaylistRecord:
    playlist_id: str
    url: str
    title: str
    uploader: str | None
    first_seen: str
    last_synced: str
    track_count: int = 0                # current (not removed) members


@dataclass(frozen=True, slots=True)
class PlaylistMember:
    playlist_id: str
    track: TrackInfo
    position: int | None
    first_seen: str
    removed_at: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    id: int
    track_id: str
    playlist_id: str | None
    status: JobStatus
    attempts: int
    bitrate_kbps: int | None
    file_path: Path | None
    error: ErrorInfo | None
    created_at: str
    updated_at: str
    completed_at: str | None
    title: str = ""
    artist: str = ""
