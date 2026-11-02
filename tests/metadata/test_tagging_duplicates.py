from __future__ import annotations

import errno
import io
from collections.abc import Callable
from pathlib import Path

import pytest
from mutagen.apev2 import APEv2
from mutagen.id3 import ID3, TIT2, ID3v1SaveOptions

from sclm.core.errors import FileSystemError
from sclm.core.models import DuplicateReason, LibraryFile, LibrarySource, TrackInfo
from sclm.core.storage import Storage
from sclm.metadata.artwork import fetch_artwork, image_mime
from sclm.metadata.duplicates import DURATION_TOLERANCE_S, audio_sha256, find_duplicate
from sclm.metadata.tagger import TXXX_TRACK_ID, read_mp3, write_track_tags


def _track(**kw: object) -> TrackInfo:
    values: dict[str, object] = dict(
        track_id="555", url="https://soundcloud.com/art/song", title="Song Ü",
        artist="Ärtist", duration_s=1.0, genre="techno", upload_date="2021-02-03",
        description="Line one\nline two", playlist_index=4)
    values.update(kw)
    return TrackInfo(**values)  # type: ignore[arg-type]


# --- tags ----------------------------------------------------------------------------

def test_write_and_read_back_all_frames(tiny_mp3: Path, jpeg_bytes: bytes) -> None:
    write_track_tags(tiny_mp3, _track(), genre="Techno", album="The Album",
                     artwork=jpeg_bytes)
    data = read_mp3(tiny_mp3)
    assert data.title == "Song Ü"
    assert data.artist == "Ärtist"
    assert data.album_artist == "Ärtist"
    assert data.album == "The Album"
    assert data.genre == "Techno"
    assert data.year == 2021
    assert data.track_number == 4
    assert data.comment == "Line one\nline two"
    assert data.source_url == "https://soundcloud.com/art/song"
    assert data.track_id == "555"
    assert data.has_artwork
    assert data.duration_s is not None and 0.9 < data.duration_s < 1.2

    raw = tiny_mp3.read_bytes()
    assert raw[:4] == b"ID3\x03"                 # ID3v2.3 on disk
    assert b"TYER" in raw and b"TDRC" not in raw  # year converted for v2.3
    assert raw[-128:-125] != b"TAG"              # no ID3v1
    tags = ID3(str(tiny_mp3))
    apic = tags.getall("APIC")
    assert len(apic) == 1 and apic[0].type == 3 and apic[0].mime == "image/jpeg"
    assert apic[0].data == jpeg_bytes
    assert tags[f"TXXX:{TXXX_TRACK_ID}"].text == ["555"]


def test_png_artwork_mime(tiny_mp3: Path, png_bytes: bytes) -> None:
    write_track_tags(tiny_mp3, _track(), genre="Techno", album=None, artwork=png_bytes)
    assert ID3(str(tiny_mp3)).getall("APIC")[0].mime == "image/png"


def test_unknown_genre_and_missing_fields_are_omitted(tiny_mp3: Path) -> None:
    t = TrackInfo("9", "https://soundcloud.com/x/y", title="Only Title")
    write_track_tags(tiny_mp3, t, genre="Unknown", album=None, artwork=None)
    tags = ID3(str(tiny_mp3))
    for frame in ("TCON", "TALB", "TPE1", "TPE2", "TRCK", "TDRC", "APIC", "COMM"):
        assert not tags.getall(frame), frame
    data = read_mp3(tiny_mp3)
    assert data.title == "Only Title" and not data.has_artwork


def test_comment_truncated_to_1000_chars(tiny_mp3: Path) -> None:
    write_track_tags(tiny_mp3, _track(description="x" * 5000), genre="Unknown", album=None,
                     artwork=None)
    assert len(read_mp3(tiny_mp3).comment) == 1000


def test_write_tags_replaces_existing_tags_and_removes_id3v1(tiny_mp3: Path) -> None:
    old = ID3()
    old.add(TIT2(encoding=3, text=["Old"]))
    old.save(str(tiny_mp3), v1=ID3v1SaveOptions.CREATE)
    assert tiny_mp3.read_bytes()[-128:-125] == b"TAG"
    write_track_tags(tiny_mp3, _track(), genre="Techno", album=None, artwork=None)
    assert read_mp3(tiny_mp3).title == "Song Ü"
    assert tiny_mp3.read_bytes()[-128:-125] != b"TAG"


def test_write_tags_failure_raises_filesystem_error(tmp_path: Path) -> None:
    with pytest.raises(FileSystemError):
        write_track_tags(tmp_path / "missing.mp3", _track(), genre="Techno", album=None,
                         artwork=None)


def test_read_mp3_rejects_non_mp3(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp3"
    bad.write_text("this is not audio at all\n" * 10)
    with pytest.raises(FileSystemError):
        read_mp3(bad)
    with pytest.raises(FileSystemError):
        read_mp3(tmp_path / "missing.mp3")


# --- artwork fetch (injected opener, no network) ------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_fetch_artwork_success_and_timeout_passed(jpeg_bytes: bytes) -> None:
    seen: dict[str, object] = {}

    def opener(req: object, timeout: float) -> _Resp:
        seen["timeout"] = timeout
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        return _Resp(jpeg_bytes)

    assert fetch_artwork("https://i1.sndcdn.com/a.jpg", timeout=3, opener=opener) == jpeg_bytes
    assert seen == {"timeout": 3, "url": "https://i1.sndcdn.com/a.jpg"}


@pytest.mark.parametrize("failure", ["oserror", "timeout", "not_image", "scheme"])
def test_fetch_artwork_failures_return_none(failure: str) -> None:
    def opener(req: object, timeout: float) -> _Resp:
        if failure == "oserror":
            raise OSError(errno.ECONNREFUSED, "refused")
        if failure == "timeout":
            raise TimeoutError("timed out")
        return _Resp(b"<html>not an image</html>")

    url = "file:///etc/passwd" if failure == "scheme" else "https://x/a.jpg"
    assert fetch_artwork(url, opener=opener) is None


def test_image_mime() -> None:
    assert image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert image_mime(b"GIF89a") is None


# --- audio hash ------------------------------------------------------------------------

def test_audio_hash_stable_across_retagging(tiny_mp3: Path, jpeg_bytes: bytes) -> None:
    h0 = audio_sha256(tiny_mp3)
    write_track_tags(tiny_mp3, _track(), genre="Techno", album="A", artwork=jpeg_bytes)
    h1 = audio_sha256(tiny_mp3)
    write_track_tags(tiny_mp3, _track(title="Other " * 50), genre="House", album=None,
                     artwork=None)
    h2 = audio_sha256(tiny_mp3)
    # ID3v1 + APEv2 trailing tags are excluded too.
    ID3(str(tiny_mp3)).save(str(tiny_mp3), v2_version=3, v1=ID3v1SaveOptions.CREATE)
    ape = APEv2()
    ape["Title"] = "ape title"
    ape.save(str(tiny_mp3))
    h3 = audio_sha256(tiny_mp3)
    assert h0 == h1 == h2 == h3
    assert len(h0) == 64


def test_audio_hash_differs_for_different_audio(tmp_path: Path,
                                                mp3_copy: Callable[..., Path]) -> None:
    a = mp3_copy(tmp_path / "a.mp3")
    b = mp3_copy(tmp_path / "b.mp3", other=True)
    assert audio_sha256(a) != audio_sha256(b)


def test_audio_hash_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(FileSystemError):
        audio_sha256(tmp_path / "missing.mp3")


# --- find_duplicate ----------------------------------------------------------------

def _lib_file(storage: Storage, path: Path, **kw: object) -> LibraryFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"x")
    f = LibraryFile(path=path, source=LibrarySource.SCAN, **kw)  # type: ignore[arg-type]
    storage.add_library_file(f)
    return f


def test_find_duplicate_none_for_empty_db(storage: Storage, make_track: Callable) -> None:
    assert find_duplicate(storage, make_track("1")) is None


def test_find_duplicate_by_track_id(storage: Storage, make_track: Callable,
                                    library_dir: Path) -> None:
    track = make_track("42")
    path = library_dir / "song.mp3"
    path.write_bytes(b"x")
    job = storage.create_download(track)
    from sclm.core.models import JobStatus
    storage.update_download(job, status=JobStatus.COMPLETED, file_path=path)
    m = find_duplicate(storage, track)
    assert m is not None and m.reason is DuplicateReason.TRACK_ID
    assert m.path == path and m.track_id == "42"
    path.unlink()                                   # file gone -> no match
    assert find_duplicate(storage, track) is None


def test_find_duplicate_by_library_track_id(storage: Storage, make_track: Callable,
                                            library_dir: Path) -> None:
    _lib_file(storage, library_dir / "x.mp3", track_id="43", artist="Zed", title="Other")
    m = find_duplicate(storage, make_track("43"))
    assert m is not None and m.reason is DuplicateReason.TRACK_ID


@pytest.mark.parametrize(("lib_duration", "matches"), [
    (180.0, True),
    (180.0 + DURATION_TOLERANCE_S, True),          # exactly +2 s
    (180.0 - DURATION_TOLERANCE_S, True),          # exactly -2 s
    (182.01, False),
    (177.99, False),
    (None, False),
])
def test_find_duplicate_artist_title_duration_boundary(
        storage: Storage, make_track: Callable, library_dir: Path,
        lib_duration: float | None, matches: bool) -> None:
    _lib_file(storage, library_dir / "a.mp3", artist="Test Artist",
              title="Track 7 (Free Download)", duration_s=lib_duration)
    m = find_duplicate(storage, make_track("7", title="track 7", duration_s=180.0))
    if matches:
        assert m is not None and m.reason is DuplicateReason.ARTIST_TITLE
        assert m.path == library_dir / "a.mp3"
    else:
        assert m is None


def test_find_duplicate_float_noise_at_boundary(storage: Storage, make_track: Callable,
                                                library_dir: Path) -> None:
    _lib_file(storage, library_dir / "a.mp3", artist="Test Artist", title="Track 7",
              duration_s=182.3)
    assert find_duplicate(storage, make_track("7", duration_s=180.3)) is not None


def test_find_duplicate_artist_title_requires_known_duration_and_title(
        storage: Storage, make_track: Callable, library_dir: Path) -> None:
    _lib_file(storage, library_dir / "a.mp3", artist="Test Artist", title="Track 7",
              duration_s=180.0)
    assert find_duplicate(storage, make_track("7", duration_s=None)) is None
    _lib_file(storage, library_dir / "b.mp3", artist="Test Artist", title="",
              duration_s=180.0)
    assert find_duplicate(storage, make_track("8", title="", duration_s=180.0)) is None


def test_find_duplicate_artist_title_ignores_missing_file(
        storage: Storage, make_track: Callable, library_dir: Path) -> None:
    path = library_dir / "a.mp3"
    _lib_file(storage, path, artist="Test Artist", title="Track 7", duration_s=180.0)
    path.unlink()
    assert find_duplicate(storage, make_track("7")) is None


def test_find_duplicate_matches_refined_artist(storage: Storage, make_track: Callable,
                                               library_dir: Path) -> None:
    # File was tagged "Real Artist - Song" -> rescan stored the refined artist.
    _lib_file(storage, library_dir / "a.mp3", artist="Real Artist", title="Song",
              duration_s=180.0)
    track = make_track("9", artist="Big Records", title="Real Artist - Song")
    m = find_duplicate(storage, track)
    assert m is not None and m.reason is DuplicateReason.ARTIST_TITLE


def test_find_duplicate_writes_nothing(storage: Storage, make_track: Callable) -> None:
    find_duplicate(storage, make_track("1"))
    assert storage.count_library_files() == 0
    assert storage.get_track("1") is None
