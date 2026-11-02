"""Conservative "Artist - Title" parsing for SoundCloud uploads.

On SoundCloud ``track.artist`` is the *uploader*. Labels, promo channels and repost
accounts usually upload as ``"Real Artist - Song"``, so tagging the uploader as the artist
is wrong for them. Parsing every dash is also wrong ("Jay-Z", "Song - Radio Edit",
"01 - Intro"), so the heuristic only acts when the evidence is strong.

Heuristic (:func:`refine_artist_title`), applied to the title's separator
``" - "`` / ``" – "`` / ``" — "`` (spaces required on both sides):

1. **Uploader repeated.** The part before the first separator equals the uploader
   (case/accents/punctuation-insensitive, via ``match_key`` normalization) -> keep the
   artist, drop the redundant prefix from the title.
2. **Uploader is a collaborator.** The head contains the uploader as a whole word, joined by
   ``&``, ``x``, ``,``, ``feat.``/``ft.``, ``and``, ``vs.`` or ``with`` (e.g.
   ``"Uploader & Friend - Song"``) -> artist = head, title = tail.
3. **Uploader looks like a label/channel.** The uploader name contains a label word
   (``Records``, ``Recordings``, ``Music``, ``Label``, ``Channel``, ``Radio``, ``TV``,
   ``Promotions``, ``Collective``, ``Network``, ``Entertainment``, ``Productions``,
   ``Official`` ...) **and** the title has exactly one separator -> artist = head,
   title = tail.

In all cases the head must contain a letter, must not be a bare track number, and must be
at most 80 characters; the tail must be non-empty. A leading promo tag on the head
(``[Premiere]``, ``(Free DL)``, ``PREMIERE:``) is ignored. Anything else is left unchanged.
"""

from __future__ import annotations

import dataclasses
import re

from sclm.core.models import TrackInfo
from sclm.core.normalize import match_key, normalize_whitespace

_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_LABEL_WORDS = re.compile(
    r"\b(?:records?|recordings|rec|music|musik|label|channel|radio|tv|promotions?|promo|"
    r"collective|network|entertainment|productions|official|repost(?:s)?|uploads|"
    r"distribution|publishing|imprint)\b",
    re.IGNORECASE,
)
_PROMO_PREFIX = re.compile(
    r"^\s*(?:[(\[{][^)\]}]*\b(?:premiere|exclusive|free\s*(?:download|dl)|out\s*now|new)\b"
    r"[^)\]}]*[)\]}]|(?:premiere|exclusive)\s*[:|])\s*",
    re.IGNORECASE,
)
_JOINERS = r"(?:&|\+|x|,|and|vs\.?|feat\.?|ft\.?|featuring|with)"
MAX_ARTIST_CHARS = 80


def _norm_part(text: str) -> str:
    """Case/accent/punctuation-insensitive form (the title half of ``match_key``)."""
    return match_key("", text).partition("|")[2]


def looks_like_label(uploader: str) -> bool:
    """True if the uploader name contains a typical label/channel word."""
    return bool(_LABEL_WORDS.search(uploader or ""))


def _valid_head(head: str) -> bool:
    return (bool(head) and len(head) <= MAX_ARTIST_CHARS and any(c.isalpha() for c in head)
            and not re.fullmatch(r"\d{1,3}\.?", head))


def _contains_collaborator(head: str, uploader_n: str) -> bool:
    if not uploader_n:
        return False
    parts = [p for p in re.split(rf"\s+{_JOINERS}\s+|\s*[,&+]\s*", head, flags=re.IGNORECASE)
             if p.strip()]
    return len(parts) > 1 and any(_norm_part(p) == uploader_n for p in parts)


def split_artist_title(title: str, uploader: str) -> tuple[str, str] | None:
    """Return ``(artist, title)`` parsed from an ``"Artist - Title"`` upload, or ``None``
    when the heuristic (module docstring) does not apply."""
    title = normalize_whitespace(title)
    uploader = normalize_whitespace(uploader)
    pieces = _SEPARATOR.split(title)
    if len(pieces) < 2:
        return None
    head = normalize_whitespace(_PROMO_PREFIX.sub("", pieces[0]))
    tail = normalize_whitespace(" - ".join(pieces[1:]))
    if not tail or not _valid_head(head):
        return None
    uploader_n = _norm_part(uploader)
    if uploader_n and _norm_part(head) == uploader_n:
        return uploader, tail
    if _contains_collaborator(head, uploader_n):
        return head, tail
    if len(pieces) == 2 and looks_like_label(uploader):
        return head, tail
    return None


def refine_artist_title(track: TrackInfo) -> TrackInfo:
    """``track`` with artist/title replaced by :func:`split_artist_title`'s result, or
    ``track`` itself when the heuristic does not apply."""
    parsed = split_artist_title(track.title, track.artist)
    if parsed is None:
        return track
    artist, title = parsed
    if artist == track.artist and title == track.title:
        return track
    return dataclasses.replace(track, artist=artist, title=title)
