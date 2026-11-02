"""yt-dlp info dict -> :class:`TrackInfo` / :class:`PlaylistInfo` mapping.

Works on two shapes of SoundCloud data:

* full track info (``extract_info`` without ``extract_flat``): id, title, uploader,
  duration, genres, upload_date, thumbnails, formats, ...
* flat playlist entries (``extract_flat="in_playlist"``): ``{"_type": "url", "id", "url",
  "ie_key", ...}`` and sometimes title/album fields. These become ``is_partial=True``.

Also detects Go+ "preview only" (30 s snippet) results, which must never be saved as if
they were the full track (ARCHITECTURE.md §1).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sclm.core.errors import AuthRequiredError, TrackUnavailableError
from sclm.core.models import PlaylistInfo, TrackInfo
from sclm.core.normalize import normalize_whitespace
from sclm.download.errors import PREVIEW_ONLY_MESSAGE

log = logging.getLogger(__name__)

# SoundCloud artwork size suffixes, best first for an ID3 cover (t500x500 is a sane size;
# "original" can be several MB).
_ARTWORK_PREFERENCE = ("t500x500", "original", "crop", "t300x300", "large")
_ARTWORK_SIZE_RE = re.compile(
    r"-(?:large|t\d+x\d+|crop|small|badge|tiny|mini|original)(?=\.(?:jpg|jpeg|png)(?:$|\?))",
    re.I)
_PREVIEW_URL_RE = re.compile(r"/preview/|/(?:preview|playlist)/0/30/", re.I)
# SoundCloud set types that denote a real album/EP rather than a user playlist.
_PLAYLIST_SET_TYPES = frozenset({"", "playlist"})
_SET_EXTRACTORS = frozenset({"SoundcloudSet", "SoundcloudPlaylist"})


def _str(value: Any) -> str:
    return normalize_whitespace(value) if isinstance(value, str) else ""


def _opt_str(value: Any) -> str | None:
    text = _str(value)
    return text or None


def _id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        text = str(value).strip()
        return text or None
    return None


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _iso_date(info: Mapping[str, Any]) -> str | None:
    raw = info.get("upload_date")
    if isinstance(raw, str) and re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    ts = info.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(ts, UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _artist(info: Mapping[str, Any]) -> str:
    """Publisher-metadata artist when SoundCloud has one, else the uploader's name."""
    artists = info.get("artists")
    if isinstance(artists, list):
        names = [_str(a) for a in artists if _str(a)]
        if names:
            return ", ".join(names)
    for key in ("artist", "uploader", "creator", "channel"):
        text = _str(info.get(key))
        if text:
            return text
    return ""


def _genre(info: Mapping[str, Any]) -> str | None:
    genre = _opt_str(info.get("genre"))
    if genre:
        return genre
    genres = info.get("genres")
    if isinstance(genres, list):
        for g in genres:
            if _str(g):
                return _str(g)
    return None


def _album(info: Mapping[str, Any]) -> str | None:
    """Only real albums/EPs count; a user playlist title is not an album."""
    album = _opt_str(info.get("album"))
    if not album:
        return None
    album_type = info.get("album_type")
    if isinstance(album_type, str) and album_type.strip().lower() in _PLAYLIST_SET_TYPES:
        return None
    return album


def upgrade_artwork_url(url: str) -> str:
    """Rewrite a SoundCloud artwork URL (``-large.jpg`` etc.) to the ``-t500x500`` size."""
    if "sndcdn.com" not in url or "-original." in url:
        return url
    return _ARTWORK_SIZE_RE.sub("-t500x500", url, count=1)


def choose_artwork_url(info: Mapping[str, Any]) -> str | None:
    """Best artwork URL: ``t500x500``, then ``original``, then the largest other thumbnail,
    then ``thumbnail`` (rewritten from ``-large`` to ``-t500x500``)."""
    thumbs = info.get("thumbnails")
    candidates: list[Mapping[str, Any]] = []
    if isinstance(thumbs, list):
        candidates = [t for t in thumbs if isinstance(t, Mapping) and _str(t.get("url"))]
    by_id = {str(t.get("id")): t for t in candidates if t.get("id") is not None}
    for wanted in _ARTWORK_PREFERENCE:
        if wanted in by_id:
            return _str(by_id[wanted]["url"])
    if candidates:
        def size(t: Mapping[str, Any]) -> tuple[float, float]:
            w = t.get("width") or 0
            pref = t.get("preference") or 0
            return (float(w) if isinstance(w, (int, float)) else 0.0,
                    float(pref) if isinstance(pref, (int, float)) else 0.0)
        best = max(candidates, key=size)
        return upgrade_artwork_url(_str(best["url"]))
    for key in ("thumbnail", "artwork_url"):
        url = _str(info.get(key))
        if url:
            return upgrade_artwork_url(url)
    return None


def _track_url(info: Mapping[str, Any]) -> str:
    for key in ("webpage_url", "original_url", "permalink_url", "url"):
        url = _str(info.get(key))
        if url.startswith(("http://", "https://", "file://")):
            return url
    return ""


def trackinfo_from_info(info: Mapping[str, Any], *, playlist_index: int | None = None,
                        partial: bool | None = None) -> TrackInfo:
    """Map one yt-dlp info dict (full or flat entry) to :class:`TrackInfo`.

    ``partial=None`` decides automatically: flat entries (``_type == "url"``) and dicts missing
    title, artist or duration are partial. Raises :class:`TrackUnavailableError` if the dict
    has no usable id or URL.
    """
    track_id = _id(info.get("id"))
    url = _track_url(info)
    if not track_id or not url:
        raise TrackUnavailableError(detail=f"info dict without id/url: id={track_id!r}")
    title = _str(info.get("title")) or _str(info.get("track"))
    artist = _artist(info)
    duration = _duration(info.get("duration"))
    if partial is None:
        partial = (info.get("_type") in ("url", "url_transparent")
                   or not title or not artist or duration is None)
    description = info.get("description")
    return TrackInfo(
        track_id=track_id,
        url=url,
        title=title,
        artist=artist,
        duration_s=duration,
        genre=_genre(info),
        album=_album(info),
        upload_date=_iso_date(info),
        artwork_url=choose_artwork_url(info),
        description=description if isinstance(description, str) and description else None,
        uploader_id=_id(info.get("uploader_id")),
        playlist_index=playlist_index,
        is_partial=bool(partial),
    )


def merge_resolved(queued: TrackInfo, resolved: TrackInfo) -> TrackInfo:
    """Combine the (possibly partial) queued track with freshly resolved full info.

    The queued ``track_id`` is authoritative (it is the DB key of the job). Non-empty
    resolved values win; values only the queued record knows (album from the playlist,
    ``playlist_index``) are kept.
    """
    if resolved.track_id != queued.track_id:
        log.warning("Resolved track id %s differs from queued id %s; keeping the queued id",
                    resolved.track_id, queued.track_id)

    def pick(new: Any, old: Any) -> Any:
        return new if new not in (None, "") else old

    return TrackInfo(
        track_id=queued.track_id,
        url=pick(queued.url, resolved.url),
        title=pick(resolved.title, queued.title),
        artist=pick(resolved.artist, queued.artist),
        duration_s=pick(resolved.duration_s, queued.duration_s),
        genre=pick(resolved.genre, queued.genre),
        album=pick(resolved.album, queued.album),
        upload_date=pick(resolved.upload_date, queued.upload_date),
        artwork_url=pick(resolved.artwork_url, queued.artwork_url),
        description=pick(resolved.description, queued.description),
        uploader_id=pick(resolved.uploader_id, queued.uploader_id),
        playlist_index=queued.playlist_index if queued.playlist_index is not None
        else resolved.playlist_index,
        is_partial=False,
    )


# --- formats ---------------------------------------------------------------------------

def is_preview_format(fmt: Mapping[str, Any]) -> bool:
    """True for SoundCloud's 30-second Go+ snippet formats."""
    format_id = str(fmt.get("format_id") or "").lower()
    if "preview" in format_id.split("_") or format_id.endswith("preview"):
        return True
    url = str(fmt.get("url") or "")
    return bool(_PREVIEW_URL_RE.search(url))


def ensure_full_audio(info: Mapping[str, Any]) -> None:
    """Raise if the resolved track can't be downloaded in full.

    * no formats at all -> :class:`TrackUnavailableError`
    * only preview (snipped) formats, or yt-dlp selected a preview format ->
      :class:`AuthRequiredError` (Go+ track; never saved as a full track)
    """
    formats = info.get("formats")
    if formats is None:
        # Direct media (e.g. file:// in tests) has url/ext on the info itself.
        if info.get("url"):
            formats = [info]
        else:
            raise TrackUnavailableError(detail="no formats in resolved info")
    formats = [f for f in formats if isinstance(f, Mapping)]
    if not formats:
        raise TrackUnavailableError(detail="no formats in resolved info")
    if all(is_preview_format(f) for f in formats):
        raise AuthRequiredError(PREVIEW_ONLY_MESSAGE,
                                detail=f"only preview formats: "
                                       f"{[f.get('format_id') for f in formats]}")
    selected = info.get("requested_formats") or info.get("requested_downloads") or []
    for f in selected:
        if isinstance(f, Mapping) and is_preview_format(f):
            raise AuthRequiredError(PREVIEW_ONLY_MESSAGE,
                                    detail=f"selected preview format {f.get('format_id')}")


# --- playlists -------------------------------------------------------------------------

def is_track_entry(entry: Mapping[str, Any]) -> bool:
    """Flat entries can be sets (e.g. reposted playlists on a user page); keep tracks only."""
    ie_key = entry.get("ie_key") or entry.get("extractor_key")
    if ie_key:
        return ie_key == "Soundcloud"
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    path = urlsplit(url).path
    return "/sets/" not in path and "/playlists/" not in path


def playlist_key(info: Mapping[str, Any], url: str) -> str:
    """Stable ``playlist_id`` for the DB.

    Sets/albums use SoundCloud's numeric playlist id. Other lists (user tracks, likes,
    reposts, ...) all carry the *user* id in yt-dlp, so they are namespaced by extractor and
    resource to stop ``/likes`` and ``/tracks`` of one user overwriting each other's
    membership.
    """
    raw_id = _id(info.get("id")) or ""
    extractor = str(info.get("extractor_key") or "")
    if extractor in _SET_EXTRACTORS or not extractor:
        return raw_id or url
    segments = [s for s in urlsplit(url).path.split("/") if s]
    resource = segments[1].lower() if len(segments) >= 2 else "all"
    return f"{extractor}:{raw_id}:{resource}"


def playlist_from_info(info: Mapping[str, Any], url: str) -> PlaylistInfo:
    """Map a yt-dlp result for ``url`` to :class:`PlaylistInfo`.

    Playlist results -> one partial-or-full TrackInfo per track entry (1-based
    ``playlist_index`` = position in the playlist, counting skipped non-track entries).
    A single track -> ``playlist_id=None`` with one full entry.
    """
    kind = info.get("_type") or "video"
    if kind in ("playlist", "multi_video"):
        entries: list[TrackInfo] = []
        seen: set[str] = set()
        raw_entries = info.get("entries") or []
        for position, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, Mapping) or not is_track_entry(entry):
                continue
            index = entry.get("playlist_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                index = position
            try:
                track = trackinfo_from_info(entry, playlist_index=index)
            except TrackUnavailableError:
                log.warning("Skipping playlist entry %d without id/url", position)
                continue
            if track.track_id in seen:
                continue
            seen.add(track.track_id)
            entries.append(track)
        pid = playlist_key(info, url)
        title = _str(info.get("title")) or pid
        return PlaylistInfo(playlist_id=pid, url=_str(info.get("webpage_url")) or url,
                            title=title, uploader=_opt_str(info.get("uploader")),
                            entries=tuple(entries))
    if kind in ("url", "url_transparent"):
        # An unresolved reference (shouldn't happen for a top-level result).
        track = trackinfo_from_info(info)
    else:
        track = trackinfo_from_info(info, partial=False if info.get("title") else None)
    return PlaylistInfo(playlist_id=None, url=track.url or url, title=track.display_name,
                        uploader=track.artist or None, entries=(track,))
