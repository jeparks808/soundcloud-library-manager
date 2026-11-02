"""Read/edit ID3 tags of library files for the GUI metadata editor (Stage 4 implements).

STUB. Signatures and documented behavior are the contract (docs/ARCHITECTURE.md).
Both functions do file I/O: the GUI calls them via ``run_in_background``.
"""

from __future__ import annotations

from pathlib import Path

from sclm.core.models import LibraryFile, TagData
from sclm.core.storage import Storage


def read_tags(path: Path) -> TagData:
    """Read the editable tags of an MP3 (any ID3 version). Missing tags -> empty fields.
    Raises FileSystemError if the file can't be read or isn't an MP3."""
    raise NotImplementedError


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
    raise NotImplementedError
