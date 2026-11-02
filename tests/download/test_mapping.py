"""info dict -> TrackInfo / PlaylistInfo mapping, artwork choice, preview-only detection."""

from __future__ import annotations

import pytest

from sclm.core.errors import AuthRequiredError, TrackUnavailableError
from sclm.download.mapping import (
    choose_artwork_url,
    ensure_full_audio,
    is_preview_format,
    is_track_entry,
    merge_resolved,
    playlist_from_info,
    playlist_key,
    trackinfo_from_info,
    upgrade_artwork_url,
)


def test_full_track_mapping(sc):
    info = sc.track("1001", artists=None)
    t = trackinfo_from_info(info)
    assert t.track_id == "1001"
    assert t.url == "https://soundcloud.com/test-artist/track-1001"
    assert t.title == "Track 1001"
    assert t.artist == "Test Artist"
    assert t.duration_s == pytest.approx(180.5)
    assert t.genre == "Deep House"
    assert t.upload_date == "2024-05-17"
    assert t.year == 2024
    assert t.artwork_url == "https://i1.sndcdn.com/artworks-000030896212-o16m9v-t500x500.jpg"
    assert t.uploader_id == "42"
    assert t.description == "Free download. Follow me!"
    assert t.album is None
    assert t.is_partial is False


def test_publisher_artist_preferred_over_uploader(sc):
    t = trackinfo_from_info(sc.track("1", artists=["Real Artist", " Feat  Guest "]))
    assert t.artist == "Real Artist, Feat Guest"


def test_numeric_id_and_whitespace_normalized(sc):
    t = trackinfo_from_info(sc.track(123998367, title="  Youtube -  Dl Test  "))
    assert t.track_id == "123998367"
    assert t.title == "Youtube - Dl Test"


def test_missing_fields_are_none_and_partial(sc):
    info = {"id": "77", "webpage_url": "https://soundcloud.com/a/b"}
    t = trackinfo_from_info(info)
    assert (t.title, t.artist, t.duration_s, t.genre, t.upload_date, t.artwork_url) == \
        ("", "", None, None, None, None)
    assert t.is_partial is True
    assert t.display_name == "Track 77"


def test_upload_date_falls_back_to_timestamp():
    t = trackinfo_from_info({"id": "5", "url": "https://soundcloud.com/a/b", "title": "x",
                             "uploader": "u", "duration": 3, "timestamp": 1715904000})
    assert t.upload_date == "2024-05-17"


def test_genre_from_plain_genre_field_and_bad_types():
    t = trackinfo_from_info({"id": "5", "url": "https://soundcloud.com/a/b", "genre": "Techno",
                             "duration": "nope", "uploader_id": 9})
    assert t.genre == "Techno"
    assert t.duration_s is None
    assert t.uploader_id == "9"


def test_no_id_or_url_raises():
    with pytest.raises(TrackUnavailableError):
        trackinfo_from_info({"title": "x"})
    with pytest.raises(TrackUnavailableError):
        trackinfo_from_info({"id": "1", "url": "not-a-url"})


def test_album_only_for_real_albums(sc):
    ep = sc.flat_entry("1", album="The EP", album_type="ep")
    pl = sc.flat_entry("2", album="My playlist", album_type="playlist")
    assert trackinfo_from_info(ep).album == "The EP"
    assert trackinfo_from_info(pl).album is None


# --- artwork -------------------------------------------------------------------------------

def test_artwork_prefers_t500_then_original(sc):
    thumbs = sc.thumbnails()
    assert choose_artwork_url({"thumbnails": thumbs}).endswith("-t500x500.jpg")
    no500 = [t for t in thumbs if t["id"] != "t500x500"]
    assert choose_artwork_url({"thumbnails": no500}).endswith("-original.jpg")


def test_artwork_largest_unknown_thumbnail_and_rewrite():
    info = {"thumbnails": [
        {"url": "https://i1.sndcdn.com/artworks-abc-small.jpg", "width": 32},
        {"url": "https://i1.sndcdn.com/artworks-abc-large.jpg", "width": 100},
    ]}
    assert choose_artwork_url(info) == "https://i1.sndcdn.com/artworks-abc-t500x500.jpg"


def test_artwork_from_thumbnail_field_and_none():
    assert choose_artwork_url({"thumbnail": "https://i1.sndcdn.com/avatars-x-large.jpg"}) == \
        "https://i1.sndcdn.com/avatars-x-t500x500.jpg"
    assert choose_artwork_url({}) is None
    # non-SoundCloud URLs are left alone
    assert upgrade_artwork_url("https://example.com/a-large.jpg") == "https://example.com/a-large.jpg"


# --- merge -----------------------------------------------------------------------------------

def test_merge_resolved_keeps_queued_id_album_and_index(sc, make_track):
    queued = make_track("1001", title="", artist="", duration_s=None, album="The EP",
                        playlist_index=4, is_partial=True)
    resolved = trackinfo_from_info(sc.track("1001"))
    merged = merge_resolved(queued, resolved)
    assert merged.track_id == "1001"
    assert merged.title == "Track 1001" and merged.artist == "Test Artist"
    assert merged.album == "The EP"
    assert merged.playlist_index == 4
    assert merged.is_partial is False


def test_merge_resolved_replaces_placeholder_title(sc, make_track):
    """A queued entry the preview couldn't look up (bare, or carrying a "Track <id>"
    placeholder) gets the resolved title/artist/duration at download time, so the
    placeholder never reaches tags or filenames."""
    resolved = trackinfo_from_info(sc.track("1001", title="Real Song", uploader="Real Artist",
                                            artists=None, duration=211.0))
    for queued_title in ("", "Track 1001"):
        queued = make_track("1001", title=queued_title, artist="", duration_s=None,
                            album="The EP", is_partial=True)
        merged = merge_resolved(queued, resolved)
        assert merged.title == "Real Song"
        assert merged.artist == "Real Artist"
        assert merged.duration_s == pytest.approx(211.0)
        assert merged.album == "The EP" and merged.is_partial is False


def test_merge_resolved_id_mismatch_keeps_queued(sc, make_track):
    merged = merge_resolved(make_track("1"), trackinfo_from_info(sc.track("999")))
    assert merged.track_id == "1"


# --- preview-only (Go+) ------------------------------------------------------------------

def test_preview_format_detection(sc):
    for f in sc.preview_only()["formats"]:
        assert is_preview_format(f)
    for f in sc.track()["formats"]:
        assert not is_preview_format(f)


def test_ensure_full_audio_rejects_preview_only(sc):
    with pytest.raises(AuthRequiredError) as ei:
        ensure_full_audio(sc.preview_only())
    assert "preview" in ei.value.user_message.lower()


def test_ensure_full_audio_rejects_selected_preview(sc):
    info = sc.track()
    info["formats"] = info["formats"] + sc.preview_only()["formats"]
    ensure_full_audio(info)  # mixed: fine, yt-dlp prefers the full format
    info["requested_downloads"] = [sc.preview_only()["formats"][1]]
    with pytest.raises(AuthRequiredError):
        ensure_full_audio(info)


def test_ensure_full_audio_no_formats(sc):
    with pytest.raises(TrackUnavailableError):
        ensure_full_audio(sc.track(formats=[]))
    with pytest.raises(TrackUnavailableError):
        ensure_full_audio({"id": "1"})
    ensure_full_audio({"id": "1", "url": "file:///x.wav"})  # direct media


# --- playlists ------------------------------------------------------------------------------

def test_flat_playlist_mapping(sc):
    info = sc.playlist([
        sc.flat_entry("1001", title="First", album="Summer EP", album_type="ep"),
        sc.flat_entry("1002", permalink=False),
        {"_type": "url", "ie_key": None, "id": "55",
         "url": "https://soundcloud.com/other/sets/a-set"},           # reposted set: skipped
        sc.flat_entry("1001", title="First again"),                     # duplicate id: skipped
        {"_type": "url", "ie_key": "Soundcloud", "url": "https://soundcloud.com/x/y"},  # no id
        sc.flat_entry("1003", title="Third"),
    ])
    pl = playlist_from_info(info, "https://soundcloud.com/test-artist/sets/summer-mix")
    assert pl.playlist_id == "2284613"
    assert pl.title == "Summer Mix" and pl.uploader == "Test Artist"
    assert not pl.is_single_track
    assert [t.track_id for t in pl.entries] == ["1001", "1002", "1003"]
    assert [t.playlist_index for t in pl.entries] == [1, 2, 6]
    assert all(t.is_partial for t in pl.entries)
    assert pl.entries[0].title == "First" and pl.entries[0].album == "Summer EP"
    assert pl.entries[1].url == "https://api-v2.soundcloud.com/tracks/1002"
    assert pl.entries[1].title == ""


def test_flat_entry_playlist_index_from_ytdlp_wins(sc):
    e = sc.flat_entry("1")
    e["playlist_index"] = 7
    pl = playlist_from_info(sc.playlist([e]), "https://soundcloud.com/a/sets/b")
    assert pl.entries[0].playlist_index == 7


def test_single_track_result(sc):
    pl = playlist_from_info(sc.track("1001"), "https://soundcloud.com/test-artist/track-1001")
    assert pl.is_single_track and pl.playlist_id is None
    assert len(pl.entries) == 1 and pl.entries[0].is_partial is False
    assert pl.title == "Test Artist - Track 1001"


def test_user_lists_are_namespaced(sc):
    likes = sc.playlist([], playlist_id="42", title="Test Artist (Likes)",
                        extractor_key="SoundcloudUser",
                        url="https://soundcloud.com/test-artist/likes")
    tracks = dict(likes, title="Test Artist (Tracks)")
    k1 = playlist_key(likes, "https://soundcloud.com/test-artist/likes")
    k2 = playlist_key(tracks, "https://soundcloud.com/test-artist/tracks")
    k3 = playlist_key(tracks, "https://soundcloud.com/test-artist")
    assert k1 == "SoundcloudUser:42:likes"
    assert k2 == "SoundcloudUser:42:tracks"
    assert k3 == "SoundcloudUser:42:all"
    assert playlist_key(sc.playlist([]), "https://soundcloud.com/a/sets/b") == "2284613"


def test_is_track_entry():
    assert is_track_entry({"ie_key": "Soundcloud", "url": "https://soundcloud.com/a/b"})
    assert not is_track_entry({"ie_key": "SoundcloudSet", "url": "https://soundcloud.com/a/sets/b"})
    assert not is_track_entry({"url": "https://soundcloud.com/a/sets/b"})
    assert is_track_entry({"url": "https://soundcloud.com/a/b"})
