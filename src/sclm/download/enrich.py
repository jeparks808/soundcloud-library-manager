"""Fill in title/artist/duration for bare flat playlist entries.

With ``extract_flat="in_playlist"`` yt-dlp builds SoundCloud *set* entries with
``url_result(...)`` only (``SoundcloudPlaylistBaseIE._extract_set``): each entry carries
``id``, ``url``, ``ie_key`` and the album fields, but no title, uploader or duration. User
lists (tracks, likes, ...) already carry that data.

This module looks up the missing tracks in batches through SoundCloud's API v2
``tracks?ids=<id,id,...>`` endpoint (max 50 ids per request), using yt-dlp's own SoundCloud
extractor so client_id discovery, headers and the user's optional cookies behave exactly as
during extraction. A 500-track set costs about 10 requests.

It is best effort: anything that goes wrong (network error, API change, private/deleted
tracks missing from the response) is logged and the affected entries stay bare, so the
preview shows ``Track <id>`` for them and the download step resolves them in full. Bare
entries keep ``title == ""``; the placeholder text is a display fallback only.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sclm.core.errors import JobCancelled
from sclm.download.mapping import is_track_entry

log = logging.getLogger(__name__)

API_BATCH_SIZE = 50
# Give up after this many failed requests in a row (each can take up to the socket timeout).
MAX_CONSECUTIVE_FAILURES = 2
_DEFAULT_API_V2_BASE = "https://api-v2.soundcloud.com/"
_SET_EXTRACTORS = frozenset({"SoundcloudSet", "SoundcloudPlaylist"})
# Never taken from the API object: identity, the URL yt-dlp resolves at download time, and
# the playlist-derived album fields.
_FLAT_ONLY_KEYS = frozenset({"_type", "ie_key", "id", "url", "album", "album_artist",
                             "album_artists", "album_type", "playlist_index"})
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

FetchTracks = Callable[[Sequence[str]], list[Any]]


def needs_enrichment(entry: Any) -> bool:
    """A flat track entry with an id but no title."""
    if not isinstance(entry, Mapping) or not is_track_entry(entry):
        return False
    if _entry_id(entry) is None:
        return False
    title = entry.get("title") or entry.get("track")
    return not (isinstance(title, str) and title.strip())


def secret_token(url: str) -> str | None:
    """The secret-share token of a private set/track URL, if any.

    ``https://soundcloud.com/<user>/sets/<slug>/<token>`` (as matched by yt-dlp's
    SoundcloudSetIE) or a ``secret_token=`` query parameter (API URLs).
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    tokens = parse_qs(parts.query).get("secret_token")
    if tokens and tokens[0].strip():
        return tokens[0].strip()
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) == 4 and segments[1] == "sets":
        return segments[3]
    return None


def api_track_to_info(obj: Mapping[str, Any]) -> dict[str, Any]:
    """Map a raw API v2 track object to the yt-dlp info-dict keys our mapping reads.

    Mirrors ``SoundcloudBaseIE._extract_info_dict`` for the fields we use, without its
    per-track HEAD request for the "original" artwork.
    """
    user = obj.get("user") if isinstance(obj.get("user"), Mapping) else {}
    info: dict[str, Any] = {"id": str(obj["id"])}

    def put(key: str, value: Any) -> None:
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, "", []):
            info[key] = value

    put("title", obj.get("title") if isinstance(obj.get("title"), str) else None)
    put("uploader", user.get("username") if isinstance(user.get("username"), str) else None)
    uid = user.get("id")
    put("uploader_id", str(uid) if isinstance(uid, (int, str)) and not isinstance(uid, bool)
        else None)
    duration = obj.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
        info["duration"] = duration / 1000.0
    permalink = obj.get("permalink_url")
    if isinstance(permalink, str) and permalink.startswith(("http://", "https://")):
        info["webpage_url"] = permalink
    put("genre", obj.get("genre") if isinstance(obj.get("genre"), str) else None)
    put("description", obj.get("description") if isinstance(obj.get("description"), str)
        else None)
    created = obj.get("created_at")
    if isinstance(created, str) and _ISO_DATE_RE.match(created):
        info["upload_date"] = created[:10]
    artwork = obj.get("artwork_url") or user.get("avatar_url")
    if isinstance(artwork, str) and artwork.startswith(("http://", "https://")):
        info["thumbnail"] = artwork
    publisher = obj.get("publisher_metadata")
    if isinstance(publisher, Mapping):
        artist = publisher.get("artist")
        if isinstance(artist, str) and artist.strip():
            info["artists"] = [artist.strip()]
    return info


def merge_entry(flat: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    """Fill gaps in a flat entry from ``meta``; values the flat entry has always win.

    ``webpage_url`` is only added for entries whose URL has no secret token (a private
    track's permalink without the token would not resolve at download time).
    """
    merged = dict(flat)
    has_token = "secret_token" in str(flat.get("url") or "")
    for key, value in meta.items():
        if key in _FLAT_ONLY_KEYS or value in (None, "", []):
            continue
        if key == "webpage_url" and has_token:
            continue
        if merged.get(key) in (None, "", []):
            merged[key] = value
    return merged


def enrich_playlist_info(ydl: Any, info: Any, url: str, *,
                         cancel: threading.Event | None = None,
                         batch_size: int = API_BATCH_SIZE,
                         fetch: FetchTracks | None = None) -> Any:
    """Return ``info`` with title-less track entries filled in (a new dict if anything
    changed). Order and the flat entries' own fields (album, url, ...) are preserved.

    Never raises except :class:`JobCancelled` (checked between batches). ``fetch`` is a test
    seam replacing the yt-dlp API call.
    """
    if not isinstance(info, Mapping) or info.get("_type") not in ("playlist", "multi_video"):
        return info
    raw_entries = info.get("entries")
    if not raw_entries:
        return info
    try:
        entries = list(raw_entries)
    except Exception as exc:  # noqa: BLE001 - a lazy entries iterator failing is not fatal
        log.warning("Could not read playlist entries for metadata lookup: %r", exc)
        return info
    missing = list(dict.fromkeys(_entry_id(e) for e in entries if needs_enrichment(e)))
    if not missing:
        return info if isinstance(raw_entries, list) else {**info, "entries": entries}

    found: dict[str, dict[str, Any]] = {}
    try:
        if fetch is None:
            token = _find_token(url, entries)
            fetch = _ytdlp_fetcher(ydl, _playlist_id(info), token)
        found = _fetch_all(fetch, missing, batch_size, cancel)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - enrichment must never fail the preview
        log.warning("Track metadata lookup unavailable (%s: %s); %d playlist entries keep "
                    "placeholder titles", type(exc).__name__, exc, len(missing))
        return {**info, "entries": entries}

    unresolved = [i for i in missing if i not in found]
    if unresolved:
        log.warning("%d of %d playlist tracks could not be looked up (deleted, private, "
                    "unavailable or lookup failed); they keep placeholder titles: %s",
                    len(unresolved), len(missing),
                    ", ".join(unresolved[:10]) + (" ..." if len(unresolved) > 10 else ""))
    new_entries = [merge_entry(e, found[_entry_id(e)])  # type: ignore[index]
                   if needs_enrichment(e) and _entry_id(e) in found else e
                   for e in entries]
    log.info("Looked up metadata for %d of %d bare playlist entries", len(found), len(missing))
    return {**info, "entries": new_entries}


# --- internals ------------------------------------------------------------------------------

def _entry_id(entry: Mapping[str, Any]) -> str | None:
    raw = entry.get("id")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    text = str(raw).strip()
    return text if text.isdigit() else None


def _batches(ids: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    size = max(1, size)
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def _fetch_all(fetch: FetchTracks, ids: Sequence[str], batch_size: int,
               cancel: threading.Event | None) -> dict[str, dict[str, Any]]:
    wanted = set(ids)
    found: dict[str, dict[str, Any]] = {}
    failures = 0
    for batch in _batches(ids, batch_size):
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
        try:
            objects = fetch(batch)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.warning("Track metadata lookup for %d tracks failed (%s: %s)", len(batch),
                        type(exc).__name__, exc)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.warning("Giving up track metadata lookup after %d failed requests",
                            failures)
                break
            continue
        failures = 0
        for obj in objects or ():
            if not isinstance(obj, Mapping) or obj.get("id") is None:
                continue
            track_id = str(obj["id"]).strip()
            if track_id in wanted:
                try:
                    found[track_id] = api_track_to_info(obj)
                except Exception as exc:  # noqa: BLE001 - one odd object must not matter
                    log.debug("Unusable API object for track %s: %r", track_id, exc)
    if cancel is not None and cancel.is_set():
        raise JobCancelled()
    return found


def _playlist_id(info: Mapping[str, Any]) -> str | None:
    if str(info.get("extractor_key") or "") not in _SET_EXTRACTORS:
        return None
    raw = info.get("id")
    return str(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) else None


def _find_token(url: str, entries: Sequence[Any]) -> str | None:
    token = secret_token(url)
    if token:
        return token
    for entry in entries:
        if isinstance(entry, Mapping):
            token = secret_token(str(entry.get("url") or ""))
            if token:
                return token
    return None


def _ytdlp_fetcher(ydl: Any, playlist_id: str | None, token: str | None) -> FetchTracks:
    """``ids -> [API track objects]`` through yt-dlp's (private) SoundCloud client.

    Raises if the yt-dlp internals we rely on are missing (an API change), which the caller
    turns into a warning.
    """
    ie = ydl.get_info_extractor("Soundcloud")
    call_api = getattr(ie, "_call_api", None)
    if not callable(call_api):
        raise AttributeError("yt-dlp SoundcloudIE has no _call_api")
    ie.initialize()  # cached client_id (or discovery) and cookie login, as yt-dlp does
    base = getattr(ie, "_API_V2_BASE", None) or _DEFAULT_API_V2_BASE
    headers = getattr(ie, "_HEADERS", None)

    def fetch(ids: Sequence[str]) -> list[Any]:
        query = {"ids": ",".join(ids)}
        if token and playlist_id:
            query.update(playlistId=playlist_id, playlistSecretToken=token)
        kwargs: dict[str, Any] = {"query": query}
        if isinstance(headers, Mapping):
            kwargs["headers"] = dict(headers)
        data = call_api(base + "tracks", playlist_id or "tracks",
                        f"Downloading metadata of {len(ids)} tracks", **kwargs)
        if isinstance(data, Mapping):
            data = data.get("collection")
        if not isinstance(data, list):
            raise ValueError(f"unexpected tracks response: {type(data).__name__}")
        return data

    return fetch
