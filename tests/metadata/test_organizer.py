"""No-overwrite placement: pre-existing files, races, EXDEV, no-hardlink filesystems,
read-only destinations."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from sclm.core.errors import FileSystemError
from sclm.core.models import OrganizeMode, TrackInfo
from sclm.core.settings import Settings
from sclm.metadata import organizer
from sclm.metadata.organizer import place_file, plan_destination, suffixed

_real_link = os.link


def _src(tmp_path: Path, content: bytes = b"NEW AUDIO") -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    p = staging / "job.mp3"
    p.write_bytes(content)
    return p


def _no_part_files(folder: Path) -> bool:
    return not any(p.name.startswith(".sclm-") for p in folder.iterdir())


def test_place_into_new_folder(tmp_path: Path) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "a" / "b" / "Song.mp3"
    assert place_file(src, dest, keep_both=False) == dest
    assert dest.read_bytes() == b"NEW AUDIO"
    assert not src.exists()


@pytest.mark.parametrize("keep_both", [False, True])
def test_existing_destination_never_overwritten(tmp_path: Path, keep_both: bool) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "Song.mp3"
    dest.parent.mkdir()
    dest.write_bytes(b"USER FILE")
    result = place_file(src, dest, keep_both=keep_both)
    assert dest.read_bytes() == b"USER FILE"
    if keep_both:
        assert result == tmp_path / "lib" / "Song (2).mp3"
        assert result.read_bytes() == b"NEW AUDIO"
        assert not src.exists()
    else:
        assert result is None
        assert src.read_bytes() == b"NEW AUDIO"      # left for the caller
        assert sorted(p.name for p in dest.parent.iterdir()) == ["Song.mp3"]


def test_keep_both_picks_next_free_suffix(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("Song.mp3", "Song (2).mp3", "Song (3).mp3"):
        (lib / name).write_bytes(name.encode())
    result = place_file(_src(tmp_path), lib / "Song.mp3", keep_both=True)
    assert result == lib / "Song (4).mp3"
    for name in ("Song.mp3", "Song (2).mp3", "Song (3).mp3"):
        assert (lib / name).read_bytes() == name.encode()


def test_suffixed() -> None:
    assert suffixed(Path("/l/A - B.mp3"), 1) == Path("/l/A - B.mp3")
    assert suffixed(Path("/l/A - B.mp3"), 2) == Path("/l/A - B (2).mp3")


def _racing_link(winner_content: bytes):
    """os.link replacement: before the first link into the library, another 'worker'
    creates the destination (the race the architecture describes)."""
    state = {"raced": False}

    def link(src: os.PathLike, dst: os.PathLike, *a: object, **kw: object) -> None:
        if not state["raced"]:
            state["raced"] = True
            Path(dst).write_bytes(winner_content)
        _real_link(src, dst, *a, **kw)

    return link


@pytest.mark.parametrize("keep_both", [False, True])
def test_race_eexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_both: bool) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "Song.mp3"
    monkeypatch.setattr(organizer.os, "link", _racing_link(b"OTHER WORKER"))
    result = place_file(src, dest, keep_both=keep_both)
    assert dest.read_bytes() == b"OTHER WORKER"     # the winner is untouched
    if keep_both:
        assert result == dest.with_name("Song (2).mp3")
        assert result.read_bytes() == b"NEW AUDIO"
    else:
        assert result is None and src.exists()


def _exdev_link(staging: Path):
    """Hard links out of the staging dir fail with EXDEV (simulated other filesystem);
    links inside the destination filesystem work."""
    calls: list[tuple[Path, Path]] = []

    def link(src: os.PathLike, dst: os.PathLike, *a: object, **kw: object) -> None:
        calls.append((Path(src), Path(dst)))
        if Path(src).parent == staging:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        _real_link(src, dst, *a, **kw)

    return link, calls


def test_cross_device_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "Song.mp3"
    link, calls = _exdev_link(src.parent)
    monkeypatch.setattr(organizer.os, "link", link)
    assert place_file(src, dest, keep_both=False) == dest
    assert dest.read_bytes() == b"NEW AUDIO"
    assert not src.exists()
    assert _no_part_files(dest.parent)
    # second link came from a hidden part file in the destination dir
    assert calls[1][0].parent == dest.parent and calls[1][0].name.startswith(".sclm-")


@pytest.mark.parametrize("keep_both", [False, True])
def test_cross_device_with_existing_destination(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch,
                                                keep_both: bool) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "Song.mp3"
    dest.parent.mkdir()
    dest.write_bytes(b"USER FILE")
    link, _ = _exdev_link(src.parent)
    monkeypatch.setattr(organizer.os, "link", link)
    result = place_file(src, dest, keep_both=keep_both)
    assert dest.read_bytes() == b"USER FILE"
    assert _no_part_files(dest.parent)
    if keep_both:
        assert result == dest.with_name("Song (2).mp3")
        assert result.read_bytes() == b"NEW AUDIO"
    else:
        assert result is None and src.read_bytes() == b"NEW AUDIO"


def test_filesystem_without_hardlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAT/exFAT: every link() fails with EPERM -> exclusive-create copy, still no
    overwrite."""
    def link(*a: object, **kw: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(organizer.os, "link", link)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "Song.mp3").write_bytes(b"USER FILE")
    assert place_file(_src(tmp_path), lib / "Song.mp3", keep_both=False) is None
    assert (lib / "Song.mp3").read_bytes() == b"USER FILE"
    result = place_file(_src(tmp_path), lib / "Song.mp3", keep_both=True)
    assert result == lib / "Song (2).mp3" and result.read_bytes() == b"NEW AUDIO"
    assert (lib / "Song.mp3").read_bytes() == b"USER FILE"
    assert _no_part_files(lib)


needs_non_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                    reason="root ignores directory permissions")


@needs_non_root
def test_read_only_destination_dir(tmp_path: Path) -> None:
    src = _src(tmp_path)
    folder = tmp_path / "lib" / "Singles"
    folder.mkdir(parents=True)
    (folder / "Existing.mp3").write_bytes(b"USER FILE")
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(FileSystemError):
            place_file(src, folder / "Song.mp3", keep_both=True)
        assert src.read_bytes() == b"NEW AUDIO"
        assert sorted(p.name for p in folder.iterdir()) == ["Existing.mp3"]
    finally:
        folder.chmod(0o755)


@needs_non_root
def test_read_only_library_root(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    lib.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(FileSystemError):
            place_file(_src(tmp_path), lib / "New Folder" / "Song.mp3", keep_both=False)
    finally:
        lib.chmod(0o755)


@needs_non_root
def test_read_only_destination_with_exdev(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path)
    folder = tmp_path / "lib"
    folder.mkdir()
    link, _ = _exdev_link(src.parent)
    monkeypatch.setattr(organizer.os, "link", link)
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(FileSystemError):
            place_file(src, folder / "Song.mp3", keep_both=False)
        assert src.exists() and list(folder.iterdir()) == []
    finally:
        folder.chmod(0o755)


def test_copy_failure_leaves_no_partial_file(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path)
    dest = tmp_path / "lib" / "Song.mp3"
    link, _ = _exdev_link(src.parent)
    monkeypatch.setattr(organizer.os, "link", link)

    def boom(*a: object, **kw: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(organizer.os, "fsync", boom)
    with pytest.raises(FileSystemError) as info:
        place_file(src, dest, keep_both=False)
    assert "full" in info.value.user_message
    assert src.exists() and list(dest.parent.iterdir()) == []


# --- destination planning -------------------------------------------------------------

@pytest.mark.parametrize(("mode", "playlist", "expected"), [
    (OrganizeMode.PLAYLIST, "Night Drive", "Night Drive/Art - Song.mp3"),
    (OrganizeMode.PLAYLIST, None, "Singles/Art - Song.mp3"),
    (OrganizeMode.ARTIST, "Night Drive", "Art/Art - Song.mp3"),
    (OrganizeMode.ALBUM, "Night Drive", "Art/Night Drive/Art - Song.mp3"),
    (OrganizeMode.ALBUM, None, "Art/Singles/Art - Song.mp3"),
    (OrganizeMode.GENRE, "Night Drive", "Deep House/Art - Song.mp3"),
])
def test_plan_destination_modes(library_dir: Path, mode: OrganizeMode, playlist: str | None,
                                expected: str) -> None:
    s = Settings(library_dir=str(library_dir), organize_mode=mode)
    t = TrackInfo("1", "u", title="Song", artist="Art", genre="deep house")
    assert plan_destination(s, t, playlist) == library_dir / expected


def test_plan_destination_album_and_fallbacks(library_dir: Path) -> None:
    t = TrackInfo("1", "u", title="Song", artist="", genre="#junk #tags", album="LP 1")
    album = Settings(library_dir=str(library_dir), organize_mode=OrganizeMode.ALBUM)
    assert plan_destination(album, t, "PL") == library_dir / "Unknown Artist/LP 1/Song.mp3"
    artist = Settings(library_dir=str(library_dir), organize_mode=OrganizeMode.ARTIST)
    assert plan_destination(artist, t, None).parent == library_dir / "Unknown Artist"
    genre = Settings(library_dir=str(library_dir), organize_mode=OrganizeMode.GENRE)
    assert plan_destination(genre, t, None).parent == library_dir / "Unknown"


def test_plan_destination_hostile_folder_names_stay_inside_library(library_dir: Path) -> None:
    t = TrackInfo("1", "u", title="../../x", artist="../..", genre="..")
    for mode in OrganizeMode:
        s = Settings(library_dir=str(library_dir), organize_mode=mode)
        dest = plan_destination(s, t, "../../../etc")
        assert dest.resolve().is_relative_to(library_dir.resolve())
        assert ".." not in dest.relative_to(library_dir).parts
