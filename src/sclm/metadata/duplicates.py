"""Duplicate detection (Stage 4 implements).

STUB. Signatures and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from pathlib import Path

from sclm.core.models import DuplicateMatch, TrackInfo
from sclm.core.storage import Storage

DURATION_TOLERANCE_S = 2.0


def find_duplicate(storage: Storage, track: TrackInfo) -> DuplicateMatch | None:
    """Pre-download duplicate check (no file needed). Returns the first match whose file
    exists on disk, checking in order:

    1. TRACK_ID: ``storage.completed_download_path(track.track_id)``.
    2. ARTIST_TITLE: ``storage.find_library_files(match_key=track.match_key)`` where both
       durations are known and differ by <= ``DURATION_TOLERANCE_S``. Empty match_key or an
       unknown duration never matches.

    Pure lookup: applies no policy (the caller decides) and writes nothing.
    """
    raise NotImplementedError


def audio_sha256(path: Path) -> str:
    """Hex SHA-256 of the MP3 audio payload, excluding a leading ID3v2 tag and trailing
    ID3v1 / APEv2 tags, so re-tagging a file does not change its hash."""
    raise NotImplementedError
