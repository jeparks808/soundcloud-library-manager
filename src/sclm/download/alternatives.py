"""Manual alternative audio sources for failed downloads (Alternatives page, 0.4.0).

For a track SoundCloud won't let us download (typically ``drm_protected``), the user can
search YouTube, compare candidates by length against the SoundCloud track, and pick one to
download. Nothing here downloads on its own: the user chooses each candidate, and the job
then runs through the normal ``DownloadManager`` pipeline with
``DownloadRequest.audio_source_url`` set. Audio comes from the chosen video; title, artist,
artwork and duplicate checks come from the SoundCloud track.

DRM-protected sources are never downloaded (``download_opts`` forces
``allow_unplayable_formats=False``; yt-dlp then fails with "DRM protected").

Pure Python, no Qt. Search and metadata lookups are network calls: run them off the GUI
thread.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import JobStatus, TrackInfo
from sclm.core.normalize import normalize_whitespace
from sclm.core.storage import Storage
from sclm.download.enrich import _ytdlp_fetcher, api_track_to_info
from sclm.download.mapping import trackinfo_from_info
from sclm.download.ytdlp_opts import common_opts

log = logging.getLogger(__name__)

SEARCH_LIMIT = 6
MATCH_TOLERANCE_S = 3.0      # |Δ| <= 3 s: same recording, most likely
CLOSE_TOLERANCE_S = 10.0     # |Δ| <= 10 s: maybe (intro/outro trimmed, different upload)
# Failure categories that a different audio source can help with. Network, file-system and
# FFmpeg failures are retried in place instead.
ALTERNATIVE_CATEGORIES = frozenset({ErrorCategory.DRM_PROTECTED, ErrorCategory.AUTH_REQUIRED,
                                    ErrorCategory.UNAVAILABLE, ErrorCategory.GEO_BLOCKED})

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com",
                            "music.youtube.com"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

YdlFactory = Callable[[dict[str, Any]], Any]


class MatchLevel(StrEnum):
    MATCH = "match"
    CLOSE = "close"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One YouTube search result, compared against the SoundCloud track's length."""

    video_id: str
    url: str
    title: str
    channel: str = ""
    channel_verified: bool = False
    duration_s: float | None = None
    view_count: int | None = None
    delta_s: float | None = None        # candidate length - SoundCloud length
    match: MatchLevel = MatchLevel.UNKNOWN


@dataclass(frozen=True, slots=True)
class FailedTrack:
    """The newest failed download of a track that still has no file in the library."""

    track: TrackInfo
    error: ErrorInfo
    download_id: int


def youtube_video_id(url: str) -> str | None:
    """The video id of an https YouTube video URL (watch, shorts, youtu.be), else None."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path
    candidate: str | None = None
    if host == "youtu.be":
        candidate = path.strip("/")
    elif host in _YOUTUBE_HOSTS:
        if path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        elif path.startswith("/shorts/"):
            candidate = path.removeprefix("/shorts/").strip("/")
    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def is_youtube_video_url(url: str) -> bool:
    return youtube_video_id(url) is not None


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def search_query(track: TrackInfo) -> str:
    return normalize_whitespace(f"{track.artist} {track.title}")


def match_level(delta_s: float | None) -> MatchLevel:
    if delta_s is None:
        return MatchLevel.UNKNOWN
    if abs(delta_s) <= MATCH_TOLERANCE_S:
        return MatchLevel.MATCH
    if abs(delta_s) <= CLOSE_TOLERANCE_S:
        return MatchLevel.CLOSE
    return MatchLevel.DIFFERENT


def candidate_from_entry(entry: Mapping[str, Any], reference_s: float | None
                         ) -> Candidate | None:
    """Map one flat ``ytsearch`` entry; None for anything that isn't a plain video."""
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not _VIDEO_ID.match(video_id):
        return None
    if entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    duration = entry.get("duration")
    duration_s = float(duration) if isinstance(duration, (int, float)) \
        and not isinstance(duration, bool) and duration > 0 else None
    delta = duration_s - reference_s if duration_s is not None and reference_s else None
    views = entry.get("view_count")
    channel = entry.get("channel") or entry.get("uploader") or ""
    return Candidate(
        video_id=video_id, url=video_url(video_id),
        title=normalize_whitespace(str(entry.get("title") or "")),
        channel=normalize_whitespace(str(channel)),
        channel_verified=bool(entry.get("channel_is_verified")),
        duration_s=duration_s,
        view_count=views if isinstance(views, int) and not isinstance(views, bool) else None,
        delta_s=delta, match=match_level(delta))


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Closest length first (unknown last); verified channels first among equals."""
    order = {MatchLevel.MATCH: 0, MatchLevel.CLOSE: 1, MatchLevel.DIFFERENT: 2,
             MatchLevel.UNKNOWN: 3}
    return sorted(candidates, key=lambda c: (
        order[c.match], abs(c.delta_s) if c.delta_s is not None else 0.0,
        not c.channel_verified))


def _default_ydl_factory(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def search_youtube(query: str, *, reference_s: float | None, cache_dir: Path,
                   limit: int = SEARCH_LIMIT, ydl_factory: YdlFactory | None = None
                   ) -> list[Candidate]:
    """Flat YouTube search (one request, no per-video page loads), ranked by length match.

    Raises whatever yt-dlp raises (classify with ``classify_ytdlp_error``).
    """
    query = normalize_whitespace(query)
    if not query:
        return []
    opts = common_opts(cache_dir) | {"extract_flat": "in_playlist", "skip_download": True}
    with (ydl_factory or _default_ydl_factory)(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max(1, limit)}:{query}", download=False)
    entries = info.get("entries") if isinstance(info, Mapping) else None
    found = [c for e in entries or [] if isinstance(e, Mapping)
             if (c := candidate_from_entry(e, reference_s)) is not None]
    return rank(found)


class Availability(StrEnum):
    AUDIO = "audio"              # a non-DRM audio stream exists: downloadable
    DRM_ONLY = "drm_only"        # every stream is DRM-protected: never downloaded
    UNAVAILABLE = "unavailable"  # removed, private, sign-in/age gate, no formats, ...


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """Result of :func:`check_source` for one candidate video."""

    availability: Availability
    note: str = ""               # e.g. "Opus 147 kbps", or the reason it is unavailable

    @property
    def downloadable(self) -> bool:
        return self.availability == Availability.AUDIO


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def availability_from_formats(formats: Any) -> SourceCheck:
    """Classify a video's raw (unprocessed) yt-dlp formats.

    DRM_ONLY when formats exist but every one with audio is DRM-protected; AUDIO with the
    best non-DRM audio-only stream's codec and bitrate otherwise.
    """
    audio = [f for f in formats or [] if isinstance(f, Mapping)
             and f.get("acodec") not in ("none",)]
    if not audio:
        return SourceCheck(Availability.UNAVAILABLE, "no audio streams")
    clear = [f for f in audio if not f.get("has_drm")]
    if not clear:
        return SourceCheck(Availability.DRM_ONLY, "DRM only")
    audio_only = [f for f in clear if f.get("vcodec") == "none"] or clear
    best = max(audio_only, key=lambda f: _num(f.get("abr")) or _num(f.get("tbr")) or 0.0)
    codec = str(best.get("acodec") or best.get("ext") or "audio").split(".")[0]
    codec = {"mp4a": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3"}.get(
        codec.lower(), codec)
    kbps = _num(best.get("abr")) or _num(best.get("tbr"))
    return SourceCheck(Availability.AUDIO, f"{codec} {round(kbps)} kbps" if kbps else codec)


def check_source(url: str, *, cache_dir: Path, ydl_factory: YdlFactory | None = None
                 ) -> SourceCheck:
    """Look at one candidate's formats without downloading anything (one page load)."""
    from sclm.download.errors import classify_ytdlp_error

    opts = common_opts(cache_dir) | {"skip_download": True, "noplaylist": True,
                                     "allow_unplayable_formats": False}
    try:
        with (ydl_factory or _default_ydl_factory)(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a label
        err = classify_ytdlp_error(exc)
        if err.category == ErrorCategory.DRM_PROTECTED:
            return SourceCheck(Availability.DRM_ONLY, "DRM only")
        return SourceCheck(Availability.UNAVAILABLE, err.user_message)
    if not isinstance(info, Mapping):
        return SourceCheck(Availability.UNAVAILABLE, "no information")
    return availability_from_formats(info.get("formats"))


def failed_tracks(storage: Storage, *, limit: int = 1000) -> list[FailedTrack]:
    """Tracks that failed for a reason another source can help with and whose newest outcome
    is still a failure (no COMPLETED/duplicate download since, no file in the library).

    A later failure of another kind (say, a network error during an alternative-source
    attempt) keeps the track listed with its original reason. Newest first.
    """
    records = storage.download_history(limit=limit, statuses=(
        JobStatus.FAILED, JobStatus.COMPLETED, JobStatus.SKIPPED_DUPLICATE))
    latest: dict[str, Any] = {}
    reason: dict[str, Any] = {}
    for rec in records:                     # newest first
        latest.setdefault(rec.track_id, rec)
        if rec.status == JobStatus.FAILED and rec.error is not None \
                and rec.error.category in ALTERNATIVE_CATEGORIES:
            reason.setdefault(rec.track_id, rec)
    failed = [reason[tid] for tid, rec in latest.items()
              if rec.status == JobStatus.FAILED and tid in reason]
    present = storage.existing_track_paths([rec.track_id for rec in failed])
    result = []
    for rec in failed:
        if rec.track_id in present:
            continue
        track = storage.get_track(rec.track_id)
        if track is not None:
            result.append(FailedTrack(track=track, error=rec.error, download_id=rec.id))
    return result


def source_note(source_url: str) -> str:
    """ID3 comment for files whose audio came from an alternative source."""
    return (f"Audio from YouTube: {source_url} (SoundCloud only offered a stream SCLM can't "
            "download; lossy source re-encoded to MP3)")


def with_soundcloud_metadata(ydl: Any, track: TrackInfo) -> TrackInfo:
    """Fill a partial track from SoundCloud's API (metadata only, never the stream).

    Best effort: on any failure the stored track is used as it is.
    """
    if not track.is_partial and track.artwork_url and track.duration_s:
        return track
    try:
        [obj] = [o for o in _ytdlp_fetcher(ydl, None, None)([track.track_id])
                 if isinstance(o, Mapping) and str(o.get("id")) == track.track_id]
        info = api_track_to_info(obj)
        info.setdefault("webpage_url", track.url)
        fresh = trackinfo_from_info(info, partial=False)
    except Exception as exc:  # noqa: BLE001 - metadata is optional here
        log.info("SoundCloud metadata lookup for %s failed (%s); using stored data",
                 track.track_id, exc)
        return track
    return replace(track, title=fresh.title or track.title, artist=fresh.artist or track.artist,
                   duration_s=fresh.duration_s or track.duration_s,
                   genre=fresh.genre or track.genre, upload_date=fresh.upload_date
                   or track.upload_date, artwork_url=fresh.artwork_url or track.artwork_url,
                   uploader_id=fresh.uploader_id or track.uploader_id, is_partial=False)
