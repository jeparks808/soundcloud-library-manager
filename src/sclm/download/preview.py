"""Playlist / track preview via yt-dlp flat extraction (Stage 3 implements).

STUB. Signature and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import threading

from sclm.core.models import PlaylistPreview
from sclm.core.settings import Settings
from sclm.core.storage import Storage


def fetch_preview(url: str, settings: Settings, storage: Storage, *,
                  cancel: threading.Event | None = None) -> PlaylistPreview:
    """Resolve ``url`` quickly and report which entries are already in the library.

    Blocking (network): the GUI calls it through ``gui.bridge.run_in_background``.

    1. Reject non-SoundCloud URLs (``core.normalize.is_soundcloud_url``) with InvalidUrlError.
    2. ``YoutubeDL({"extract_flat": "in_playlist", ...}).extract_info(url, download=False)``.
       Playlists/sets/albums/user track lists/likes -> one TrackInfo per entry with
       ``is_partial=True`` where fields are missing and ``playlist_index`` = 1-based position.
       A single track URL -> ``PlaylistInfo(playlist_id=None, entries=(track,))``.
    3. For playlists, ``storage.sync_playlist(info)`` (membership first_seen/removed_at).
    4. ``storage.existing_track_paths(ids)`` -> ``PreviewEntry.existing_path``.

    Errors: InvalidUrlError, PrivateTrackError, AuthRequiredError, GeoBlockedError,
    TrackUnavailableError, NetworkError (no automatic retry for previews; the user retries),
    JobCancelled if ``cancel`` is set.
    """
    raise NotImplementedError
