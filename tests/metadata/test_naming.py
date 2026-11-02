from __future__ import annotations

import pytest

from sclm.core.models import TrackInfo
from sclm.core.settings import FILENAME_FIELDS
from sclm.metadata.naming import MAX_COMPONENT_BYTES, render_filename, sanitize_component

UNSAFE = set('/\\:*?"<>|')


def _assert_safe(name: str) -> None:
    assert name
    assert name not in (".", "..")
    assert not (UNSAFE & set(name))
    assert not any(ord(c) < 32 or ord(c) == 127 for c in name)
    assert name == name.strip(" .")
    assert len(name.encode("utf-8")) <= MAX_COMPONENT_BYTES
    name.encode("utf-8")  # valid UTF-8 (no lone surrogates)


@pytest.mark.parametrize(("raw", "expected"), [
    ("Artist - Title", "Artist - Title"),
    ("AC/DC", "AC_DC"),
    ("a\\b:c*d?e\"f<g>h|i", "a_b_c_d_e_f_g_h_i"),
    ("../../etc/passwd", "_.._etc_passwd"),
    ("..", "Unknown"),
    (".", "Unknown"),
    ("....", "Unknown"),
    ("...hidden...", "hidden"),
    ("  spaced   out  ", "spaced out"),
    ("tab\tnew\nline", "tab new line"),
    ("nul\x00byte", "nul_byte"),
    ("bell\x07", "bell_"),
    ("\u202eevil\u202c", "evil"),
    ("", "Unknown"),
    ("   ", "Unknown"),
    ("CON", "CON_"),
    ("con", "con_"),
    ("NUL.txt", "NUL_.txt"),
    ("Com1", "Com1_"),
    ("LPT9", "LPT9_"),
    ("CONSOLE", "CONSOLE"),
    ("Café 😀 Ünïcode", "Café 😀 Ünïcode"),
])
def test_sanitize_component_cases(raw: str, expected: str) -> None:
    result = sanitize_component(raw)
    assert result == expected
    _assert_safe(result)


def test_sanitize_fallback_is_used_for_empty_results() -> None:
    assert sanitize_component("///", fallback="Singles") == "___"
    assert sanitize_component(" . . ", fallback="Singles") == "Singles"
    assert sanitize_component("", fallback="Unknown Artist") == "Unknown Artist"


def test_sanitize_nfc_normalizes() -> None:
    decomposed = "Cafe\u0301"
    assert sanitize_component(decomposed) == "Café"


@pytest.mark.parametrize("raw", [
    "a" * 300,                      # 300 bytes ASCII
    "é" * 150,                      # 300 bytes, 2-byte chars
    "😀" * 75,                      # 300 bytes, 4-byte chars
    "x" + "😀" * 80,                # odd alignment
    "ab" + "e\u0301" * 120,         # combining sequences
    "👩\u200d👩\u200d👧\u200d👦" * 20,               # ZWJ emoji sequences
    "日本語のタイトル" * 20,
])
def test_sanitize_truncates_on_character_boundaries(raw: str) -> None:
    result = sanitize_component(raw)
    _assert_safe(result)
    assert raw.startswith(result) or raw.replace("e\u0301", "é").startswith(result)
    assert len(result.encode("utf-8")) > MAX_COMPONENT_BYTES - 30
    # Never ends in a dangling combining mark / joiner.
    assert result[-1] not in "\u0301\u200d"


def test_truncation_then_trailing_dots_stripped() -> None:
    raw = "a" * (MAX_COMPONENT_BYTES - 1) + ". tail"
    result = sanitize_component(raw)
    _assert_safe(result)
    assert result == "a" * (MAX_COMPONENT_BYTES - 1)


@pytest.mark.parametrize("hostile", [
    "/", "\\", "..", "../..", "\x00", "\x00\x00", ". .", "\u202e", "|<>|", "CON.", " AUX ",
])
def test_hostile_names_always_safe(hostile: str) -> None:
    _assert_safe(sanitize_component(hostile))


# --- render_filename ------------------------------------------------------------------

@pytest.fixture
def track() -> TrackInfo:
    return TrackInfo("777", "https://soundcloud.com/a/b", title="Song", artist="Art",
                     genre="deep house", album=None, upload_date="2020-06-01",
                     playlist_index=3)


@pytest.mark.parametrize(("template", "playlist", "expected"), [
    ("{artist} - {title}", None, "Art - Song"),
    ("{title}", None, "Song"),
    ("{index}. {title}", None, "03. Song"),
    ("{index} - {artist} - {title}", "Mix", "03 - Art - Song"),
    ("{playlist} - {title}", "My List", "My List - Song"),
    ("{playlist} - {title}", None, "Song"),
    ("{title} ({year})", None, "Song (2020)"),
    ("{title} [{genre}]", None, "Song [Deep House]"),
    ("{title} - {track_id}", None, "Song - 777"),
    ("{artist} - {album} - {title}", None, "Art - Song"),
    ("{artist} - {album} - {title}", "PL", "Art - PL - Song"),
])
def test_render_filename(track: TrackInfo, template: str, playlist: str | None,
                         expected: str) -> None:
    assert render_filename(template, track, playlist_title=playlist) == expected


def test_render_filename_missing_values_are_tidied() -> None:
    t = TrackInfo("1", "u", title="Song")
    assert render_filename("{artist} - {title}", t) == "Song"
    assert render_filename("{index}. {title}", t) == "Song"
    assert render_filename("{title} ({year})", t) == "Song"
    assert render_filename("{title} [{genre}]", TrackInfo("1", "u", title="Song",
                                                          genre="#a #b")) == "Song"
    assert render_filename("{artist} - {title}", TrackInfo("9", "u")) == "Track 9"


def test_render_filename_sanitizes_hostile_values() -> None:
    t = TrackInfo("1", "u", title="../../etc/passwd\x00", artist="A/B")
    name = render_filename("{artist} - {title}", t)
    _assert_safe(name)
    assert "/" not in name and "\x00" not in name


def test_render_filename_long_values_fit_limit() -> None:
    t = TrackInfo("1", "u", title="T" * 400, artist="😀" * 100)
    name = render_filename("{artist} - {title}", t)
    _assert_safe(name)
    assert len((name + " (999).mp3").encode()) < 255


def test_every_documented_field_renders(track: TrackInfo) -> None:
    template = " ".join("{" + f + "}" for f in sorted(FILENAME_FIELDS))
    name = render_filename(template, track, playlist_title="PL")
    assert "{" not in name and "Song" in name and "777" in name
