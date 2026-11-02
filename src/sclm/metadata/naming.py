"""Filename/folder naming.

Every folder and file name SCLM creates in the library goes through
:func:`sanitize_component`, because titles, artists, playlist names and genres all come
from untrusted uploader text.
"""

from __future__ import annotations

import re
import unicodedata

from sclm.core.models import TrackInfo
from sclm.core.normalize import normalize_whitespace
from sclm.metadata.genre import UNKNOWN_GENRE, normalize_genre

MAX_COMPONENT_BYTES = 200   # UTF-8 bytes per path component, before " (n)" suffix/".mp3"

# Characters invalid on FAT/exFAT/NTFS (and '/' on Linux) -> '_'.
_UNSAFE = frozenset('/\\:*?"<>|')
# Invisible bidi/direction controls (can disguise names, e.g. "evil\u202egpj.mp3").
_BIDI = frozenset("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Windows reserved device names (also reserved with any extension, e.g. "CON.mp3").
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
    | {f"COM{c}" for c in "¹²³"} | {f"LPT{c}" for c in "¹²³"}
)


def _is_attached(ch: str) -> bool:
    """True for characters that belong to the preceding character (combining marks,
    zero-width joiner, variation selectors, emoji skin-tone modifiers)."""
    return (unicodedata.combining(ch) != 0 or ch in "\u200d\ufe0e\ufe0f"
            or 0x1F3FB <= ord(ch) <= 0x1F3FF)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Longest prefix of ``text`` whose UTF-8 encoding fits ``max_bytes``, cut on a
    character boundary without splitting a combining sequence or emoji ZWJ sequence."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    cut = used = 0
    for ch in text:
        used += len(ch.encode("utf-8"))
        if used > max_bytes:
            break
        cut += 1
    while cut > 0 and ((cut < len(text) and _is_attached(text[cut]))
                       or text[cut - 1] == "\u200d"):
        cut -= 1
    return text[:cut]


def sanitize_component(name: str, *, fallback: str = "Unknown") -> str:
    """Make ``name`` safe as ONE path component on Linux and FAT/exFAT USB drives:
    replace ``/ \\ : * ? " < > |`` and control chars with ``_`` (or a close equivalent),
    collapse whitespace, strip leading/trailing spaces and dots, avoid '.'/'..', truncate to
    ``MAX_COMPONENT_BYTES`` UTF-8 bytes on a character boundary. Empty result -> ``fallback``.

    Details: the text is NFC-normalized; tabs/newlines become spaces, other control
    characters (including NUL) become ``_``; invisible bidi override characters are dropped;
    Windows device names (``CON``, ``NUL``, ``COM1``, ``LPT1``... also as ``CON.foo``) get a
    trailing ``_``. Truncation never splits a multi-byte character or a combining/emoji
    sequence. ``fallback`` itself is assumed safe.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    chars: list[str] = []
    for ch in text:
        if ch in _BIDI:
            continue
        if ch in "\t\n\r\v\f\x85  ":
            chars.append(" ")
        elif ch in _UNSAFE or unicodedata.category(ch) == "Cc":
            chars.append("_")
        else:
            chars.append(ch)
    text = normalize_whitespace("".join(chars))
    text = text.strip(" .")
    if text:
        stem = text.split(".", 1)[0].rstrip(" ").upper()
        if stem in _RESERVED:
            text = text[:len(stem)] + "_" + text[len(stem):]
    text = _truncate_utf8(text, MAX_COMPONENT_BYTES).strip(" .")
    if not text or set(text) <= {"."}:
        return fallback
    return text


# Separators left dangling when a template field renders empty.
_SEP = r"[-–—_|~,.:;]"
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
_LEADING_SEP = re.compile(rf"^(?:\s*{_SEP})+\s*")
_TRAILING_SEP = re.compile(rf"\s*(?:{_SEP}\s*)+$")


def _tidy(rendered: str) -> str:
    text = normalize_whitespace(rendered)
    text = _EMPTY_BRACKETS.sub("", text)
    text = normalize_whitespace(text)
    # "A -  - T" -> "A - T": collapse a run of separators into the first one.
    prev = None
    while prev != text:
        prev = text
        text = re.sub(rf"(\s{_SEP})\s+{_SEP}(?=\s|$)", r"\1", text)
    text = _LEADING_SEP.sub("", text)
    text = _TRAILING_SEP.sub("", text)
    return normalize_whitespace(text)


def template_values(track: TrackInfo, *, playlist_title: str | None = None) -> dict[str, str]:
    """Field values for ``Settings.filename_template`` (exposed for previews/tests)."""
    genre = normalize_genre(track.genre)
    return {
        "artist": normalize_whitespace(track.artist),
        "title": normalize_whitespace(track.title) or f"Track {track.track_id}",
        "playlist": normalize_whitespace(playlist_title),
        "index": f"{track.playlist_index:02d}" if track.playlist_index is not None else "",
        "track_id": normalize_whitespace(track.track_id),
        "genre": "" if genre == UNKNOWN_GENRE else genre,
        "year": str(track.year) if track.year is not None else "",
        "album": normalize_whitespace(track.album) or normalize_whitespace(playlist_title),
    }


def render_filename(template: str, track: TrackInfo, *,
                    playlist_title: str | None = None) -> str:
    """Render ``Settings.filename_template`` for ``track`` WITHOUT the ``.mp3`` extension,
    sanitized with :func:`sanitize_component`.

    Fields: {artist} {title} {playlist} {index} {track_id} {genre} {year} {album}.
    Missing values render as '' and leftover separators (" - ", "  ") are tidied;
    {genre} is the normalized genre; {index} is ``playlist_index`` zero-padded to 2 digits.
    The template is assumed valid (``core.settings.validate_filename_template``).

    An empty title renders as ``"Track <id>"`` (like ``TrackInfo.display_name``) and
    ``{album}`` falls back to the playlist title, mirroring the TALB tag.
    """
    values = template_values(track, playlist_title=playlist_title)
    rendered = template.format_map(values)
    fallback = sanitize_component(f"Track {track.track_id}", fallback="Track")
    return sanitize_component(_tidy(rendered), fallback=fallback)
