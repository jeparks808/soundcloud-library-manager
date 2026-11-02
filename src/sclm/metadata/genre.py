"""Genre normalization.

SoundCloud genres are free text typed by uploaders, so they are treated as untrusted: they
are often empty, hashtag lists, promo text ("Free Download"), URLs or emoji. This module
turns them into a short, consistently cased label, or ``UNKNOWN_GENRE``.
"""

from __future__ import annotations

import re
import unicodedata

from sclm.core.normalize import normalize_whitespace

UNKNOWN_GENRE = "Unknown"
MAX_GENRE_CHARS = 40

# Lower-case word -> canonical spelling. Words not listed are title-cased.
_CANONICAL_WORDS: dict[str, str] = {
    "r&b": "R&B", "rnb": "RnB", "edm": "EDM", "uk": "UK", "us": "US", "ukg": "UKG",
    "idm": "IDM", "ebm": "EBM", "dj": "DJ", "djs": "DJs", "dnb": "DnB", "d&b": "D&B",
    "lofi": "Lofi", "lo-fi": "Lo-Fi", "hip-hop": "Hip-Hop", "j-pop": "J-Pop",
    "k-pop": "K-Pop", "j-rock": "J-Rock", "nyc": "NYC", "la": "LA", "80s": "80s",
    "90s": "90s", "70s": "70s", "60s": "60s", "00s": "00s", "2-step": "2-Step",
    "a&r": "A&R", "uk-garage": "UK-Garage",
}
# Joining words kept lower-case unless they start the genre.
_SMALL_WORDS = frozenset({"and", "n", "'n'", "of", "the", "with", "&", "+", "x"})

# Values that carry no genre information (compared after lower-casing / trimming).
_JUNK_VALUES = frozenset({
    "none", "other", "others", "unknown", "n/a", "na", "null", "nil", "misc",
    "miscellaneous", "genre", "music", "untitled", "no genre", "various", "various genres",
    "all", "everything", "mix", "mixes", "free", "free download", "free dl", "download",
    "soundcloud", "track", "tracks", "song", "songs", "beat", "test", "tbd", "?", "-",
})
_JUNK_SUBSTRINGS = re.compile(
    r"https?:|www\.|\.com\b|\.net\b|\.org\b|@|free\s*(?:download|dl)|out\s*now|"
    r"buy\s*link|follow\s+me|subscribe|click",
    re.IGNORECASE,
)
# Characters kept inside a genre; everything else (emoji, symbols, control chars) is dropped.
_KEEP_PUNCT = set("&-'/+.,")


def _clean_chars(text: str) -> str:
    # Emoji, symbols, controls and other punctuation become a word separator.
    kept = "".join(ch if (ch.isalnum() or ch in _KEEP_PUNCT) else " " for ch in text)
    return normalize_whitespace(kept)


def _case_word(word: str, *, first: bool) -> str:
    lower = word.lower()
    if lower in _CANONICAL_WORDS:
        return _CANONICAL_WORDS[lower]
    if not first and lower in _SMALL_WORDS:
        return lower
    # Title-case each hyphen/slash part: "synth-pop" -> "Synth-Pop", "house/techno".
    parts = re.split(r"([-/])", lower)
    return "".join(p[:1].upper() + p[1:] if p not in ("-", "/") else p for p in parts)


def normalize_genre(raw: str | None) -> str:
    """Treat SoundCloud genre as untrusted free text.

    Collapse whitespace, title-case words (keep known acronyms like "R&B", "EDM", "UK"),
    and return ``UNKNOWN_GENRE`` for empty or junk values (e.g. only punctuation/digits,
    URLs, '#hashtags' lists, 'none', 'other', 'free download', longer than 40 chars).
    Only used for the TCON tag and, when organize_mode is GENRE, the folder name.

    Rules, in order:
      * ``None``/empty/whitespace -> Unknown.
      * Text is NFKC-normalized (full-width letters become ASCII, etc.).
      * Two or more ``#`` (a hashtag list), URLs, e-mail/@handles and promo phrases -> Unknown.
        A single leading ``#`` is dropped ("#techno" -> "Techno").
      * Emoji, symbols and control characters are removed; only letters, digits, spaces
        and ``& - ' / + . ,`` survive. Surrounding punctuation is trimmed.
      * No letters left, a known junk word ("none", "other", "music", ...) or more than
        40 characters -> Unknown.
      * Otherwise each word is title-cased (known acronyms keep their canonical spelling,
        joining words like "and" stay lower-case).
    """
    if raw is None:
        return UNKNOWN_GENRE
    text = unicodedata.normalize("NFKC", str(raw))
    text = normalize_whitespace(text)
    if not text:
        return UNKNOWN_GENRE
    if text.count("#") >= 2 or _JUNK_SUBSTRINGS.search(text):
        return UNKNOWN_GENRE
    text = text.lstrip("#")
    text = _clean_chars(text)
    text = text.strip(" .,/+-'&")
    if not text or not any(ch.isalpha() for ch in text):
        return UNKNOWN_GENRE
    if text.lower() in _JUNK_VALUES or len(text) > MAX_GENRE_CHARS:
        return UNKNOWN_GENRE
    words = text.split(" ")
    return " ".join(_case_word(w, first=(i == 0)) for i, w in enumerate(words))
