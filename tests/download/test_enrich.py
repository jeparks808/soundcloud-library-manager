"""Batch metadata lookup for bare flat set entries (download.enrich), offline.

The fake YoutubeDL mimics what yt-dlp 2026.10.26 returns for a SoundCloud set with
``extract_flat``: entries with only id/url/ie_key/album fields. Its ``get_info_extractor``
returns a fake SoundcloudIE whose ``_call_api`` serves ``tracks?ids=`` from a dict.
"""

from __future__ import annotations

import logging
import threading

import pytest

from sclm.core.errors import JobCancelled
from sclm.download import enrich
from sclm.download.enrich import (
    api_track_to_info,
    enrich_playlist_info,
    merge_entry,
    needs_enrichment,
    secret_token,
)
from sclm.download.preview import fetch_preview

SET_URL = "https://soundcloud.com/test-artist/sets/the-ep"
ENRICH_LOGGER = "sclm.download.enrich"


def api_track(track_id: str, **overrides):
    obj = {
        "id": int(track_id),
        "kind": "track",
        "title": f"Song {track_id}",
        "duration": 201500,
        "permalink_url": f"https://soundcloud.com/test-artist/song-{track_id}",
        "genre": "Indie/pop",
        "created_at": "2012-07-28T17:51:00Z",
        "description": "desc",
        "artwork_url": f"https://i1.sndcdn.com/artworks-{track_id}-large.jpg",
        "user": {"id": 42, "username": "Test Artist",
                 "avatar_url": "https://i1.sndcdn.com/avatars-42-large.jpg"},
        "publisher_metadata": None,
    }
    obj.update(overrides)
    return obj


def bare_entry(track_id: str, *, api_url: bool = False, album: str = "The EP",
               album_type: str = "ep"):
    """Exactly the shape yt-dlp's _extract_set produces under extract_flat."""
    url = (f"https://api-v2.soundcloud.com/tracks/{track_id}" if api_url
           else f"https://soundcloud.com/test-artist/song-{track_id}")
    return {"_type": "url_transparent", "ie_key": "Soundcloud", "id": track_id, "url": url,
            "album": album, "album_artist": "Test Artist", "album_type": album_type}


def set_info(entries, *, playlist_id="2284613", url=SET_URL):
    return {"_type": "playlist", "id": playlist_id, "title": "The EP",
            "uploader": "Test Artist", "webpage_url": url, "extractor_key": "SoundcloudSet",
            "entries": entries}


class FakeSoundcloudIE:
    _API_V2_BASE = "https://api-v2.soundcloud.com/"

    def __init__(self, tracks, *, error=None, on_call=None):
        self.tracks = tracks          # id -> API object (ids missing here are omitted)
        self.error = error            # raised from _call_api (an exception or a list of them)
        self.on_call = on_call
        self.calls = []
        self.initialized = 0
        self._HEADERS = {"Authorization": "OAuth abc"}

    def initialize(self):
        self.initialized += 1

    def _call_api(self, url, video_id, note=None, **kwargs):
        self.calls.append({"url": url, "video_id": video_id, **kwargs})
        if self.on_call:
            self.on_call(len(self.calls))
        err = self.error.pop(0) if isinstance(self.error, list) and self.error else \
            (self.error if not isinstance(self.error, list) else None)
        if err is not None:
            raise err
        ids = kwargs["query"]["ids"].split(",")
        return [self.tracks[i] for i in ids if i in self.tracks]


class EnrichYdl:
    def __init__(self, result, ie):
        self.result = result
        self.ie = ie
        self.ie_keys = []

    def __call__(self, opts):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def extract_info(self, url, download=False):
        return self.result

    def get_info_extractor(self, key):
        self.ie_keys.append(key)
        return self.ie


@pytest.fixture
def run_preview(settings, storage, app_paths):
    def run(info, ie, url=SET_URL, **kwargs):
        ydl = EnrichYdl(info, ie)
        return fetch_preview(url, settings, storage, paths=app_paths, ydl_factory=ydl,
                             **kwargs), ydl
    return run


def enrich_warnings(caplog):
    return [r for r in caplog.records
            if r.name == ENRICH_LOGGER and r.levelno == logging.WARNING]


# --- through fetch_preview -------------------------------------------------------------------

def test_bare_entries_are_enriched_in_order_missing_stay_placeholders(run_preview, caplog,
                                                                      storage):
    ids = ["75206121", "47127625", "47127627", "30510138", "47127629", "47127631"]
    entries = [bare_entry(i, api_url=(i == "47127631")) for i in ids]
    # 47127627 (deleted) and 30510138 (private) are not returned by the API
    api = {i: api_track(i) for i in ids if i not in ("47127627", "30510138")}
    api["47127625"]["publisher_metadata"] = {"artist": "The Royal Concept"}
    ie = FakeSoundcloudIE(api)
    caplog.set_level(logging.INFO)

    res, ydl = run_preview(set_info(entries), ie)

    tracks = [e.track for e in res.entries]
    assert [t.track_id for t in tracks] == ids
    assert [t.playlist_index for t in tracks] == [1, 2, 3, 4, 5, 6]
    assert [t.title for t in tracks] == ["Song 75206121", "Song 47127625", "", "",
                                         "Song 47127629", "Song 47127631"]
    assert tracks[0].artist == "Test Artist"
    assert tracks[1].artist == "The Royal Concept"  # publisher artist like full extraction
    assert tracks[0].duration_s == pytest.approx(201.5)
    assert tracks[0].genre == "Indie/pop" and tracks[0].upload_date == "2012-07-28"
    assert tracks[0].uploader_id == "42"
    assert tracks[0].artwork_url == "https://i1.sndcdn.com/artworks-75206121-t500x500.jpg"
    # playlist-derived album kept for every entry (ep -> real album)
    assert all(t.album == "The EP" for t in tracks)
    # the API stub URL is replaced by the permalink; normal URLs untouched
    assert tracks[5].url == "https://soundcloud.com/test-artist/song-47127631"
    assert tracks[0].url == "https://soundcloud.com/test-artist/song-75206121"
    # unresolved ones: bare, placeholder only as display text, URL kept
    for t in (tracks[2], tracks[3]):
        assert t.title == "" and t.artist == "" and t.duration_s is None and t.is_partial
        assert t.display_name == f"Track {t.track_id}"
    # one API call for 6 ids, through yt-dlp's SoundCloud extractor with its headers
    assert ydl.ie_keys == ["Soundcloud"] and ie.initialized == 1
    [call] = ie.calls
    assert call["url"] == "https://api-v2.soundcloud.com/tracks"
    assert call["query"] == {"ids": ",".join(ids)}
    assert call["headers"] == {"Authorization": "OAuth abc"}
    # exactly one warning naming the unresolved ids
    [warning] = enrich_warnings(caplog)
    assert "2 of 6" in warning.getMessage()
    assert "47127627" in warning.getMessage() and "30510138" in warning.getMessage()
    # enriched metadata reaches the DB (a flat record never overwrites known values later)
    assert storage.get_track("75206121").title == "Song 75206121"


def test_batches_of_50(run_preview):
    ids = [str(1000 + n) for n in range(120)]
    ie = FakeSoundcloudIE({i: api_track(i) for i in ids})
    res, _ = run_preview(set_info([bare_entry(i) for i in ids]), ie)
    sizes = [len(c["query"]["ids"].split(",")) for c in ie.calls]
    assert sizes == [50, 50, 20]
    sent = [i for c in ie.calls for i in c["query"]["ids"].split(",")]
    assert sent == ids
    assert [e.track.title for e in res.entries] == [f"Song {i}" for i in ids]


def test_entries_with_titles_are_not_looked_up(run_preview):
    entries = [dict(bare_entry("1"), title="Known", uploader="U", duration=10.0),
               bare_entry("2")]
    ie = FakeSoundcloudIE({"1": api_track("1"), "2": api_track("2")})
    res, _ = run_preview(set_info(entries), ie)
    assert [c["query"]["ids"] for c in ie.calls] == ["2"]
    assert [e.track.title for e in res.entries] == ["Known", "Song 2"]


def test_no_lookup_when_nothing_missing(run_preview):
    entries = [dict(bare_entry("1"), title="Known")]
    ie = FakeSoundcloudIE({})
    run_preview(set_info(entries), ie)
    assert ie.calls == [] and ie.initialized == 0


def test_lookup_failure_never_fails_preview(run_preview, caplog):
    from yt_dlp.utils import ExtractorError

    ie = FakeSoundcloudIE({}, error=ExtractorError("Unable to download JSON metadata: "
                                                    "HTTP Error 500"))
    res, _ = run_preview(set_info([bare_entry("1"), bare_entry("2")]), ie)
    assert [e.track.track_id for e in res.entries] == ["1", "2"]
    assert all(e.track.title == "" for e in res.entries)
    assert res.sync.added == ("1", "2")
    assert enrich_warnings(caplog)


def test_ytdlp_internals_missing_never_fails_preview(run_preview, caplog):
    class NoApiIE:
        pass

    res, _ = run_preview(set_info([bare_entry("1")]), NoApiIE())
    assert res.entries[0].track.title == ""
    assert enrich_warnings(caplog)


def test_unexpected_response_shape_keeps_placeholders(run_preview, caplog):
    ie = FakeSoundcloudIE({})
    ie._call_api = lambda *a, **k: {"unexpected": True}
    res, _ = run_preview(set_info([bare_entry("1")]), ie)
    assert res.entries[0].track.title == ""
    assert enrich_warnings(caplog)


def test_one_failed_batch_does_not_stop_the_rest(run_preview):
    ids = [str(1000 + n) for n in range(120)]
    ie = FakeSoundcloudIE({i: api_track(i) for i in ids},
                          error=[None, RuntimeError("boom"), None])
    res, _ = run_preview(set_info([bare_entry(i) for i in ids]), ie)
    titles = [e.track.title for e in res.entries]
    assert len(ie.calls) == 3
    assert all(titles[:50]) and not any(titles[50:100]) and all(titles[100:])


def test_gives_up_after_consecutive_failures(run_preview):
    ids = [str(1000 + n) for n in range(200)]
    ie = FakeSoundcloudIE({}, error=[RuntimeError("down")] * 4)
    res, _ = run_preview(set_info([bare_entry(i) for i in ids]), ie)
    assert len(ie.calls) == enrich.MAX_CONSECUTIVE_FAILURES == 2
    assert len(res.entries) == 200


def test_cancel_between_batches(run_preview):
    ids = [str(1000 + n) for n in range(120)]
    cancel = threading.Event()
    ie = FakeSoundcloudIE({i: api_track(i) for i in ids},
                          on_call=lambda n: cancel.set() if n == 1 else None)
    with pytest.raises(JobCancelled):
        run_preview(set_info([bare_entry(i) for i in ids]), ie, cancel=cancel)
    assert len(ie.calls) == 1


def test_secret_set_passes_playlist_token(run_preview):
    url = "https://soundcloud.com/test-artist/sets/the-ep/s-AbC123"
    ie = FakeSoundcloudIE({"1": api_track("1")})
    entry = bare_entry("1")
    res, _ = run_preview(set_info([entry], url=url), ie, url=url)
    [call] = ie.calls
    assert call["query"] == {"ids": "1", "playlistId": "2284613",
                             "playlistSecretToken": "s-AbC123"}
    assert call["video_id"] == "2284613"
    assert res.entries[0].track.title == "Song 1"


def test_secret_token_entry_url_is_not_replaced_by_permalink(run_preview):
    url = "https://soundcloud.com/test-artist/sets/the-ep/s-AbC123"
    entry = bare_entry("1", api_url=True)
    entry["url"] += "?secret_token=s-AbC123"
    ie = FakeSoundcloudIE({"1": api_track("1")})
    res, _ = run_preview(set_info([entry], url=url), ie, url=url)
    t = res.entries[0].track
    assert t.title == "Song 1"
    assert t.url == "https://api-v2.soundcloud.com/tracks/1?secret_token=s-AbC123"


def test_user_list_entries_with_data_are_left_alone(run_preview):
    """User lists (tracks/likes) already carry flat metadata: no API call at all."""
    entries = [{"_type": "url", "ie_key": "Soundcloud", "id": "1", "title": "A",
                "uploader": "U", "duration": 100.0,
                "url": "https://soundcloud.com/u/a", "webpage_url": "https://soundcloud.com/u/a"}]
    info = dict(set_info(entries), extractor_key="SoundcloudUser")
    ie = FakeSoundcloudIE({})
    res, _ = run_preview(info, ie, url="https://soundcloud.com/u/tracks")
    assert ie.calls == [] and res.entries[0].track.title == "A"


# --- units ------------------------------------------------------------------------------------

def test_needs_enrichment():
    assert needs_enrichment(bare_entry("1"))
    assert not needs_enrichment(dict(bare_entry("1"), title="X"))
    assert not needs_enrichment(dict(bare_entry("1"), ie_key="SoundcloudSet"))  # a nested set
    assert not needs_enrichment(dict(bare_entry("1"), id=None))
    assert not needs_enrichment("nope")


def test_secret_token_parsing():
    assert secret_token("https://soundcloud.com/u/sets/slug/s-xyz") == "s-xyz"
    assert secret_token("https://soundcloud.com/u/sets/slug") is None
    assert secret_token("https://api-v2.soundcloud.com/tracks/1?secret_token=s-q") == "s-q"
    assert secret_token("https://soundcloud.com/u/track") is None
    assert secret_token("") is None


def test_api_track_to_info_is_defensive():
    info = api_track_to_info({"id": 5, "title": "  T  ", "duration": None, "user": None,
                              "permalink_url": "not a url", "created_at": "garbage"})
    assert info == {"id": "5", "title": "T"}
    full = api_track_to_info(api_track("7", artwork_url=None))
    assert full["thumbnail"] == "https://i1.sndcdn.com/avatars-42-large.jpg"
    assert full["duration"] == pytest.approx(201.5) and full["uploader"] == "Test Artist"


def test_merge_entry_keeps_flat_fields():
    flat = bare_entry("1", api_url=True)
    meta = {"id": "999", "url": "x", "album": "Other", "title": "T", "uploader": "U",
            "webpage_url": "https://soundcloud.com/u/t"}
    merged = merge_entry(flat, meta)
    assert merged["id"] == "1" and merged["url"] == flat["url"]
    assert merged["album"] == "The EP" and merged["album_type"] == "ep"
    assert merged["_type"] == "url_transparent"
    assert merged["title"] == "T" and merged["webpage_url"] == "https://soundcloud.com/u/t"


def test_enrich_non_playlist_is_untouched():
    info = {"_type": "video", "id": "1", "title": ""}
    assert enrich_playlist_info(object(), info, SET_URL) is info


def test_enrich_with_fetch_seam_preserves_order_and_dupes():
    entries = [bare_entry("1"), bare_entry("2"), bare_entry("1")]
    calls = []

    def fetch(ids):
        calls.append(list(ids))
        return [api_track("1"), api_track("2"), api_track("3"), "junk", {"no": "id"}]

    out = enrich_playlist_info(None, set_info(entries), SET_URL, fetch=fetch)
    assert calls == [["1", "2"]]
    assert [e["title"] for e in out["entries"]] == ["Song 1", "Song 2", "Song 1"]
