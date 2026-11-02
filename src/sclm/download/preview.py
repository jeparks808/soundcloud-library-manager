"""Playlist / track preview via yt-dlp flat extraction (ARCHITECTURE.md §4 step 2)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sclm.core.errors import InvalidUrlError, JobCancelled, SclmError, TrackUnavailableError
from sclm.core.models import PlaylistPreview, PlaylistSyncResult, PreviewEntry
from sclm.core.normalize import is_soundcloud_url
from sclm.core.paths import AppPaths
from sclm.core.settings import Settings
from sclm.core.storage import Storage
from sclm.download.enrich import enrich_playlist_info
from sclm.download.errors import classify_ytdlp_error
from sclm.download.mapping import ensure_full_audio, playlist_from_info
from sclm.download.ytdlp_opts import prepare_cookiefile, preview_opts

log = logging.getLogger(__name__)

YdlFactory = Callable[[dict[str, Any]], Any]


def _default_factory(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)  # type: ignore[arg-type]


def fetch_preview(url: str, settings: Settings, storage: Storage, *,
                  cancel: threading.Event | None = None,
                  paths: AppPaths | None = None,
                  ydl_factory: YdlFactory | None = None,
                  extra_ydl_opts: Mapping[str, Any] | None = None) -> PlaylistPreview:
    """Resolve ``url`` quickly and report which entries are already in the library.

    Blocking (network): the GUI calls it through ``gui.bridge.run_in_background``.

    1. Reject non-SoundCloud URLs (``core.normalize.is_soundcloud_url``) with InvalidUrlError.
    2. ``YoutubeDL({"extract_flat": "in_playlist", ...}).extract_info(url, download=False)``.
       Playlists/sets/albums/user track lists/likes -> one TrackInfo per entry with
       ``is_partial=True`` where fields are missing and ``playlist_index`` = 1-based position.
       A single track URL -> ``PlaylistInfo(playlist_id=None, entries=(track,))``.
       A single track that only offers Go+ preview formats -> AuthRequiredError.
       Set entries without a title are batch-looked up via ``download.enrich`` (API v2
       ``tracks?ids=``, 50 per request); entries it can't resolve keep ``title=""``.
    3. For playlists, ``storage.sync_playlist(info)`` (membership first_seen/removed_at).
    4. ``storage.existing_track_paths(ids)`` -> ``PreviewEntry.existing_path``.

    Errors: InvalidUrlError, PrivateTrackError, AuthRequiredError, GeoBlockedError,
    TrackUnavailableError, NetworkError (no automatic retry for previews; the user retries),
    ConfigError (configured cookies file missing), JobCancelled if ``cancel`` is set.

    Keyword-only test seams: ``paths`` (default ``AppPaths.from_env()``, used for the yt-dlp
    cache dir), ``ydl_factory`` (default ``yt_dlp.YoutubeDL``), ``extra_ydl_opts``.
    """
    url = (url or "").strip()
    if not is_soundcloud_url(url):
        raise InvalidUrlError(detail=f"not a SoundCloud URL: {url[:200]!r}")
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise JobCancelled()

    paths = paths or AppPaths.from_env()
    factory = ydl_factory or _default_factory
    workdir = Path(tempfile.mkdtemp(prefix="preview-", dir=_ensure_dir(paths.cache_dir)))
    try:
        cookiefile = prepare_cookiefile(settings, workdir)
        opts = preview_opts(paths.cache_dir, cookiefile, extra_ydl_opts)
        try:
            with factory(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if cancel.is_set():
                    raise JobCancelled()
                # Flat set entries have no title/artist/duration: batch-look them up
                # (best effort, never raises except JobCancelled).
                info = enrich_playlist_info(ydl, info, url, cancel=cancel)
        except SclmError:
            raise
        except Exception as exc:
            if cancel.is_set():
                raise JobCancelled() from exc
            err = classify_ytdlp_error(exc)
            log.warning("Preview of %s failed (%s): %s", url, err.category, err.detail)
            raise err from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if cancel.is_set():
        raise JobCancelled()
    if not isinstance(info, Mapping):
        raise TrackUnavailableError(detail="yt-dlp returned no information")

    playlist = playlist_from_info(info, url)
    if playlist.is_single_track:
        ensure_full_audio(info)

    sync = PlaylistSyncResult()
    if not playlist.is_single_track:
        sync = storage.sync_playlist(playlist)
    existing = storage.existing_track_paths(t.track_id for t in playlist.entries)
    entries = tuple(PreviewEntry(track=t, existing_path=existing.get(t.track_id))
                    for t in playlist.entries)
    log.info("Preview of %s: %d entries, %d new, +%d/-%d membership changes", url,
             len(entries), sum(1 for e in entries if e.is_new), len(sync.added),
             len(sync.removed))
    return PlaylistPreview(playlist=playlist, entries=entries, sync=sync)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
