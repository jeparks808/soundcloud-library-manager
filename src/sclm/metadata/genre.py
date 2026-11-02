"""Genre normalization (Stage 4 implements).

STUB. Signature and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

UNKNOWN_GENRE = "Unknown"


def normalize_genre(raw: str | None) -> str:
    """Treat SoundCloud genre as untrusted free text.

    Collapse whitespace, title-case words (keep known acronyms like "R&B", "EDM", "UK"),
    and return ``UNKNOWN_GENRE`` for empty or junk values (e.g. only punctuation/digits,
    URLs, '#hashtags' lists, 'none', 'other', 'free download', longer than 40 chars).
    Only used for the TCON tag and, when organize_mode is GENRE, the folder name.
    """
    raise NotImplementedError
