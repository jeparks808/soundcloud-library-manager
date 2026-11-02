from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from sclm.core.errors import ErrorCategory, ErrorInfo, StorageError
from sclm.core.models import (
    JobStatus,
    LibraryFile,
    LibrarySource,
    PlaylistInfo,
    TrackInfo,
)
from sclm.core.storage import INTERRUPTED_MESSAGE, SCHEMA_VERSION, Storage


def _playlist(make_track, ids, pid="pl1", title="My Set"):
    return PlaylistInfo(playlist_id=pid, url=f"https://soundcloud.com/u/sets/{pid}",
                        title=title, uploader="u",
                        entries=tuple(make_track(i) for i in ids))


# --- schema / lifecycle --------------------------------------------------------------

def test_fresh_db_is_migrated_and_uses_wal(storage: Storage, app_paths):
    assert storage.schema_version == SCHEMA_VERSION
    conn = sqlite3.connect(app_paths.db_file)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"tracks", "playlists", "playlist_tracks", "downloads", "library_files"} <= tables


def test_reopen_keeps_data_and_does_not_remigrate(app_paths, make_track):
    with Storage(app_paths.db_file) as db:
        db.upsert_track(make_track("1"))
    with Storage(app_paths.db_file) as db:
        assert db.schema_version == SCHEMA_VERSION
        assert db.get_track("1").title == "Track 1"


def test_newer_schema_is_rejected(app_paths):
    conn = sqlite3.connect(app_paths.db_file)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StorageError, match="newer version"):
        Storage(app_paths.db_file)


def test_in_memory_db_works(make_track):
    with Storage(":memory:") as db:
        db.upsert_track(make_track("1"))
        assert db.get_track("1") is not None


def test_closed_db_raises_storage_error(app_paths):
    db = Storage(app_paths.db_file)
    db.close()
    db.close()  # idempotent
    with pytest.raises(StorageError):
        db.get_track("1")


# --- tracks --------------------------------------------------------------------------

def test_upsert_track_roundtrip(storage, make_track):
    t = make_track("7", album="EP")
    storage.upsert_track(t)
    got = storage.get_track("7")
    assert got.title == t.title and got.artist == t.artist
    assert got.duration_s == 180.0 and got.genre == "Deep House" and got.album == "EP"
    assert got.upload_date == "2024-05-17" and not got.is_partial
    assert storage.get_track("missing") is None


def test_partial_track_never_overwrites_full_data(storage, make_track):
    storage.upsert_track(make_track("7", title="Real Title"))
    storage.upsert_track(TrackInfo("7", "https://soundcloud.com/x/y", title="", is_partial=True))
    got = storage.get_track("7")
    assert got.title == "Real Title" and got.duration_s == 180.0 and not got.is_partial


def test_full_track_upgrades_partial(storage, make_track):
    storage.upsert_track(TrackInfo("7", "https://soundcloud.com/x/y", is_partial=True))
    assert storage.get_track("7").is_partial
    storage.upsert_track(make_track("7", title="Now Known"))
    got = storage.get_track("7")
    assert got.title == "Now Known" and not got.is_partial


def test_empty_values_do_not_erase_known_ones(storage, make_track):
    storage.upsert_track(make_track("7"))
    storage.upsert_track(make_track("7", genre=None, artwork_url=None, title="New Title"))
    got = storage.get_track("7")
    assert got.genre == "Deep House" and got.artwork_url and got.title == "New Title"


# --- playlists -----------------------------------------------------------------------

def test_sync_playlist_tracks_membership_changes(storage, make_track):
    r1 = storage.sync_playlist(_playlist(make_track, ["a", "b", "c"]))
    assert r1.added == ("a", "b", "c") and r1.removed == ()
    first_seen_a = storage.playlist_members("pl1")[0].first_seen

    r2 = storage.sync_playlist(_playlist(make_track, ["a", "c", "d"]))
    assert r2.added == ("d",) and r2.removed == ("b",)
    current = [m.track.track_id for m in storage.playlist_members("pl1")]
    assert current == ["a", "c", "d"]
    all_members = storage.playlist_members("pl1", include_removed=True)
    assert [m.track.track_id for m in all_members][-1] == "b"
    assert all_members[-1].removed_at is not None

    # b comes back: counted as added again, removed_at cleared, first_seen kept
    r3 = storage.sync_playlist(_playlist(make_track, ["a", "b", "c", "d"]))
    assert r3.added == ("b",) and r3.removed == ()
    members = {m.track.track_id: m for m in storage.playlist_members("pl1")}
    assert members["b"].removed_at is None
    assert members["a"].first_seen == first_seen_a

    # unchanged sync -> no changes
    r4 = storage.sync_playlist(_playlist(make_track, ["a", "b", "c", "d"]))
    assert r4.added == () and r4.removed == ()


def test_sync_playlist_dedupes_repeated_entries_and_uses_positions(storage, make_track):
    pl = PlaylistInfo("pl1", "u", "T", entries=(
        make_track("a", playlist_index=1), make_track("b", playlist_index=2),
        make_track("a", playlist_index=3)))
    r = storage.sync_playlist(pl)
    assert r.added == ("a", "b")
    assert [(m.track.track_id, m.position) for m in storage.playlist_members("pl1")] == [
        ("a", 1), ("b", 2)]


def test_sync_playlist_rejects_single_track(storage, make_track):
    with pytest.raises(ValueError):
        storage.sync_playlist(PlaylistInfo(None, "u", "T", entries=(make_track("a"),)))


def test_get_and_list_playlists(storage, make_track):
    storage.sync_playlist(_playlist(make_track, ["a", "b"], pid="p2", title="Zeta"))
    storage.sync_playlist(_playlist(make_track, ["c"], pid="p1", title="alpha"))
    storage.sync_playlist(_playlist(make_track, ["a"], pid="p2", title="Zeta renamed"))
    p2 = storage.get_playlist("p2")
    assert p2.title == "Zeta renamed" and p2.track_count == 1
    assert [p.playlist_id for p in storage.list_playlists()] == ["p1", "p2"]
    assert storage.get_playlist("nope") is None


# --- downloads -----------------------------------------------------------------------

def test_download_lifecycle_and_history(storage, make_track, tmp_path):
    t = make_track("9", title="Song", artist="Artist")
    job_id = storage.create_download(t, playlist_id="pl1", bitrate_kbps=320)
    rec = storage.get_download(job_id)
    assert rec.status == JobStatus.QUEUED and rec.attempts == 0 and rec.bitrate_kbps == 320
    assert rec.title == "Song" and rec.artist == "Artist" and rec.playlist_id == "pl1"
    assert storage.get_track("9") is not None  # create_download upserts the track

    err = ErrorInfo(ErrorCategory.NETWORK, "Network error.", "timeout")
    storage.update_download(job_id, status=JobStatus.WAITING_RETRY, attempts=1, error=err)
    rec = storage.get_download(job_id)
    assert rec.status == JobStatus.WAITING_RETRY and rec.error == err and rec.attempts == 1

    f = tmp_path / "song.mp3"
    storage.update_download(job_id, status=JobStatus.COMPLETED, attempts=2, file_path=f)
    rec = storage.get_download(job_id)
    assert rec.status == JobStatus.COMPLETED and rec.error is None
    assert rec.file_path == f and rec.completed_at is not None

    other = storage.create_download(make_track("10"))
    storage.update_download(other, status=JobStatus.FAILED,
                            error=ErrorInfo(ErrorCategory.PRIVATE, "Private."))
    assert [r.id for r in storage.download_history()] == [other, job_id]
    assert [r.id for r in storage.download_history(statuses=[JobStatus.FAILED])] == [other]
    assert storage.download_history(statuses=[]) == []
    assert [r.id for r in storage.download_history(limit=1, offset=1)] == [job_id]


def test_update_unknown_download_raises(storage):
    with pytest.raises(StorageError):
        storage.update_download(999, status=JobStatus.FAILED)


def test_mark_interrupted_downloads(storage, make_track):
    running = storage.create_download(make_track("1"))
    storage.update_download(running, status=JobStatus.DOWNLOADING)
    queued = storage.create_download(make_track("2"))
    done = storage.create_download(make_track("3"))
    storage.update_download(done, status=JobStatus.COMPLETED)
    assert storage.mark_interrupted_downloads() == 2
    for job_id in (running, queued):
        rec = storage.get_download(job_id)
        assert rec.status == JobStatus.FAILED and rec.error.user_message == INTERRUPTED_MESSAGE
    assert storage.get_download(done).status == JobStatus.COMPLETED
    assert storage.mark_interrupted_downloads() == 0


def test_existing_track_paths_requires_file_on_disk(storage, make_track, tmp_path):
    present = tmp_path / "present.mp3"
    present.write_bytes(b"x")
    gone = tmp_path / "gone.mp3"

    a = storage.create_download(make_track("a"))
    storage.update_download(a, status=JobStatus.COMPLETED, file_path=present)
    b = storage.create_download(make_track("b"))
    storage.update_download(b, status=JobStatus.COMPLETED, file_path=gone)
    c = storage.create_download(make_track("c"))
    storage.update_download(c, status=JobStatus.FAILED, file_path=present)

    result = storage.existing_track_paths(["a", "b", "c", "zzz"])
    assert result == {"a": present}
    assert storage.completed_download_path("a") == present
    assert storage.completed_download_path("b") is None


def test_existing_track_paths_falls_back_to_library_files(storage, make_track, tmp_path):
    moved = tmp_path / "moved.mp3"
    moved.write_bytes(b"x")
    a = storage.create_download(make_track("a"))
    storage.update_download(a, status=JobStatus.COMPLETED, file_path=tmp_path / "old.mp3")
    storage.add_library_file(LibraryFile(moved, LibrarySource.SCAN, track_id="a"))
    assert storage.existing_track_paths(["a"]) == {"a": moved}


def test_existing_track_paths_handles_many_ids(storage):
    ids = [str(i) for i in range(1200)]
    assert storage.existing_track_paths(ids) == {}


# --- library files -------------------------------------------------------------------

def test_library_file_upsert_and_lookups(storage, tmp_path):
    p = tmp_path / "lib" / "A - Song.mp3"
    f = LibraryFile(p, LibrarySource.DOWNLOAD, track_id="1", artist="A", title="Song",
                    duration_s=200.0, audio_sha256="abc", size_bytes=10, mtime=1.0)
    row_id = storage.add_library_file(f)
    got = storage.get_library_file(p)
    assert got.id == row_id and got.track_id == "1" and got.added_at

    # rescan without track id / hash: keeps both and keeps source 'download'
    again = storage.add_library_file(LibraryFile(p, LibrarySource.SCAN, artist="A",
                                                 title="Song", duration_s=200.0, mtime=2.0))
    got = storage.get_library_file(p)
    assert again == row_id and got.track_id == "1" and got.audio_sha256 == "abc"
    assert got.source == LibrarySource.DOWNLOAD and got.mtime == 2.0

    assert [x.path for x in storage.find_library_files(track_id="1")] == [p]
    assert [x.path for x in storage.find_library_files(match_key=f.match_key)] == [p]
    assert [x.path for x in storage.find_library_files(audio_sha256="abc")] == [p]
    assert storage.find_library_files(match_key="") == []
    with pytest.raises(ValueError):
        storage.find_library_files()
    with pytest.raises(ValueError):
        storage.find_library_files(track_id="1", audio_sha256="abc")
    assert storage.count_library_files() == 1


def test_list_library_files_search_and_paths_under(storage, tmp_path):
    root = tmp_path / "lib"
    storage.add_library_file(LibraryFile(root / "x" / "b.mp3", LibrarySource.SCAN,
                                         artist="Bravo", title="100% Pure"))
    storage.add_library_file(LibraryFile(root / "a.mp3", LibrarySource.SCAN,
                                         artist="alpha", title="Song_One"))
    storage.add_library_file(LibraryFile(tmp_path / "libother" / "c.mp3", LibrarySource.SCAN,
                                         artist="Charlie", title="Other"))
    assert [f.artist for f in storage.list_library_files()] == ["alpha", "Bravo", "Charlie"]
    assert [f.artist for f in storage.list_library_files(search="100%")] == ["Bravo"]
    assert [f.artist for f in storage.list_library_files(search="g_o")] == ["alpha"]
    assert [f.artist for f in storage.list_library_files(search="xyz")] == []
    assert sorted(p.name for p in storage.library_paths_under(root)) == ["a.mp3", "b.mp3"]

    assert storage.remove_library_file(root / "a.mp3") is True
    assert storage.remove_library_file(root / "a.mp3") is False
    assert storage.count_library_files() == 2


# --- concurrency ---------------------------------------------------------------------

def test_concurrent_access_from_threads(storage, make_track):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(25):
                job = storage.create_download(make_track(f"{n}-{i}"))
                storage.update_download(job, status=JobStatus.DOWNLOADING, attempts=1)
                storage.update_download(job, status=JobStatus.COMPLETED,
                                        file_path=Path(f"/nonexistent/{n}-{i}.mp3"))
                storage.download_history(limit=5)
                storage.existing_track_paths([f"{n}-{i}"])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(storage.download_history(limit=1000)) == 150
