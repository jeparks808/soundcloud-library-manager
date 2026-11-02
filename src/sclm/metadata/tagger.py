"""Mutagen ID3 helpers: write a downloaded track's tags, read tags back.

Tags are saved as ID3v2.3 (``v2_version=3``) with any ID3v1 tag removed; see
docs/ARCHITECTURE.md §12 for the frame list.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import (
    APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX, WOAS,
    ID3NoHeaderError, ID3v1SaveOptions, PictureType,
)
from mutagen.mp3 import MP3

from sclm.core.errors import FileSystemError
from sclm.core.models import TagData, TrackInfo
from sclm.core.normalize import normalize_whitespace
from sclm.metadata.artwork import image_mime
from sclm.metadata.genre import UNKNOWN_GENRE

log = logging.getLogger(__name__)

TXXX_TRACK_ID = "SOUNDCLOUD_TRACK_ID"
COMMENT_MAX_CHARS = 1000
TEXT_ENCODING = 3   # UTF-8; mutagen converts to UTF-16 when writing v2.3


def save_id3(tags: ID3, path: Path) -> None:
    """Save as ID3v2.3 (v2.4-only frames converted, e.g. TDRC -> TYER), removing ID3v1.
    ``path`` must already exist (mutagen would otherwise create it). Raises
    FileSystemError."""
    if not Path(path).is_file():
        raise FileSystemError(f"{Path(path).name} does not exist.", detail=str(path))
    try:
        tags.update_to_v23()
        tags.save(str(path), v2_version=3, v1=ID3v1SaveOptions.REMOVE)
    except (MutagenError, OSError, ValueError) as exc:
        raise FileSystemError(f"Could not write tags to {Path(path).name}.",
                              detail=repr(exc)) from exc


def load_id3(path: Path) -> ID3:
    """Existing ID3 tag of ``path`` or an empty one. Raises FileSystemError."""
    try:
        return ID3(str(path))
    except ID3NoHeaderError:
        return ID3()
    except (MutagenError, OSError) as exc:
        raise FileSystemError(f"Could not read tags of {Path(path).name}.",
                              detail=repr(exc)) from exc


def artwork_frame(data: bytes) -> APIC:
    return APIC(encoding=TEXT_ENCODING, mime=image_mime(data) or "image/jpeg",
                type=PictureType.COVER_FRONT, desc="Cover", data=data)


def write_track_tags(path: Path, track: TrackInfo, *, genre: str, album: str | None,
                     artwork: bytes | None) -> None:
    """Replace all tags of ``path`` (a freshly converted staging file) with tags for
    ``track``. ``genre`` must already be normalized; ``UNKNOWN_GENRE`` omits TCON.
    Raises FileSystemError."""
    tags = ID3()
    title = normalize_whitespace(track.title)
    artist = normalize_whitespace(track.artist)
    if title:
        tags.add(TIT2(encoding=TEXT_ENCODING, text=[title]))
    if artist:
        tags.add(TPE1(encoding=TEXT_ENCODING, text=[artist]))
        tags.add(TPE2(encoding=TEXT_ENCODING, text=[artist]))
    album = normalize_whitespace(album)
    if album:
        tags.add(TALB(encoding=TEXT_ENCODING, text=[album]))
    if genre and genre != UNKNOWN_GENRE:
        tags.add(TCON(encoding=TEXT_ENCODING, text=[genre]))
    if track.year is not None:
        tags.add(TDRC(encoding=TEXT_ENCODING, text=[str(track.year)]))
    if track.playlist_index is not None and track.playlist_index > 0:
        tags.add(TRCK(encoding=TEXT_ENCODING, text=[str(track.playlist_index)]))
    if track.url:
        tags.add(WOAS(url=track.url))
    if track.track_id:
        tags.add(TXXX(encoding=TEXT_ENCODING, desc=TXXX_TRACK_ID, text=[track.track_id]))
    description = (track.description or "").strip()
    if description:
        tags.add(COMM(encoding=TEXT_ENCODING, lang="eng", desc="",
                      text=[description[:COMMENT_MAX_CHARS]]))
    if artwork:
        tags.add(artwork_frame(artwork))
    save_id3(tags, path)


# --- reading ---------------------------------------------------------------------------

def _text(tags: ID3 | None, frame_id: str) -> str:
    if tags is None:
        return ""
    frame = tags.get(frame_id)
    if frame is None or not getattr(frame, "text", None):
        return ""
    return normalize_whitespace("/".join(str(t) for t in frame.text))


def pick_comment(tags: ID3 | None) -> COMM | None:
    """The comment shown in the editor: first COMM with an empty description, else the first
    COMM that isn't an iTunes/encoder internal one."""
    if tags is None:
        return None
    comments = tags.getall("COMM")
    for frame in comments:
        if frame.desc == "":
            return frame
    for frame in comments:
        if not frame.desc.lower().startswith(("itun", "id3v1")):
            return frame
    return None


def _year(tags: ID3 | None) -> int | None:
    if tags is None:
        return None
    for frame_id in ("TDRC", "TYER", "TDOR", "TORY"):
        frame = tags.get(frame_id)
        if frame is not None and frame.text:
            stamp = frame.text[0]
            year = getattr(stamp, "year", None)
            if year:
                return int(year)
            head = str(stamp)[:4]
            if head.isdigit():
                return int(head)
    return None


def _track_number(tags: ID3 | None) -> int | None:
    head = _text(tags, "TRCK").split("/", 1)[0].strip()
    return int(head) if head.isdigit() and int(head) > 0 else None


def tag_data_from_id3(tags: ID3 | None, *, duration_s: float | None) -> TagData:
    track_id = None
    source_url = None
    if tags is not None:
        txxx = tags.get(f"TXXX:{TXXX_TRACK_ID}")
        if txxx is not None and txxx.text:
            track_id = normalize_whitespace(str(txxx.text[0])) or None
        woas = tags.getall("WOAS")
        if woas:
            source_url = str(woas[0].url).strip() or None
    comment = pick_comment(tags)
    return TagData(
        title=_text(tags, "TIT2"), artist=_text(tags, "TPE1"), album=_text(tags, "TALB"),
        album_artist=_text(tags, "TPE2"), genre=_text(tags, "TCON"), year=_year(tags),
        track_number=_track_number(tags),
        comment=("\n".join(str(t) for t in comment.text).strip() if comment else ""),
        track_id=track_id, source_url=source_url, duration_s=duration_s,
        has_artwork=bool(tags is not None and tags.getall("APIC")),
    )


def read_mp3(path: Path) -> TagData:
    """Tags plus duration of an MP3 (any ID3 version, or none). Raises FileSystemError if
    the file can't be read or has no MPEG audio frames."""
    try:
        audio = MP3(str(path))
    except (MutagenError, OSError) as exc:
        raise FileSystemError(f"{Path(path).name} could not be read as an MP3.",
                              detail=repr(exc)) from exc
    length = getattr(audio.info, "length", None)
    duration = round(float(length), 3) if length else None
    tags = audio.tags if isinstance(audio.tags, ID3) else None
    return tag_data_from_id3(tags, duration_s=duration)
