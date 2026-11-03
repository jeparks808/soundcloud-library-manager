"""Alternatives page backend: YouTube candidates, DRM labelling, and the manual-source job."""

from __future__ import annotations

from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import DownloadRequest, JobStatus, LibraryFile, LibrarySource
from sclm.download.alternatives import (
    Availability,
    MatchLevel,
    availability_from_formats,
    candidate_from_entry,
    check_source,
    failed_tracks,
    match_level,
    search_youtube,
    youtube_video_id,
)
from sclm.download.ytdlp_opts import download_opts

S = JobStatus
VIDEO = "https://www.youtube.com/watch?v=svlzfK6EiAE"


class _Ydl:
    """Minimal YoutubeDL stand-in: returns ``result`` (or raises it) from extract_info."""

    def __init__(self, result: Any, seen: list[dict[str, Any]], opts: dict[str, Any]) -> None:
        self.result, self.seen = result, seen
        seen.append(opts)

    def __enter__(self) -> _Ydl:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False, **_: Any) -> Any:
        assert download is False
        self.seen.append({"url": url})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _factory(result: Any, seen: list[dict[str, Any]]):
    return lambda opts: _Ydl(result, seen, opts)


def _fmt(fid: str, acodec: str = "opus", vcodec: str = "none", abr: float = 147.0,
         drm: bool = False) -> dict[str, Any]:
    return {"format_id": fid, "acodec": acodec, "vcodec": vcodec, "abr": abr, "has_drm": drm}


# --- URLs and matching ---------------------------------------------------------------------

@pytest.mark.parametrize("url,vid", [
    (VIDEO, "svlzfK6EiAE"),
    ("https://youtu.be/svlzfK6EiAE", "svlzfK6EiAE"),
    ("https://music.youtube.com/watch?v=svlzfK6EiAE&si=x", "svlzfK6EiAE"),
    ("https://www.youtube.com/shorts/svlzfK6EiAE", "svlzfK6EiAE"),
    ("http://www.youtube.com/watch?v=svlzfK6EiAE", None),          # not https
    ("https://www.youtube.com/playlist?list=PL123", None),
    ("https://evil.example/watch?v=svlzfK6EiAE", None),
    ("https://user:pw@www.youtube.com/watch?v=svlzfK6EiAE", None),
    ("https://www.youtube.com/watch?v=short", None),
])
def test_youtube_video_id(url, vid):
    assert youtube_video_id(url) == vid


def test_match_levels_and_candidate_mapping():
    assert match_level(None) == MatchLevel.UNKNOWN
    assert match_level(2.9) == match_level(-3.0) == MatchLevel.MATCH
    assert match_level(8.3) == MatchLevel.CLOSE
    assert match_level(-34.7) == MatchLevel.DIFFERENT
    c = candidate_from_entry({"id": "svlzfK6EiAE", "title": " Chris  Travis - 9k ",
                              "channel": "Chris Travis", "channel_is_verified": True,
                              "duration": 135, "view_count": 4882365}, 132.748)
    assert c is not None and c.url == VIDEO and c.title == "Chris Travis - 9k"
    assert c.match == MatchLevel.MATCH and round(c.delta_s, 1) == 2.3 and c.channel_verified
    assert candidate_from_entry({"id": "svlzfK6EiAE", "live_status": "is_live"}, 1.0) is None
    assert candidate_from_entry({"id": "PLnot-a-video"}, 1.0) is None


def test_search_ranks_by_length_then_verified(tmp_path):
    entries = [
        {"id": "aaaaaaaaaaa", "title": "live", "duration": 98},
        {"id": "bbbbbbbbbbb", "title": "reupload", "duration": 135},
        {"id": "ccccccccccc", "title": "official", "duration": 135, "channel_is_verified": True},
        {"id": "ddddddddddd", "title": "no length"},
        {"id": "eeeeeeeeeee", "title": "bass boosted", "duration": 141},
    ]
    seen: list[dict[str, Any]] = []
    found = search_youtube("Chris Travis 9k Freestyle", reference_s=132.748, cache_dir=tmp_path,
                           ydl_factory=_factory({"entries": entries}, seen))
    assert [c.title for c in found] == ["official", "reupload", "bass boosted", "live",
                                        "no length"]
    assert seen[0]["extract_flat"] == "in_playlist"
    assert seen[1]["url"] == "ytsearch6:Chris Travis 9k Freestyle"


# --- DRM labelling -------------------------------------------------------------------------

def test_drm_only_formats_are_labelled_and_not_downloadable():
    check = availability_from_formats([_fmt("1", drm=True), _fmt("2", "mp4a.40.2", drm=True)])
    assert check.availability == Availability.DRM_ONLY and not check.downloadable


def test_best_clear_audio_is_reported_with_quality():
    check = availability_from_formats([
        _fmt("drm", abr=320.0, drm=True),             # better, but DRM: ignored
        _fmt("140", "mp4a.40.2", abr=129.5),
        _fmt("251", "opus", abr=146.9),
        _fmt("18", "mp4a.40.2", vcodec="avc1", abr=96.0),
    ])
    assert check.downloadable and check.note == "Opus 147 kbps"
    assert availability_from_formats([_fmt("v", acodec="none", vcodec="avc1")]).availability \
        == Availability.UNAVAILABLE


def test_check_source_never_allows_drm_and_labels_errors(tmp_path):
    seen: list[dict[str, Any]] = []
    drm = DownloadError("ERROR: [youtube] x: This video is DRM protected")
    assert check_source(VIDEO, cache_dir=tmp_path, ydl_factory=_factory(drm, seen)
                        ).availability == Availability.DRM_ONLY
    assert seen[0]["allow_unplayable_formats"] is False
    gone = DownloadError("ERROR: [youtube] x: Video unavailable")
    check = check_source(VIDEO, cache_dir=tmp_path, ydl_factory=_factory(gone, seen))
    assert check.availability == Availability.UNAVAILABLE and check.note
    ok = check_source(VIDEO, cache_dir=tmp_path,
                      ydl_factory=_factory({"formats": [_fmt("251")]}, seen))
    assert ok.downloadable


def test_download_opts_never_allow_drm_formats(settings, tmp_path):
    opts = download_opts(settings, cache_dir=tmp_path, staging_dir=tmp_path, ffmpeg_path=tmp_path,
                         progress_hook=lambda d: None, postprocessor_hook=lambda d: None,
                         extra={"allow_unplayable_formats": True})
    assert opts["allow_unplayable_formats"] is False


# --- failed tracks -------------------------------------------------------------------------

def test_failed_tracks_lists_only_unresolved_alternative_failures(storage, make_track, tmp_path):
    def fail(track, category):
        job = storage.create_download(track)
        storage.update_download(job, status=S.FAILED, error=ErrorInfo(category, "msg", "d"))
        return job

    drm, net, later_ok, on_disk = (make_track(i) for i in ("1", "2", "3", "4"))
    drm_job = fail(drm, ErrorCategory.DRM_PROTECTED)
    fail(net, ErrorCategory.NETWORK)                          # retry instead
    fail(later_ok, ErrorCategory.DRM_PROTECTED)
    ok = storage.create_download(later_ok)
    storage.update_download(ok, status=S.COMPLETED, file_path=tmp_path / "x.mp3")
    fail(on_disk, ErrorCategory.AUTH_REQUIRED)
    file = tmp_path / "have.mp3"
    file.write_bytes(b"ID3")
    storage.add_library_file(LibraryFile(path=file, source=LibrarySource.SCAN, track_id="4"))
    retried = make_track("5")
    retried_job = fail(retried, ErrorCategory.GEO_BLOCKED)
    fail(retried, ErrorCategory.NETWORK)         # the alternative attempt hit a network error

    found = failed_tracks(storage)
    assert [(f.track.track_id, f.download_id) for f in found] == [("5", retried_job),
                                                                   ("1", drm_job)]
    assert [f.error.category for f in found] == [ErrorCategory.GEO_BLOCKED,
                                                 ErrorCategory.DRM_PROTECTED]


# --- the manual-source download job --------------------------------------------------------

def _youtube_info(formats=None) -> dict[str, Any]:
    return {"id": "svlzfK6EiAE", "title": "Chris Travis - 9k Freestyle [Official Music Video]",
            "uploader": "Chris Travis", "duration": 135, "webpage_url": VIDEO,
            "original_url": VIDEO, "extractor": "youtube",
            "formats": formats if formats is not None else [_fmt("251")]}


def test_alternative_source_download_keeps_soundcloud_metadata(
        run_one, make_track, fake_ydl, fake_script_cls, fake_finalize, fake_find_duplicate):
    track = make_track("109665566", title="9k Freestyle", artist="Chris Travis",
                       duration_s=132.748)
    fake_ydl.scripts[VIDEO] = fake_script_cls(info=_youtube_info())
    _, job = run_one(track, request_kwargs={"audio_source_url": VIDEO})

    assert job.status == S.COMPLETED and "YouTube" in job.message, job
    assert job.audio_source_url == VIDEO
    assert fake_ydl.extract_calls == [VIDEO] and fake_ydl.download_calls == [VIDEO]
    assert fake_ydl.opts[0]["allow_unplayable_formats"] is False
    tagged = fake_finalize.calls[0]["track"]
    assert (tagged.track_id, tagged.title, tagged.artist) == ("109665566", "9k Freestyle",
                                                              "Chris Travis")
    assert tagged.artwork_url == track.artwork_url and VIDEO in tagged.description
    assert fake_find_duplicate.calls == ["109665566"]          # duplicates by SoundCloud id


def test_alternative_source_with_only_drm_formats_is_never_downloaded(
        run_one, make_track, fake_ydl, fake_script_cls, fake_finalize):
    track = make_track("5")
    fake_ydl.scripts[VIDEO] = fake_script_cls(info=_youtube_info([_fmt("1", drm=True)]))
    _, job = run_one(track, request_kwargs={"audio_source_url": VIDEO})
    assert job.status == S.FAILED and job.error.category == ErrorCategory.DRM_PROTECTED
    assert fake_ydl.download_calls == [] and fake_finalize.calls == []


def test_alternative_source_must_be_a_youtube_video(run_one, make_track, fake_ydl):
    track = make_track("6")
    _, job = run_one(track, request_kwargs={"audio_source_url": "https://evil.example/a.mp3"})
    assert job.status == S.FAILED and job.error.category == ErrorCategory.INVALID_URL
    assert fake_ydl.extract_calls == []
