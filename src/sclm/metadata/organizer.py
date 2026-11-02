"""Library layout and no-overwrite file placement (docs/ARCHITECTURE.md §5, §12).

Placement never overwrites anything:

* same filesystem: ``os.link(src, dest)`` (fails with ``EEXIST`` if ``dest`` is taken),
  then ``os.unlink(src)``;
* different filesystem (``EXDEV``) or a filesystem without hard links (``EPERM``,
  ``ENOTSUP``... e.g. FAT/exFAT USB drives): copy to ``dest.parent/.sclm-<rand>.part``,
  fsync, ``os.link(part, dest)``, unlink the part file and then ``src``. If the
  destination filesystem has no hard links at all, the final step creates ``dest`` with
  ``O_CREAT | O_EXCL`` (still never overwrites) and copies into it.

``shutil.move`` and ``os.replace`` are never used on a library path.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
from pathlib import Path

from sclm.core.errors import FileSystemError
from sclm.core.models import OrganizeMode, TrackInfo
from sclm.core.settings import Settings
from sclm.metadata.genre import normalize_genre
from sclm.metadata.naming import render_filename, sanitize_component

log = logging.getLogger(__name__)

MAX_SUFFIX = 999
SINGLES_FOLDER = "Singles"
UNKNOWN_ARTIST_FOLDER = "Unknown Artist"
_PART_PREFIX = ".sclm-"
# link() errors meaning "this filesystem can't hard-link here" -> copy fallback.
_NO_LINK_ERRNOS = frozenset({errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP,
                             errno.EMLINK, errno.ENOSYS})
_COPY_CHUNK = 1024 * 1024


def plan_folder(settings: Settings, track: TrackInfo, playlist_title: str | None) -> Path:
    """Destination folder for ``track`` under the library, per ``settings.organize_mode``."""
    lib = settings.library_path
    mode = OrganizeMode(settings.organize_mode)
    if mode is OrganizeMode.PLAYLIST:
        return lib / sanitize_component(playlist_title or "", fallback=SINGLES_FOLDER)
    artist = sanitize_component(track.artist, fallback=UNKNOWN_ARTIST_FOLDER)
    if mode is OrganizeMode.ARTIST:
        return lib / artist
    if mode is OrganizeMode.ALBUM:
        return lib / artist / sanitize_component(track.album or playlist_title or "",
                                                 fallback=SINGLES_FOLDER)
    return lib / sanitize_component(normalize_genre(track.genre), fallback="Unknown")


def plan_destination(settings: Settings, track: TrackInfo,
                     playlist_title: str | None) -> Path:
    """Full destination path (folder + rendered filename + ``.mp3``)."""
    name = render_filename(settings.filename_template, track, playlist_title=playlist_title)
    return plan_folder(settings, track, playlist_title) / f"{name}.mp3"


def suffixed(dest: Path, n: int) -> Path:
    """``dest`` with `` (n)`` before the extension; ``n == 1`` returns ``dest``."""
    return dest if n == 1 else dest.with_name(f"{dest.stem} ({n}){dest.suffix}")


def _fs_error(exc: OSError, dest: Path) -> FileSystemError:
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        msg = f"No permission to write to {dest.parent}."
    elif exc.errno == errno.ENOSPC:
        msg = "The library drive is full."
    else:
        msg = f"Could not place the file in {dest.parent}."
    return FileSystemError(msg, detail=repr(exc))


def _copy_into(src: Path, fd: int) -> None:
    with open(src, "rb") as fin, os.fdopen(fd, "wb", closefd=False) as fout:
        while chunk := fin.read(_COPY_CHUNK):
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def _make_part_copy(src: Path, folder: Path) -> Path:
    """Copy ``src`` to a hidden, fsynced part file in ``folder`` (created exclusively)."""
    for _ in range(20):
        part = folder / f"{_PART_PREFIX}{secrets.token_hex(6)}.part"
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        try:
            _copy_into(src, fd)
        except BaseException:
            os.close(fd)
            _unlink_quietly(part)
            raise
        os.close(fd)
        return part
    raise FileExistsError(errno.EEXIST, "could not create a unique part file", str(folder))


def _exclusive_copy(src: Path, dest: Path) -> None:
    """Create ``dest`` exclusively (``O_EXCL``) and copy ``src`` into it. Raises
    FileExistsError if ``dest`` exists; removes the partial ``dest`` on failure."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        _copy_into(src, fd)
    except BaseException:
        os.close(fd)
        _unlink_quietly(dest)
        raise
    os.close(fd)


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


def place_file(src: Path, dest: Path, *, keep_both: bool) -> Path | None:
    """Move ``src`` to ``dest`` without ever overwriting an existing file.

    If ``dest`` is taken: ``keep_both=False`` -> return ``None`` (``src`` untouched);
    ``keep_both=True`` -> try ``"<stem> (2).mp3"``, ``" (3)"`` ... up to ``MAX_SUFFIX``.
    Returns the final path. Raises FileSystemError for any other failure (``src`` is then
    left in place and no partial file remains in the library).
    """
    src, dest = Path(src), Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fs_error(exc, dest) from exc

    part: Path | None = None        # copy in the destination dir (cross-device path)
    no_links = False                # destination fs can't hard-link at all
    try:
        for n in range(1, MAX_SUFFIX + 1):
            candidate = suffixed(dest, n)
            try:
                if no_links:
                    _exclusive_copy(src, candidate)
                elif part is not None:
                    os.link(part, candidate)
                else:
                    try:
                        os.link(src, candidate)
                    except OSError as exc:
                        if exc.errno not in _NO_LINK_ERRNOS:
                            raise
                        log.debug("link %s -> %s failed (%s); copying", src, candidate, exc)
                        part = _make_part_copy(src, candidate.parent)
                        try:
                            os.link(part, candidate)
                        except OSError as exc2:
                            if exc2.errno not in _NO_LINK_ERRNOS:
                                raise
                            no_links = True
                            _unlink_quietly(part)
                            part = None
                            _exclusive_copy(src, candidate)
            except FileExistsError:
                if not keep_both:
                    return None
                continue
            except OSError as exc:
                raise _fs_error(exc, candidate) from exc
            _unlink_quietly(src)
            return candidate
        raise FileSystemError(f"Too many files named like {dest.name}.",
                              detail=f"tried up to suffix ({MAX_SUFFIX})")
    finally:
        if part is not None:
            _unlink_quietly(part)
