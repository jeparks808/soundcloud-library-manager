"""Filename/folder naming (Stage 4 implements).

STUB. Signatures and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from sclm.core.models import TrackInfo

MAX_COMPONENT_BYTES = 200   # UTF-8 bytes per path component, before " (n)" suffix/".mp3"


def sanitize_component(name: str, *, fallback: str = "Unknown") -> str:
    """Make ``name`` safe as ONE path component on Linux and FAT/exFAT USB drives:
    replace ``/ \\ : * ? " < > |`` and control chars with ``_`` (or a close equivalent),
    collapse whitespace, strip leading/trailing spaces and dots, avoid '.'/'..', truncate to
    ``MAX_COMPONENT_BYTES`` UTF-8 bytes on a character boundary. Empty result -> ``fallback``.
    """
    raise NotImplementedError


def render_filename(template: str, track: TrackInfo, *,
                    playlist_title: str | None = None) -> str:
    """Render ``Settings.filename_template`` for ``track`` WITHOUT the ``.mp3`` extension,
    sanitized with :func:`sanitize_component`.

    Fields: {artist} {title} {playlist} {index} {track_id} {genre} {year} {album}.
    Missing values render as '' and leftover separators (" - ", "  ") are tidied;
    {genre} is the normalized genre; {index} is ``playlist_index`` zero-padded to 2 digits.
    The template is assumed valid (``core.settings.validate_filename_template``).
    """
    raise NotImplementedError
