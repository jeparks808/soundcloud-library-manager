"""Duplicate detection: pre-download lookup and the tag-independent audio hash."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import BinaryIO

from sclm.core.errors import FileSystemError
from sclm.core.models import DuplicateMatch, DuplicateReason, LibraryFile, TrackInfo
from sclm.core.storage import Storage
from sclm.metadata.titles import refine_artist_title

log = logging.getLogger(__name__)

DURATION_TOLERANCE_S = 2.0
_FLOAT_EPS = 1e-6           # so 180.3 vs 182.3 (= 2.0000000000000284) still counts as 2 s
_CHUNK = 1024 * 1024


def _durations_close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= DURATION_TOLERANCE_S + _FLOAT_EPS


def _existing(f: LibraryFile) -> bool:
    try:
        return Path(f.path).is_file()
    except OSError:
        return False


def find_duplicate(storage: Storage, track: TrackInfo) -> DuplicateMatch | None:
    """Pre-download duplicate check (no file needed). Returns the first match whose file
    exists on disk, checking in order:

    1. TRACK_ID: ``storage.completed_download_path(track.track_id)``.
    2. ARTIST_TITLE: ``storage.find_library_files(match_key=track.match_key)`` where both
       durations are known and differ by <= ``DURATION_TOLERANCE_S``. Empty match_key or an
       unknown duration never matches.

    Pure lookup: applies no policy (the caller decides) and writes nothing.

    ARTIST_TITLE also tries the key of the "Artist - Title" refinement
    (:func:`sclm.metadata.titles.refine_artist_title`), because files are tagged with the
    refined artist and a rescan stores that in ``library_files``.
    """
    if track.track_id:
        path = storage.completed_download_path(track.track_id)
        if path is not None:
            return DuplicateMatch(DuplicateReason.TRACK_ID, Path(path), track.track_id)

    if track.duration_s is None:
        return None
    keys: list[str] = []
    for key in (track.match_key, refine_artist_title(track).match_key):
        if key and key not in keys:
            keys.append(key)
    for key in keys:
        for f in storage.find_library_files(match_key=key):
            if _durations_close(track.duration_s, f.duration_s) and _existing(f):
                return DuplicateMatch(DuplicateReason.ARTIST_TITLE, Path(f.path), f.track_id)
    return None


def _syncsafe(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def _audio_span(fh: BinaryIO, size: int) -> tuple[int, int]:
    """Byte range ``[start, end)`` of the audio payload, excluding leading ID3v2 tag(s)
    and trailing ID3v1 / APEv2 / Lyrics3v2 tags."""
    start = 0
    while True:   # one or more ID3v2 tags at the start
        fh.seek(start)
        hdr = fh.read(10)
        if (len(hdr) == 10 and hdr[:3] == b"ID3" and hdr[3] != 0xFF and hdr[4] != 0xFF
                and all(b < 0x80 for b in hdr[6:10])):
            tag_len = 10 + _syncsafe(hdr[6:10]) + (10 if hdr[5] & 0x10 else 0)
            if start + tag_len > size:
                break
            start += tag_len
        else:
            break
    end = size
    while end > start:
        avail = end - start
        if avail >= 128:
            fh.seek(end - 128)
            if fh.read(3) == b"TAG":
                end -= 128
                continue
        if avail >= 32:
            fh.seek(end - 32)
            footer = fh.read(32)
            if footer[:8] == b"APETAGEX":
                tag_size = int.from_bytes(footer[12:16], "little")
                flags = int.from_bytes(footer[20:24], "little")
                total = tag_size + (32 if flags & 0x80000000 else 0)
                if 32 <= total <= avail:
                    end -= total
                    continue
        if avail >= 15:
            fh.seek(end - 15)
            tail = fh.read(15)
            if tail[6:] == b"LYRICS200" and tail[:6].isdigit():
                total = int(tail[:6]) + 15
                if total <= avail:
                    end -= total
                    continue
        break
    return start, end


def audio_sha256(path: Path) -> str:
    """Hex SHA-256 of the MP3 audio payload, excluding a leading ID3v2 tag and trailing
    ID3v1 / APEv2 tags, so re-tagging a file does not change its hash.
    Raises FileSystemError if the file can't be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            start, end = _audio_span(fh, size)
            fh.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise FileSystemError(f"Could not read {Path(path).name}.", detail=repr(exc)) from exc
    return digest.hexdigest()
