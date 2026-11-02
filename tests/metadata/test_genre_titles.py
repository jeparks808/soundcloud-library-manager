from __future__ import annotations

import pytest

from sclm.core.models import TrackInfo
from sclm.metadata.genre import UNKNOWN_GENRE, normalize_genre
from sclm.metadata.titles import looks_like_label, refine_artist_title, split_artist_title


@pytest.mark.parametrize(("raw", "expected"), [
    ("deep house", "Deep House"),
    ("  Deep   House ", "Deep House"),
    ("DEEP HOUSE", "Deep House"),
    ("techno", "Techno"),
    ("r&b", "R&B"),
    ("R&B / Soul", "R&B / Soul"),
    ("edm", "EDM"),
    ("uk garage", "UK Garage"),
    ("drum and bass", "Drum and Bass"),
    ("Drum & Bass", "Drum & Bass"),
    ("dnb", "DnB"),
    ("hip-hop", "Hip-Hop"),
    ("hip hop & rap", "Hip Hop & Rap"),
    ("synth-pop", "Synth-Pop"),
    ("lo-fi", "Lo-Fi"),
    ("#techno", "Techno"),
    ("🔥 Techno 🔥", "Techno"),
    ("Ｔｅｃｈｎｏ", "Techno"),   # full-width (NFKC)
    ("drum & bass.", "Drum & Bass"),
])
def test_normalize_genre_good_values(raw: str, expected: str) -> None:
    assert normalize_genre(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "\t\n",
    "none", "None", "OTHER", "other", "Unknown", "n/a", "N/A", "misc", "music", "genre",
    "free download", "FREE DOWNLOAD", "Free DL", "techno (free download)",
    "#house #techno #deep", "#house#techno",
    "https://soundcloud.com/someone", "www.label.com", "label.com", "follow @me",
    "1234", "2019", "!!!", "...", "----", "🔥🔥🔥", "😀",
    "x" * 41,
    "a very long genre description that goes on and on",
])
def test_normalize_genre_junk_is_unknown(raw: str | None) -> None:
    assert normalize_genre(raw) == UNKNOWN_GENRE


def test_normalize_genre_length_boundary() -> None:
    assert normalize_genre("a" * 40) == "A" + "a" * 39
    assert normalize_genre("a" * 41) == UNKNOWN_GENRE


def test_normalize_genre_is_idempotent() -> None:
    for raw in ("deep house", "r&b", "drum and bass", "UK garage", "hip-hop", "junk!!", None):
        once = normalize_genre(raw)
        assert normalize_genre(once) == once


# --- "Artist - Title" parsing ------------------------------------------------------------

@pytest.mark.parametrize(("title", "uploader", "expected"), [
    # label/channel uploader + exactly one separator -> split
    ("Artist X - Song", "Some Records", ("Artist X", "Song")),
    ("Artist X – Song", "Deep Music Channel", ("Artist X", "Song")),
    ("[Premiere] Artist X - Song", "Cool Recordings", ("Artist X", "Song")),
    ("PREMIERE: Artist X - Song", "Label TV", ("Artist X", "Song")),
    ("Jay-Z - Song", "Roc Records", ("Jay-Z", "Song")),
    # uploader repeated in the title -> drop redundant prefix
    ("DJ Bob - Song", "DJ Bob", ("DJ Bob", "Song")),
    ("dj bob - Song - Radio Edit", "DJ Bob", ("DJ Bob", "Song - Radio Edit")),
    # uploader is one of several credited artists
    ("DJ Bob & Alice - Song", "DJ Bob", ("DJ Bob & Alice", "Song")),
    ("Alice x DJ Bob - Tune", "DJ Bob", ("Alice x DJ Bob", "Tune")),
    ("Alice feat. DJ Bob - Tune", "DJ Bob", ("Alice feat. DJ Bob", "Tune")),
])
def test_split_artist_title_applies(title: str, uploader: str,
                                    expected: tuple[str, str]) -> None:
    assert split_artist_title(title, uploader) == expected


@pytest.mark.parametrize(("title", "uploader"), [
    ("Artist X - Song", "DJ Bob"),            # personal uploader: could be a remix credit
    ("Song - Radio Edit", "DJ Bob"),
    ("01 - Intro", "Some Records"),           # track number, not an artist
    ("A - B - C", "Some Records"),            # ambiguous: two separators
    ("No separator here", "Some Records"),
    ("Hyphen-ated-title", "Some Records"),    # no spaced separator
    ("Bobby & Alice - Tune", "Bob"),          # 'Bob' is not a whole collaborator name
    (" - Song", "Some Records"),              # empty head
    ("Artist - ", "Some Records"),            # empty tail
    ("!!! - Song", "Some Records"),           # head without letters
    ("A" * 81 + " - Song", "Some Records"),   # implausibly long artist
])
def test_split_artist_title_is_conservative(title: str, uploader: str) -> None:
    assert split_artist_title(title, uploader) is None


def test_looks_like_label() -> None:
    assert looks_like_label("Anjunadeep Records")
    assert looks_like_label("Chill Music Channel")
    assert not looks_like_label("DJ Bob")
    assert not looks_like_label("")


def test_refine_artist_title_returns_same_object_when_unchanged() -> None:
    t = TrackInfo("1", "u", title="Song", artist="DJ Bob")
    assert refine_artist_title(t) is t
    t2 = TrackInfo("1", "u", title="Artist - Song", artist="Some Records")
    r = refine_artist_title(t2)
    assert (r.artist, r.title, r.track_id) == ("Artist", "Song", "1")
