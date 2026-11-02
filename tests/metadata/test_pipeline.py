from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from sclm.core.errors import FileSystemError, JobCancelled
from sclm.core.models import (
    ConflictPolicy, DuplicateReason, FinalizeOutcome, JobStatus, LibraryFile, LibrarySource,
    OrganizeMode, TrackInfo,
)
from sclm.core.settings import Settings
from sclm.core.storage import Storage
from sclm.metadata import organizer, pipeline
from sclm.metadata.duplicates import audio_sha256
from sclm.metadata.pipeline import ARTWORK_WARNING, finalize_track
from sclm.metadata.tagger import read_mp3

_real_link = os.link


def _finalize(staged: Path, track: TrackInfo, settings: Settings, storage: Storage,
              **kw: object):
    kw.setdefault("playlist_title", None)
    kw.setdefault("artwork_fetcher", lambda url: None)
    return finalize_track(staged, track, settings=settings, storage=storage, **kw)


def test_placed_with_tags_artwork_and_stages(tiny_mp3: Path, make_track: Callable,
                                             settings: Settings, storage: Storage,
                                             library_dir: Path, jpeg_bytes: bytes) -> None:
    stages: list[JobStatus] = []
    fetched: list[str] = []

    def fetcher(url: str) -> bytes:
        fetched.append(url)
        return jpeg_bytes

    track = make_track("100", title="Song", playlist_index=2, description="About")
    result = _finalize(tiny_mp3, track, settings, storage, playlist_title="Night Drive",
                       on_stage=stages.append, artwork_fetcher=fetcher)
    assert result.outcome is FinalizeOutcome.PLACED
    assert result.final_path == library_dir / "Night Drive" / "Test Artist - Song.mp3"
    assert result.final_path.is_file() and not tiny_mp3.exists()
    assert result.warnings == ()
    assert stages == [JobStatus.TAGGING, JobStatus.ORGANIZING]
    assert fetched == [track.artwork_url]
    assert result.audio_sha256 == audio_sha256(result.final_path)
    data = read_mp3(result.final_path)
    assert (data.title, data.artist, data.album_artist, data.album) == (
        "Song", "Test Artist", "Test Artist", "Night Drive")
    assert (data.genre, data.year, data.track_number) == ("Deep House", 2024, 2)
    assert (data.track_id, data.source_url, data.comment) == ("100", track.url, "About")
    assert data.has_artwork
    assert ID3(str(result.final_path)).version == (2, 3, 0)
    # never writes to the DB
    assert storage.count_library_files() == 0 and storage.get_track("100") is None


@pytest.mark.parametrize("fetcher", [
    lambda url: None,
    lambda url: (_ for _ in ()).throw(OSError("network down")),
])
def test_artwork_failure_is_a_warning(tiny_mp3: Path, make_track: Callable,
                                      settings: Settings, storage: Storage,
                                      fetcher: Callable) -> None:
    result = _finalize(tiny_mp3, make_track("1"), settings, storage, artwork_fetcher=fetcher)
    assert result.outcome is FinalizeOutcome.PLACED
    assert result.warnings == (ARTWORK_WARNING,)
    assert not read_mp3(result.final_path).has_artwork


def test_artwork_skipped_when_disabled_or_no_url(tiny_mp3: Path, make_track: Callable,
                                                 settings: Settings, storage: Storage,
                                                 mp3_copy: Callable, tmp_path: Path) -> None:
    def fail(url: str) -> bytes:
        raise AssertionError("must not fetch")

    r1 = _finalize(tiny_mp3, make_track("1"), settings.replace(embed_artwork=False), storage,
                   artwork_fetcher=fail)
    other = mp3_copy(tmp_path / "s2" / "x.mp3", other=True)
    r2 = _finalize(other, make_track("2", artwork_url=None), settings, storage,
                   artwork_fetcher=fail)
    assert r1.warnings == () and r2.warnings == ()


def test_default_fetcher_is_used_when_not_injected(tiny_mp3: Path, make_track: Callable,
                                                   settings: Settings, storage: Storage,
                                                   _no_network_artwork: list[str]) -> None:
    track = make_track("1")
    finalize_track(tiny_mp3, track, playlist_title=None, settings=settings, storage=storage)
    assert _no_network_artwork == [track.artwork_url]


@pytest.mark.parametrize(("mode", "playlist", "rel"), [
    (OrganizeMode.PLAYLIST, "PL", "PL/Test Artist - Song.mp3"),
    (OrganizeMode.PLAYLIST, None, "Singles/Test Artist - Song.mp3"),
    (OrganizeMode.ARTIST, "PL", "Test Artist/Test Artist - Song.mp3"),
    (OrganizeMode.ALBUM, "PL", "Test Artist/PL/Test Artist - Song.mp3"),
    (OrganizeMode.ALBUM, None, "Test Artist/Singles/Test Artist - Song.mp3"),
    (OrganizeMode.GENRE, "PL", "Deep House/Test Artist - Song.mp3"),
])
def test_organize_modes(tiny_mp3: Path, make_track: Callable, settings: Settings,
                        storage: Storage, library_dir: Path, mode: OrganizeMode,
                        playlist: str | None, rel: str) -> None:
    result = _finalize(tiny_mp3, make_track("1", title="Song"),
                       settings.replace(organize_mode=mode), storage, playlist_title=playlist)
    assert result.final_path == library_dir / rel


def test_label_upload_artist_extracted_for_tags_and_name(
        tiny_mp3: Path, make_track: Callable, settings: Settings, storage: Storage,
        library_dir: Path) -> None:
    track = make_track("1", artist="Big Room Records", title="Real Artist - Anthem")
    result = _finalize(tiny_mp3, track, settings.replace(organize_mode=OrganizeMode.ARTIST),
                       storage)
    assert result.final_path == library_dir / "Real Artist" / "Real Artist - Anthem.mp3"
    data = read_mp3(result.final_path)
    assert (data.artist, data.title) == ("Real Artist", "Anthem")


def test_filename_template_used(tiny_mp3: Path, make_track: Callable, settings: Settings,
                                storage: Storage, library_dir: Path) -> None:
    s = settings.replace(filename_template="{index} - {title} [{track_id}]")
    result = _finalize(tiny_mp3, make_track("9", title="Song", playlist_index=7), s, storage,
                       playlist_title="PL")
    assert result.final_path == library_dir / "PL" / "07 - Song [9].mp3"


# --- duplicates & conflicts --------------------------------------------------------------

def _place_existing(storage: Storage, path: Path, mp3_copy: Callable, *,
                    track_id: str | None = "old") -> Path:
    mp3_copy(path)
    storage.add_library_file(LibraryFile(path=path, source=LibrarySource.SCAN,
                                         track_id=track_id, audio_sha256=audio_sha256(path)))
    return path


@pytest.mark.parametrize("policy", [ConflictPolicy.SKIP, ConflictPolicy.ASK])
def test_content_hash_duplicate_skipped(tiny_mp3: Path, make_track: Callable,
                                        settings: Settings, storage: Storage,
                                        library_dir: Path, mp3_copy: Callable,
                                        policy: ConflictPolicy) -> None:
    existing = _place_existing(storage, library_dir / "elsewhere" / "Same.mp3", mp3_copy)
    before = existing.read_bytes()
    result = _finalize(tiny_mp3, make_track("2"), settings.replace(conflict_policy=policy),
                       storage)
    assert result.outcome is FinalizeOutcome.DUPLICATE
    assert result.duplicate is not None
    assert result.duplicate.reason is DuplicateReason.CONTENT_HASH
    assert result.duplicate.path == existing and result.duplicate.track_id == "old"
    assert result.audio_sha256 == audio_sha256(existing)
    assert tiny_mp3.exists()                      # left for the caller
    assert existing.read_bytes() == before
    assert not (library_dir / "Singles").exists()


def test_content_hash_ignored_if_file_missing(tiny_mp3: Path, make_track: Callable,
                                              settings: Settings, storage: Storage,
                                              library_dir: Path, mp3_copy: Callable) -> None:
    existing = _place_existing(storage, library_dir / "gone.mp3", mp3_copy)
    existing.unlink()
    assert _finalize(tiny_mp3, make_track("2"), settings, storage).outcome \
        is FinalizeOutcome.PLACED


@pytest.mark.parametrize(("policy", "force"), [(ConflictPolicy.KEEP_BOTH, False),
                                               (ConflictPolicy.SKIP, True)])
def test_content_hash_keep_both_or_force_places(tiny_mp3: Path, make_track: Callable,
                                                settings: Settings, storage: Storage,
                                                library_dir: Path, mp3_copy: Callable,
                                                policy: ConflictPolicy, force: bool) -> None:
    _place_existing(storage, library_dir / "x" / "Same.mp3", mp3_copy)
    result = _finalize(tiny_mp3, make_track("2"), settings.replace(conflict_policy=policy),
                       storage, force=force)
    assert result.outcome is FinalizeOutcome.PLACED


@pytest.mark.parametrize(("policy", "force", "expected_name"), [
    (ConflictPolicy.SKIP, False, None),
    (ConflictPolicy.ASK, False, None),
    (ConflictPolicy.KEEP_BOTH, False, "Test Artist - Track 3 (2).mp3"),
    (ConflictPolicy.SKIP, True, "Test Artist - Track 3 (2).mp3"),
])
def test_path_exists_policies(tiny_mp3: Path, make_track: Callable, settings: Settings,
                              storage: Storage, library_dir: Path, mp3_copy: Callable,
                              policy: ConflictPolicy, force: bool,
                              expected_name: str | None) -> None:
    taken = mp3_copy(library_dir / "Singles" / "Test Artist - Track 3.mp3", other=True)
    storage.add_library_file(LibraryFile(path=taken, source=LibrarySource.SCAN,
                                         track_id="other-id"))
    before = taken.read_bytes()
    result = _finalize(tiny_mp3, make_track("3"), settings.replace(conflict_policy=policy),
                       storage, force=force)
    assert taken.read_bytes() == before            # user file byte-identical
    if expected_name is None:
        assert result.outcome is FinalizeOutcome.DUPLICATE
        assert result.duplicate is not None
        assert result.duplicate.reason is DuplicateReason.PATH_EXISTS
        assert result.duplicate.path == taken and result.duplicate.track_id == "other-id"
        assert tiny_mp3.exists()
    else:
        assert result.outcome is FinalizeOutcome.PLACED
        assert result.final_path == library_dir / "Singles" / expected_name
        assert not tiny_mp3.exists()


@pytest.mark.parametrize("policy", [ConflictPolicy.SKIP, ConflictPolicy.KEEP_BOTH])
def test_race_between_workers(tiny_mp3: Path, make_track: Callable, settings: Settings,
                              storage: Storage, library_dir: Path,
                              monkeypatch: pytest.MonkeyPatch, policy: ConflictPolicy) -> None:
    dest = library_dir / "Singles" / "Test Artist - Track 4.mp3"
    raced = []

    def link(src: os.PathLike, dst: os.PathLike, *a: object, **kw: object) -> None:
        if not raced:
            raced.append(dst)
            Path(dst).write_bytes(b"WINNER")
        _real_link(src, dst, *a, **kw)

    monkeypatch.setattr(organizer.os, "link", link)
    result = _finalize(tiny_mp3, make_track("4"), settings.replace(conflict_policy=policy),
                       storage)
    assert dest.read_bytes() == b"WINNER"
    if policy is ConflictPolicy.SKIP:
        assert result.outcome is FinalizeOutcome.DUPLICATE
        assert result.duplicate.reason is DuplicateReason.PATH_EXISTS
    else:
        assert result.final_path == dest.with_name("Test Artist - Track 4 (2).mp3")


def test_cross_device_placement(tiny_mp3: Path, make_track: Callable, settings: Settings,
                                storage: Storage, library_dir: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tiny_mp3.parent

    def link(src: os.PathLike, dst: os.PathLike, *a: object, **kw: object) -> None:
        if Path(src).parent == staging:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        _real_link(src, dst, *a, **kw)

    monkeypatch.setattr(organizer.os, "link", link)
    result = _finalize(tiny_mp3, make_track("5"), settings, storage)
    assert result.outcome is FinalizeOutcome.PLACED
    assert not tiny_mp3.exists()
    assert read_mp3(result.final_path).track_id == "5"
    assert not any(p.name.startswith(".sclm-") for p in result.final_path.parent.iterdir())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_read_only_library_raises(tiny_mp3: Path, make_track: Callable, settings: Settings,
                                  storage: Storage, library_dir: Path) -> None:
    library_dir.chmod(0o555)
    try:
        with pytest.raises(FileSystemError):
            _finalize(tiny_mp3, make_track("6"), settings, storage)
        assert tiny_mp3.exists()
    finally:
        library_dir.chmod(0o755)


def test_tag_write_failure_raises(tmp_path: Path, make_track: Callable, settings: Settings,
                                  storage: Storage) -> None:
    bogus = tmp_path / "staging" / "nope.mp3"
    with pytest.raises(FileSystemError):
        _finalize(bogus, make_track("7"), settings, storage)
    assert not bogus.exists()


# --- cancellation ---------------------------------------------------------------------------

def test_cancel_before_start(tiny_mp3: Path, make_track: Callable, settings: Settings,
                             storage: Storage, library_dir: Path) -> None:
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        _finalize(tiny_mp3, make_track("8"), settings, storage, cancel=ev)
    assert list(library_dir.iterdir()) == []


def test_cancel_during_organizing_stage_prevents_move(tiny_mp3: Path, make_track: Callable,
                                                      settings: Settings, storage: Storage,
                                                      library_dir: Path) -> None:
    ev = threading.Event()

    def on_stage(stage: JobStatus) -> None:
        if stage is JobStatus.ORGANIZING:
            ev.set()

    with pytest.raises(JobCancelled):
        _finalize(tiny_mp3, make_track("8"), settings, storage, cancel=ev, on_stage=on_stage)
    assert tiny_mp3.exists()
    assert not any(library_dir.rglob("*.mp3"))


def test_cancel_after_move_still_placed(tiny_mp3: Path, make_track: Callable,
                                        settings: Settings, storage: Storage,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    ev = threading.Event()
    real_place = pipeline.place_file

    def place_then_cancel(*a: object, **kw: object) -> Path | None:
        result = real_place(*a, **kw)
        ev.set()
        return result

    monkeypatch.setattr(pipeline, "place_file", place_then_cancel)
    result = _finalize(tiny_mp3, make_track("8"), settings, storage, cancel=ev)
    assert result.outcome is FinalizeOutcome.PLACED and result.final_path.is_file()


# --- beets (seams; the real-beets test is in test_beets.py) -------------------------------

def test_beets_disabled_never_called(tiny_mp3: Path, make_track: Callable,
                                     settings: Settings, storage: Storage) -> None:
    def importer(path: Path) -> None:
        raise AssertionError("beets disabled")

    result = _finalize(tiny_mp3, make_track("1"), settings.replace(embed_artwork=False),
                       storage, beets_importer=importer)
    assert result.outcome is FinalizeOutcome.PLACED and result.warnings == ()


def test_beets_enabled_called_with_final_path(tiny_mp3: Path, make_track: Callable,
                                              settings: Settings, storage: Storage) -> None:
    calls: list[Path] = []
    result = _finalize(tiny_mp3, make_track("1"),
                       settings.replace(beets_enabled=True, embed_artwork=False),
                       storage, beets_importer=calls.append)
    assert calls == [result.final_path] and result.warnings == ()


def test_beets_failure_is_warning(tiny_mp3: Path, make_track: Callable, settings: Settings,
                                  storage: Storage) -> None:
    def importer(path: Path) -> None:
        raise RuntimeError("beets exploded")

    result = _finalize(tiny_mp3, make_track("1"),
                       settings.replace(beets_enabled=True, embed_artwork=False),
                       storage, beets_importer=importer)
    assert result.outcome is FinalizeOutcome.PLACED and result.final_path.is_file()
    assert len(result.warnings) == 1 and "beets exploded" in result.warnings[0]


def test_beets_enabled_but_unavailable(tiny_mp3: Path, make_track: Callable,
                                       settings: Settings, storage: Storage,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline.beets_bridge, "is_available", lambda: False)
    result = _finalize(tiny_mp3, make_track("1"),
                       settings.replace(beets_enabled=True, embed_artwork=False),
                       storage)
    assert result.outcome is FinalizeOutcome.PLACED
    assert len(result.warnings) == 1 and "not installed" in result.warnings[0]
