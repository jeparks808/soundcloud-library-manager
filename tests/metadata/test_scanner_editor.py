from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from mutagen.id3 import ID3, TIT2, TPE1, ID3v1SaveOptions

from sclm.core.errors import FileSystemError, JobCancelled
from sclm.core.models import LibraryFile, LibrarySource, TagData, TrackInfo
from sclm.core.storage import Storage
from sclm.metadata import editor
from sclm.metadata.duplicates import audio_sha256
from sclm.metadata.editor import read_tags, save_tags
from sclm.metadata.scanner import scan_library
from sclm.metadata.tagger import write_track_tags


def _tag(path: Path, track_id: str | None = None, *, title: str = "Song",
         artist: str = "Art") -> Path:
    write_track_tags(path, TrackInfo(track_id or "", "https://soundcloud.com/a/s",
                                     title=title, artist=artist),
                     genre="Unknown", album=None, artwork=None)
    return path


# --- scanner --------------------------------------------------------------------------

@pytest.fixture
def tree(library_dir: Path, mp3_copy: Callable) -> dict[str, Path]:
    a = _tag(mp3_copy(library_dir / "Artist" / "a.mp3"), "111", title="Alpha")
    b = _tag(mp3_copy(library_dir / "Other" / "deep" / "b.MP3", other=True), None,
             title="Beta", artist="Bee")
    c = mp3_copy(library_dir / "Untagged Artist - Untagged Song.mp3")
    corrupt = library_dir / "broken.mp3"
    corrupt.write_text("definitely not an mp3 file\n" * 20)
    (library_dir / "notes.txt").write_text("ignored")
    (library_dir / ".sclm-abc.part").write_bytes(b"partial")
    return {"a": a, "b": b, "c": c, "corrupt": corrupt}


def test_scan_adds_files_and_reports_corrupt(tree: dict[str, Path], library_dir: Path,
                                             storage: Storage) -> None:
    calls: list[tuple[int, int]] = []
    result = scan_library(library_dir, storage, progress=lambda d, t: calls.append((d, t)))
    assert result.files_seen == 4
    assert result.added == 3 and result.updated == 0 and result.removed_missing == 0
    assert len(result.errors) == 1 and str(tree["corrupt"]) in result.errors[0]
    assert calls[0] == (0, 4) and calls[-1] == (4, 4)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)

    a = storage.get_library_file(tree["a"])
    assert a is not None and a.source is LibrarySource.SCAN
    assert (a.track_id, a.artist, a.title) == ("111", "Art", "Alpha")
    assert a.audio_sha256 == audio_sha256(tree["a"])
    assert a.duration_s is not None and a.size_bytes == tree["a"].stat().st_size
    b = storage.get_library_file(tree["b"])
    assert b is not None and b.track_id is None and b.title == "Beta"
    c = storage.get_library_file(tree["c"])
    assert c is not None and (c.artist, c.title) == ("Untagged Artist", "Untagged Song")
    assert storage.get_library_file(tree["corrupt"]) is None
    assert storage.find_library_files(track_id="111")[0].path == tree["a"]


def test_rescan_unchanged_updated_and_missing(tree: dict[str, Path], library_dir: Path,
                                              storage: Storage,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    scan_library(library_dir, storage)
    hashed: list[Path] = []
    import sclm.metadata.scanner as scanner_mod
    real = scanner_mod.audio_sha256
    monkeypatch.setattr(scanner_mod, "audio_sha256", lambda p: hashed.append(p) or real(p))

    again = scan_library(library_dir, storage)
    assert (again.added, again.updated, again.removed_missing) == (0, 0, 0)
    assert hashed == []                               # unchanged files aren't re-hashed

    _tag(tree["a"], "111", title="Alpha Renamed")
    os.utime(tree["a"], (1_000_000, 1_000_000))
    tree["b"].unlink()
    third = scan_library(library_dir, storage)
    assert (third.added, third.updated, third.removed_missing) == (0, 1, 1)
    assert hashed == [tree["a"]]
    assert storage.get_library_file(tree["a"]).title == "Alpha Renamed"
    assert storage.get_library_file(tree["b"]) is None
    assert tree["c"].exists()                         # files are never touched


def test_scan_keeps_download_source_and_track_id(library_dir: Path, storage: Storage,
                                                 mp3_copy: Callable) -> None:
    p = mp3_copy(library_dir / "x.mp3")       # untagged: no TXXX id in the file
    storage.add_library_file(LibraryFile(path=p, source=LibrarySource.DOWNLOAD,
                                         track_id="999"))
    scan_library(library_dir, storage)
    row = storage.get_library_file(p)
    assert row.source is LibrarySource.DOWNLOAD and row.track_id == "999"
    assert row.audio_sha256 == audio_sha256(p)


def test_scan_removes_only_rows_under_root(library_dir: Path, tmp_path: Path,
                                           storage: Storage) -> None:
    outside = tmp_path / "elsewhere" / "gone.mp3"
    inside = library_dir / "gone.mp3"
    for p in (outside, inside):
        storage.add_library_file(LibraryFile(path=p, source=LibrarySource.SCAN))
    result = scan_library(library_dir, storage)
    assert result.removed_missing == 1
    assert storage.get_library_file(outside) is not None


def test_scan_cancel(tree: dict[str, Path], library_dir: Path, storage: Storage) -> None:
    ev = threading.Event()

    def progress(done: int, total: int) -> None:
        if done == 1:
            ev.set()

    with pytest.raises(JobCancelled):
        scan_library(library_dir, storage, progress=progress, cancel=ev)
    assert storage.count_library_files() <= 1


def test_scan_missing_root(tmp_path: Path, storage: Storage) -> None:
    with pytest.raises(FileSystemError):
        scan_library(tmp_path / "nope", storage)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_scan_unreadable_file_and_dir(library_dir: Path, storage: Storage,
                                      mp3_copy: Callable) -> None:
    good = mp3_copy(library_dir / "good.mp3")
    locked = mp3_copy(library_dir / "locked.mp3")
    locked.chmod(0)
    sealed = library_dir / "sealed"
    sealed.mkdir()
    mp3_copy(sealed / "hidden.mp3")
    sealed.chmod(0)
    try:
        result = scan_library(library_dir, storage)
    finally:
        locked.chmod(0o644)
        sealed.chmod(0o755)
    assert result.added == 1 and storage.get_library_file(good) is not None
    assert any("locked.mp3" in e for e in result.errors)
    assert any("sealed" in e for e in result.errors)


def test_scan_does_not_follow_symlink_loops(library_dir: Path, storage: Storage,
                                            mp3_copy: Callable) -> None:
    mp3_copy(library_dir / "sub" / "a.mp3")
    (library_dir / "sub" / "loop").symlink_to(library_dir, target_is_directory=True)
    assert scan_library(library_dir, storage).files_seen == 1


# --- editor ---------------------------------------------------------------------------------

def test_read_tags_untagged(tiny_mp3: Path) -> None:
    data = read_tags(tiny_mp3)
    assert data.title == "" and data.track_id is None and not data.has_artwork
    assert data.duration_s is not None


def test_read_tags_any_id3_version(tiny_mp3: Path) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["v24 title"]))
    tags.add(TPE1(encoding=3, text=["v24 artist"]))
    tags.save(str(tiny_mp3), v2_version=4, v1=ID3v1SaveOptions.CREATE)
    data = read_tags(tiny_mp3)
    assert (data.title, data.artist) == ("v24 title", "v24 artist")


def test_read_tags_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp3"
    bad.write_text("nope" * 100)
    for p in (bad, tmp_path / "missing.mp3"):
        with pytest.raises(FileSystemError):
            read_tags(p)


def test_editor_round_trip_preserves_id_url_and_hash(tiny_mp3: Path, storage: Storage,
                                                     jpeg_bytes: bytes,
                                                     png_bytes: bytes) -> None:
    write_track_tags(tiny_mp3, TrackInfo("321", "https://soundcloud.com/a/b", title="Old",
                                         artist="Old Artist", description="old comment"),
                     genre="Techno", album="Old Album", artwork=jpeg_bytes)
    h0 = audio_sha256(tiny_mp3)
    inode_parent = sorted(p.name for p in tiny_mp3.parent.iterdir())
    original = read_tags(tiny_mp3)
    edited = TagData(title="New Title", artist="New Artist", album="", album_artist="AA",
                     genre="House", year=1999, track_number=5, comment="new comment",
                     track_id="tampered", source_url="https://evil", has_artwork=False)
    row = save_tags(tiny_mp3, edited, storage, artwork=png_bytes)
    after = read_tags(tiny_mp3)
    assert (after.title, after.artist, after.album, after.album_artist, after.genre) == (
        "New Title", "New Artist", "", "AA", "House")
    assert (after.year, after.track_number, after.comment) == (1999, 5, "new comment")
    assert after.track_id == original.track_id == "321"          # read-only preserved
    assert after.source_url == original.source_url == "https://soundcloud.com/a/b"
    assert after.has_artwork
    apic = ID3(str(tiny_mp3)).getall("APIC")
    assert len(apic) == 1 and apic[0].data == png_bytes
    assert ID3(str(tiny_mp3)).version == (2, 3, 0)
    assert audio_sha256(tiny_mp3) == h0
    assert sorted(p.name for p in tiny_mp3.parent.iterdir()) == inode_parent  # no temp left
    # DB row
    assert row.path == tiny_mp3 and row.track_id == "321"
    assert (row.artist, row.title) == ("New Artist", "New Title")
    assert row.audio_sha256 == h0 and row.size_bytes == tiny_mp3.stat().st_size
    assert storage.get_library_file(tiny_mp3) == row

    save_tags(tiny_mp3, edited, storage, remove_artwork=True)
    assert not read_tags(tiny_mp3).has_artwork
    assert audio_sha256(tiny_mp3) == h0


def test_editor_keeps_download_source(tiny_mp3: Path, storage: Storage) -> None:
    storage.add_library_file(LibraryFile(path=tiny_mp3, source=LibrarySource.DOWNLOAD,
                                         track_id="5", audio_sha256="abc"))
    row = save_tags(tiny_mp3, TagData(title="T"), storage)
    assert row.source is LibrarySource.DOWNLOAD and row.audio_sha256 == "abc"


def test_save_tags_atomic_when_tagging_fails(tiny_mp3: Path, storage: Storage,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _tag(tiny_mp3, "1", title="Original")
    before = tiny_mp3.read_bytes()

    def broken_save(tags: object, path: Path) -> None:
        Path(path).write_bytes(b"half-written garbage")    # damage the temp copy...
        raise FileSystemError("Could not write tags.")      # ...then fail

    monkeypatch.setattr(editor, "save_id3", broken_save)
    with pytest.raises(FileSystemError):
        save_tags(tiny_mp3, TagData(title="New"), storage)
    assert tiny_mp3.read_bytes() == before                  # original intact
    assert [p.name for p in tiny_mp3.parent.iterdir()] == ["tiny.mp3"]
    assert storage.get_library_file(tiny_mp3) is None


def test_save_tags_atomic_when_replace_fails(tiny_mp3: Path, storage: Storage,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    before = tiny_mp3.read_bytes()

    def broken_replace(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(editor.os, "replace", broken_replace)
    with pytest.raises(FileSystemError):
        save_tags(tiny_mp3, TagData(title="New"), storage)
    assert tiny_mp3.read_bytes() == before
    assert [p.name for p in tiny_mp3.parent.iterdir()] == ["tiny.mp3"]


def test_save_tags_rejects_non_mp3(tmp_path: Path, storage: Storage) -> None:
    bad = tmp_path / "bad.mp3"
    bad.write_text("text " * 100)
    before = bad.read_bytes()
    with pytest.raises(FileSystemError):
        save_tags(bad, TagData(title="x"), storage)
    assert bad.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_save_tags_read_only_dir(tiny_mp3: Path, storage: Storage) -> None:
    before = tiny_mp3.read_bytes()
    tiny_mp3.parent.chmod(0o555)
    try:
        with pytest.raises(FileSystemError):
            save_tags(tiny_mp3, TagData(title="x"), storage)
    finally:
        tiny_mp3.parent.chmod(0o755)
    assert tiny_mp3.read_bytes() == before


def test_save_tags_preserves_file_mode(tiny_mp3: Path, storage: Storage) -> None:
    tiny_mp3.chmod(0o640)
    save_tags(tiny_mp3, TagData(title="x"), storage)
    assert tiny_mp3.stat().st_mode & 0o777 == 0o640
