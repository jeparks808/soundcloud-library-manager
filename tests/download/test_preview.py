"""fetch_preview with a fake YoutubeDL and real SQLite: sync diff and "only new" flags."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sclm.core.errors import (
    AuthRequiredError,
    ConfigError,
    InvalidUrlError,
    JobCancelled,
    NetworkError,
    PrivateTrackError,
)
from sclm.core.models import JobStatus, LibraryFile, LibrarySource
from sclm.download.preview import fetch_preview

SET_URL = "https://soundcloud.com/test-artist/sets/summer-mix"


class PreviewYdl:
    """Minimal fake: returns ``result`` (or raises it) from extract_info, records options."""

    def __init__(self, result):
        self.result = result
        self.opts = []
        self.urls = []

    def __call__(self, opts):
        self.opts.append(opts)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def extract_info(self, url, download=False):
        assert download is False
        self.urls.append(url)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def preview(settings, storage, app_paths):
    def run(url, result, **kwargs):
        ydl = PreviewYdl(result)
        res = fetch_preview(url, kwargs.pop("settings", settings), storage, paths=app_paths,
                            ydl_factory=ydl, **kwargs)
        return res, ydl
    return run


def place_file(storage, library_dir: Path, track_id: str, *, via_download: bool) -> Path:
    path = library_dir / f"{track_id}.mp3"
    path.write_bytes(b"x")
    if via_download:
        track = storage.get_track(track_id)
        job = storage.create_download(track)
        storage.update_download(job, status=JobStatus.COMPLETED, file_path=path)
    else:
        storage.add_library_file(LibraryFile(path, LibrarySource.SCAN, track_id=track_id))
    return path


def test_rejects_non_soundcloud_urls_before_network(preview):
    for url in ("https://youtube.com/watch?v=x", "ftp://soundcloud.com/a", "", "soundcloud.com",
                "https://soundcloud.com/"):
        with pytest.raises(InvalidUrlError):
            preview(url, AssertionError("must not be called"))


def test_playlist_preview_syncs_and_flags_new(preview, sc, storage, library_dir):
    entries = [sc.flat_entry("1001", title="One"), sc.flat_entry("1002", title="Two"),
               sc.flat_entry("1003", permalink=False)]
    res, ydl = preview(SET_URL, sc.playlist(entries))

    opts = ydl.opts[0]
    assert opts["extract_flat"] == "in_playlist" and opts["skip_download"] is True
    assert opts["socket_timeout"] == 20 and "cookiefile" not in opts
    assert res.playlist.playlist_id == "2284613"
    assert [e.track.track_id for e in res.entries] == ["1001", "1002", "1003"]
    assert all(e.is_new for e in res.entries)
    assert res.sync.added == ("1001", "1002", "1003") and res.sync.removed == ()
    assert storage.get_playlist("2284613").track_count == 3

    # 1001 downloaded earlier, 1002 found by the scanner, then the playlist changes:
    # 1003 removed, 1004 added.
    p1 = place_file(storage, library_dir, "1001", via_download=True)
    p2 = place_file(storage, library_dir, "1002", via_download=False)
    entries2 = [sc.flat_entry("1001", title="One"), sc.flat_entry("1002", title="Two"),
                sc.flat_entry("1004", title="Four")]
    res2, _ = preview(SET_URL, sc.playlist(entries2))
    flags = {e.track.track_id: e.existing_path for e in res2.entries}
    assert flags == {"1001": p1, "1002": p2, "1004": None}
    assert [e.track.track_id for e in res2.new_entries] == ["1004"]
    assert res2.sync.added == ("1004",) and res2.sync.removed == ("1003",)


def test_only_new_after_file_deleted(preview, sc, storage, library_dir):
    preview(SET_URL, sc.playlist([sc.flat_entry("1001", title="One")]))
    p = place_file(storage, library_dir, "1001", via_download=True)
    res, _ = preview(SET_URL, sc.playlist([sc.flat_entry("1001", title="One")]))
    assert not res.entries[0].is_new
    p.unlink()  # user deleted the file -> counts as new again
    res, _ = preview(SET_URL, sc.playlist([sc.flat_entry("1001", title="One")]))
    assert res.entries[0].is_new


def test_single_track_preview(preview, sc, storage):
    url = "https://soundcloud.com/test-artist/track-1001"
    res, _ = preview(url, sc.track("1001"))
    assert res.playlist.is_single_track
    assert len(res.entries) == 1 and res.entries[0].is_new
    assert res.entries[0].track.title == "Track 1001"
    assert res.sync.added == () and storage.list_playlists() == []


def test_single_track_preview_only_raises_auth(preview, sc):
    with pytest.raises(AuthRequiredError):
        preview("https://soundcloud.com/a/go-plus", sc.preview_only())


def test_errors_are_classified(preview):
    from yt_dlp.utils import DownloadError

    with pytest.raises(PrivateTrackError):
        preview(SET_URL, DownloadError("ERROR: [soundcloud:set] x: This playlist is private"))
    with pytest.raises(NetworkError):
        preview(SET_URL, DownloadError("ERROR: [soundcloud:set] x: Unable to download JSON "
                                       "metadata: Read timed out."))


def test_cancel(preview, sc):
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        preview(SET_URL, sc.playlist([]), cancel=ev)


def test_cookies_copy_is_used_and_original_untouched(preview, sc, settings, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    res, ydl = preview(SET_URL, sc.playlist([]),
                       settings=settings.replace(cookies_file=str(cookies)))
    used = Path(ydl.opts[0]["cookiefile"])
    assert used != cookies and not used.exists()  # private copy, removed afterwards
    assert cookies.read_text() == "# Netscape HTTP Cookie File\n"


def test_missing_cookies_file_is_config_error(preview, sc, settings, tmp_path):
    with pytest.raises(ConfigError):
        preview(SET_URL, sc.playlist([]),
                settings=settings.replace(cookies_file=str(tmp_path / "nope.txt")))
