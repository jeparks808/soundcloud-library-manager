"""Small pure helpers shared by storage, download and metadata.

``match_key`` is persisted in SQLite (tracks.match_key, library_files.match_key).
Changing its algorithm requires a schema migration that recomputes the column.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlsplit

SOUNDCLOUD_HOSTS = frozenset({
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
})

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]|_")
# Bracketed promo junk common in SoundCloud titles, e.g. "(Free Download)", "[OUT NOW]".
_JUNK_BRACKETS = re.compile(
    r"[(\[{][^)\]}]*\b(?:free\s*(?:download|dl)|out\s*now|premiere|official\s*(?:audio|video))\b"
    r"[^)\]}]*[)\]}]",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with seconds precision, e.g. '2026-11-02T10:00:00+00:00'."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_whitespace(text: str | None) -> str:
    """Collapse runs of whitespace to one space and strip. ``None`` -> ''."""
    return _WS.sub(" ", text or "").strip()


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _norm_part(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _JUNK_BRACKETS.sub(" ", text)
    text = text.replace("&", " and ")
    text = _strip_accents(text)
    text = _NON_WORD.sub(" ", text)
    return normalize_whitespace(text)


def match_key(artist: str | None, title: str | None) -> str:
    """Normalized ``artist|title`` key for fuzzy duplicate detection.

    Case/accents/punctuation-insensitive, strips promo brackets, and drops a redundant
    ``"<artist> - "`` prefix from the title. Returns '' when the title is empty (an empty
    key never matches anything).
    """
    artist_n = _norm_part(artist or "")
    title = title or ""
    if " - " in title:
        head, tail = title.split(" - ", 1)
        if tail.strip() and _norm_part(head) == artist_n:
            title = tail
    title_n = _norm_part(title)
    if not title_n:
        return ""
    return f"{artist_n}|{title_n}"


def is_soundcloud_url(url: str | None) -> bool:
    """True for http(s) URLs on a SoundCloud web host with a non-empty path."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme in ("http", "https") and host in SOUNDCLOUD_HOSTS
            and parts.path.strip("/") != "")
