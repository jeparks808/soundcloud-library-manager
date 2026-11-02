"""Read/edit ID3 tags of library files for the GUI metadata editor.

Both functions do file I/O: the GUI calls them via ``run_in_background``.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from pathlib import Path

from mutagen.id3 import COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK

from sclm.core.errors import FileSystemError
from sclm.core.models import LibraryFile, LibrarySource, TagData
from sclm.core.normalize import normalize_whitespace
from sclm.core.storage import Storage
from sclm.metadata.duplicates import audio_sha256
from sclm.metadata.tagger import (
    TEXT_ENCODING, artwork_frame, load_id3, pick_comment, read_mp3, save_id3,
)

log = logging.getLogger(__name__)

_TEMP_PREFIX = ".sclm-edit-"


def read_tags(path: Path) -> TagData:
    """Read the editable tags of an MP3 (any ID3 version). Missing tags -> empty fields.
    Raises FileSystemError if the file can't be read or isn't an MP3."""
    return read_mp3(Path(path))


def _set_text(tags: ID3, frame_cls: type, value: str) -> None:
    frame_id = frame_cls.__name__
    tags.delall(frame_id)
    value = normalize_whitespace(value)
    if value:
        tags.add(frame_cls(encoding=TEXT_ENCODING, text=[value]))


def _apply(tags: ID3, data: TagData, *, artwork: bytes | None, remove_artwork: bool) -> None:
    _set_text(tags, TIT2, data.title)
    _set_text(tags, TPE1, data.artist)
    _set_text(tags, TALB, data.album)
    _set_text(tags, TPE2, data.album_artist)
    _set_text(tags, TCON, data.genre)
    for frame_id in ("TDRC", "TYER", "TDAT", "TIME"):
        tags.delall(frame_id)
    if data.year is not None and data.year > 0:
        tags.add(TDRC(encoding=TEXT_ENCODING, text=[f"{data.year:04d}"]))
    _set_text(tags, TRCK, str(data.track_number)
              if data.track_number is not None and data.track_number > 0 else "")
    # Comment: replace the frame the editor showed (and any other empty-description one).
    shown = pick_comment(tags)
    for frame in list(tags.getall("COMM")):
        if frame.desc == "" or frame is shown:
            del tags[frame.HashKey]
    comment = (data.comment or "").strip()
    if comment:
        tags.add(COMM(encoding=TEXT_ENCODING, lang="eng", desc="", text=[comment]))
    # TXXX:SOUNDCLOUD_TRACK_ID and WOAS are left untouched (read-only in TagData).
    if remove_artwork or artwork:
        tags.delall("APIC")
    if artwork:
        tags.add(artwork_frame(artwork))


def _fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_tags(path: Path, tags: TagData, storage: Storage, *,
              artwork: bytes | None = None, remove_artwork: bool = False) -> LibraryFile:
    """Write ``tags`` to ``path`` as ID3v2.3. Only called from the editor's explicit Save
    action (this is the user-confirmed path for modifying a user file).

    Safe write: copy to a hidden temp file in the same directory, tag the copy, fsync, then
    ``os.replace`` it onto ``path`` (a crash never leaves a half-written file). Empty fields
    remove their frame; read-only fields of ``tags`` are ignored and the existing
    ``TXXX:SOUNDCLOUD_TRACK_ID`` / ``WOAS`` frames are preserved. ``artwork`` replaces the
    front cover; ``remove_artwork`` deletes it. The file is never renamed or moved.

    Then upserts the ``library_files`` row (artist/title/duration/size/mtime; the audio hash
    is unchanged because it excludes tags) and returns it. Raises FileSystemError.
    """
    path = Path(path).absolute()
    read_mp3(path)   # validates: exists, readable, really an MP3
    temp = path.with_name(f"{_TEMP_PREFIX}{secrets.token_hex(6)}.tmp")
    try:
        try:
            shutil.copy2(path, temp)   # keeps permissions and timestamps
        except OSError as exc:
            raise FileSystemError(f"Could not write to {path.parent}.",
                                  detail=repr(exc)) from exc
        id3 = load_id3(temp)
        _apply(id3, tags, artwork=artwork, remove_artwork=remove_artwork)
        save_id3(id3, temp)
        try:
            with open(temp, "rb+") as fh:
                os.fsync(fh.fileno())
            os.replace(temp, path)
        except OSError as exc:
            raise FileSystemError(f"Could not save the tags of {path.name}.",
                                  detail=repr(exc)) from exc
        _fsync_dir(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not remove temp file %s: %s", temp, exc)

    info = read_mp3(path)
    st = path.stat()
    existing = storage.get_library_file(path)
    digest = existing.audio_sha256 if existing and existing.audio_sha256 else audio_sha256(path)
    storage.add_library_file(LibraryFile(
        path=path, source=existing.source if existing else LibrarySource.SCAN,
        track_id=info.track_id, artist=info.artist, title=info.title,
        duration_s=info.duration_s, audio_sha256=digest, size_bytes=st.st_size,
        mtime=st.st_mtime))
    row = storage.get_library_file(path)
    assert row is not None
    return row
