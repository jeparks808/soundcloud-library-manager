"""Opt-in tests against real SoundCloud (``SCLM_NETWORK_TESTS=1``).

They use public test material that yt-dlp's own test suite uses: a ~10 s test track
published via a share link by a youtube-dl developer, and a small public EP.
These can break when SoundCloud changes its API or the uploads disappear; a failure here is
reported, not hidden.
"""

from __future__ import annotations

import pytest
from mutagen.mp3 import MP3

from sclm.core.models import DownloadRequest, JobStatus
from sclm.download.manager import DownloadManager
from sclm.download.preview import fetch_preview

pytestmark = pytest.mark.network

TEST_TRACK_URL = "https://soundcloud.com/jaimemf/youtube-dl-test-video-a-y-baw/s-8Pjrp"
TEST_TRACK_ID = "123998367"
TEST_SET_URL = "https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep"


def test_preview_public_track(settings, storage, app_paths):
    res = fetch_preview(TEST_TRACK_URL, settings, storage, paths=app_paths)
    assert res.playlist.is_single_track
    [entry] = res.entries
    assert entry.track.track_id == TEST_TRACK_ID
    assert entry.track.title and entry.track.artist
    assert entry.track.duration_s == pytest.approx(9.9, abs=1.0)
    assert entry.is_new


def test_preview_public_playlist(settings, storage, app_paths):
    res = fetch_preview(TEST_SET_URL, settings, storage, paths=app_paths)
    assert not res.playlist.is_single_track
    assert res.playlist.playlist_id and res.playlist.title
    assert len(res.entries) >= 1
    assert all(e.track.track_id.isdigit() for e in res.entries)
    assert [e.track.playlist_index for e in res.entries] == \
        sorted(e.track.playlist_index for e in res.entries)
    assert set(res.sync.added) == {e.track.track_id for e in res.entries}
    # yt-dlp's flat set entries carry no metadata; download.enrich must fill all of them
    for e in res.entries:
        t = e.track
        assert t.title and t.title != f"Track {t.track_id}", t
        assert t.artist, t
        assert t.duration_s and t.duration_s > 0, t
        assert t.album == "The Royal Concept EP", t
        assert t.url.startswith("https://soundcloud.com/"), t
    # a second preview sees no membership change
    again = fetch_preview(TEST_SET_URL, settings, storage, paths=app_paths)
    assert again.sync.added == () and again.sync.removed == ()


def test_download_public_track(storage, settings_store, settings, app_paths, listener,
                               fake_finalize, fake_find_duplicate, wait_until):
    settings_store.update(settings.replace(bitrate_kbps=128))
    seen = {}
    fake_finalize.inspect = lambda p: seen.update(length=MP3(p).info.length, name=p.name)
    m = DownloadManager(storage, settings_store, app_paths, listener, finalize=fake_finalize,
                        find_duplicate=fake_find_duplicate)
    try:
        res = fetch_preview(TEST_TRACK_URL, settings_store.get(), storage, paths=app_paths)
        [job] = m.enqueue([DownloadRequest(res.entries[0].track)])
        wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=120)
        final = m.get_job(job.job_id)
        assert final.status == JobStatus.COMPLETED, final.error
        assert seen["name"] == f"{TEST_TRACK_ID}.mp3"
        assert seen["length"] == pytest.approx(9.9, abs=1.5)
        assert final.final_path.is_file()
        assert storage.get_download(job.job_id).status == JobStatus.COMPLETED
    finally:
        m.shutdown(wait=True, timeout=30)


GO_PLUS_URL = "https://soundcloud.com/taylorswiftofficial/look-what-you-made-me-do"


def test_go_plus_preview_only_track_is_rejected(settings, storage, app_paths):
    """A Go+ track without a subscription only offers 30 s snippets: reported, never saved."""
    from sclm.core.errors import AuthRequiredError

    with pytest.raises(AuthRequiredError):
        fetch_preview(GO_PLUS_URL, settings, storage, paths=app_paths)
