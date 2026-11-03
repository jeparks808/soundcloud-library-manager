from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sclm.core.errors import (
    AuthRequiredError,
    DrmProtectedError,
    ErrorCategory,
    ErrorInfo,
    FFmpegError,
    FFmpegNotFoundError,
    GeoBlockedError,
    JobCancelled,
    NetworkError,
    SclmError,
    error_info_from_exception,
    tail_lines,
)
from sclm.core.models import (
    DownloadJob,
    JobStatus,
    PlaylistInfo,
    PlaylistPreview,
    PreviewEntry,
    TrackInfo,
)
from sclm.core.normalize import is_soundcloud_url, match_key, normalize_whitespace


# --- models ----------------------------------------------------------------------------

def test_job_status_classification():
    terminal = {s for s in JobStatus if s.is_terminal}
    active = {s for s in JobStatus if s.is_active}
    assert terminal == {JobStatus.COMPLETED, JobStatus.SKIPPED_DUPLICATE,
                        JobStatus.FAILED, JobStatus.CANCELLED}
    assert JobStatus.QUEUED not in active and JobStatus.WAITING_RETRY in active
    assert not terminal & active
    assert JobStatus("downloading") is JobStatus.DOWNLOADING


def test_track_info_derived_fields(make_track):
    t = make_track("5", artist="Artist", title="Artist - Song (Free Download)")
    assert t.year == 2024
    assert t.display_name == "Artist - Artist - Song (Free Download)"
    assert t.match_key == "artist|song"
    bare = TrackInfo("9", "u", is_partial=True)
    assert bare.year is None and bare.display_name == "Track 9" and bare.match_key == ""
    assert TrackInfo("9", "u", upload_date="n/a").year is None


def test_models_are_frozen(make_track):
    t = make_track()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.title = "x"  # type: ignore[misc]
    job = DownloadJob(job_id=1, track=t)
    moved = dataclasses.replace(job, status=JobStatus.COMPLETED, final_path=Path("/a.mp3"))
    assert job.status is JobStatus.QUEUED and moved.status is JobStatus.COMPLETED


def test_playlist_and_preview(make_track):
    single = PlaylistInfo(None, "u", "Song", entries=(make_track("1"),))
    assert single.is_single_track
    pl = PlaylistInfo("p", "u", "Set", entries=(make_track("1"), make_track("2")))
    preview = PlaylistPreview(pl, (PreviewEntry(pl.entries[0], Path("/x.mp3")),
                                   PreviewEntry(pl.entries[1])))
    assert not pl.is_single_track
    assert [e.track.track_id for e in preview.new_entries] == ["2"]
    assert preview.sync.added == ()


# --- normalize -------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    (("Artist", "Song"), ("ARTIST", "song")),
    (("Beyoncé", "Halo"), ("Beyonce", "Halo!")),
    (("A & B", "Track"), ("A and B", "Track")),
    (("DJ X", "Tune [OUT NOW]"), ("dj x", "Tune")),
    (("DJ X", "DJ X - Tune"), ("DJ X", "Tune")),
    (("X", "Song   (Free DL)"), ("X", "Song")),
])
def test_match_key_equivalences(a, b):
    assert match_key(*a) == match_key(*b) != ""


@pytest.mark.parametrize("a,b", [
    (("Artist", "Song"), ("Artist", "Song (Remix)")),
    (("Artist", "Song"), ("Other", "Song")),
])
def test_match_key_differences(a, b):
    assert match_key(*a) != match_key(*b)


def test_match_key_empty_title():
    assert match_key("Artist", "") == "" and match_key(None, None) == ""
    assert match_key("Artist", "!!!") == ""


def test_normalize_whitespace():
    assert normalize_whitespace("  a \t b\n") == "a b"
    assert normalize_whitespace(None) == ""


@pytest.mark.parametrize("url,ok", [
    ("https://soundcloud.com/artist/track", True),
    ("https://soundcloud.com/artist/sets/my-set", True),
    ("http://m.soundcloud.com/artist/likes", True),
    ("  https://on.soundcloud.com/AbC123  ", True),
    ("https://www.soundcloud.com/artist", True),
    ("https://soundcloud.com/", False),
    ("https://evil.com/soundcloud.com/x", False),
    ("https://soundcloud.com.evil.com/x", False),
    ("ftp://soundcloud.com/x", False),
    ("soundcloud.com/artist/track", False),
    ("", False), (None, False), ("http://[::1", False),
])
def test_is_soundcloud_url(url, ok):
    assert is_soundcloud_url(url) is ok


# --- errors ----------------------------------------------------------------------------

def test_error_categories_and_retryability():
    assert NetworkError().retryable and NetworkError().category is ErrorCategory.NETWORK
    for exc in (GeoBlockedError(), AuthRequiredError(), FFmpegError(), JobCancelled()):
        assert not exc.retryable
    info = NetworkError(detail="timed out").to_info()
    assert info == ErrorInfo(ErrorCategory.NETWORK, NetworkError.default_message, "timed out")
    assert info.retryable
    assert isinstance(GeoBlockedError(), SclmError)
    assert "bypass" in AuthRequiredError().user_message


def test_drm_error_is_its_own_category_and_not_blamed_on_go_plus():
    exc = DrmProtectedError()
    assert exc.category is ErrorCategory.DRM_PROTECTED and not exc.retryable
    message = exc.user_message
    assert "bypass DRM" in message and "free" in message and "not only Go+" in message


def test_custom_user_message_and_str():
    exc = NetworkError("Custom", detail="d")
    assert exc.user_message == "Custom" and str(exc) == "Custom (d)"


def test_ffmpeg_error_keeps_stderr_tail():
    stderr = "\n".join(f"line {i}" for i in range(40)) + "\n\n"
    exc = FFmpegError(stderr=stderr)
    assert exc.stderr_tail.splitlines() == [f"line {i}" for i in range(25, 40)]
    assert exc.to_info().detail == exc.stderr_tail
    assert isinstance(FFmpegNotFoundError(), FFmpegError)
    assert tail_lines("", 3) == ""


def test_error_info_from_exception():
    assert error_info_from_exception(GeoBlockedError()).category is ErrorCategory.GEO_BLOCKED
    fs = error_info_from_exception(PermissionError(13, "Permission denied", "/x"))
    assert fs.category is ErrorCategory.FILESYSTEM and "/x" in fs.user_message
    unknown = error_info_from_exception(RuntimeError("boom"))
    assert unknown.category is ErrorCategory.UNKNOWN and "boom" in unknown.detail


def test_tag_data_defaults_and_stub_modules_import():
    from sclm.core.models import TagData

    t = TagData(title="T")
    assert t.artist == "" and t.year is None and not t.has_artwork
    # every cross-package stub module must at least import cleanly
    import importlib

    for mod in ("sclm.download.manager", "sclm.download.preview", "sclm.metadata.pipeline",
                "sclm.metadata.duplicates", "sclm.metadata.naming", "sclm.metadata.genre",
                "sclm.metadata.scanner", "sclm.metadata.editor", "sclm.gui.bridge",
                "sclm.gui.main_window"):
        importlib.import_module(mod)
