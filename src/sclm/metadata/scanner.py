"""Library scanner: index existing MP3s for duplicate detection."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from sclm.core.errors import FileSystemError, JobCancelled
from sclm.core.models import LibraryFile, LibrarySource, ScanResult
from sclm.core.storage import Storage
from sclm.metadata.duplicates import audio_sha256
from sclm.metadata.tagger import read_mp3

log = logging.getLogger(__name__)

# SCLM's own temp/part files (organizer ".sclm-*.part", editor ".sclm-edit-*.tmp").
_OWN_TEMP_PREFIX = ".sclm-"


def _find_mp3s(root: Path, errors: list[str]) -> list[Path]:
    found: list[Path] = []

    def onerror(exc: OSError) -> None:
        errors.append(f"{exc.filename}: {exc.strerror or exc}")

    # followlinks=False: never loop through symlinked directories.
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(".mp3") and not name.startswith(_OWN_TEMP_PREFIX):
                found.append(Path(dirpath) / name)
    return found


def _title_from_stem(stem: str) -> tuple[str, str]:
    """Untagged file: ``"Artist - Title"`` stems give (artist, title), else ("", stem)."""
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        if artist.strip() and title.strip():
            return artist.strip(), title.strip()
    return "", stem.strip()


def scan_library(
    root: Path,
    storage: Storage,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> ScanResult:
    """Walk ``root`` for ``*.mp3`` and upsert a ``library_files`` row per file
    (source SCAN; artist/title/duration from tags, track_id from ``TXXX:SOUNDCLOUD_TRACK_ID``
    if present, ``audio_sha256``). Files unchanged since the last scan (same size and mtime)
    skip hashing. Rows under ``root`` whose file no longer exists are removed from the DB
    (files are never touched). Unreadable files are listed in ``ScanResult.errors``.

    Blocking; the GUI runs it via ``run_in_background``. ``progress(done, total)`` is called
    from the scanning thread. Raises JobCancelled if ``cancel`` is set.

    Details: the ``.mp3`` suffix match is case-insensitive; symlinked directories are not
    followed; SCLM's own hidden ``.sclm-*`` temp files are ignored. A file with no title
    tag uses its file name (``"Artist - Title"`` names are split). Unchanged files count in
    ``files_seen`` only. ``errors`` entries look like ``"<path>: <reason>"``.
    """
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise FileSystemError(f"The library folder {root} does not exist.")

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise JobCancelled()

    errors: list[str] = []
    check_cancel()
    files = _find_mp3s(root, errors)
    total = len(files)
    if progress is not None:
        progress(0, total)
    added = updated = 0
    for done, path in enumerate(files, start=1):
        check_cancel()
        try:
            st = path.stat()
            existing = storage.get_library_file(path)
            unchanged = (existing is not None and existing.audio_sha256
                         and existing.size_bytes == st.st_size
                         and existing.mtime is not None
                         and abs(existing.mtime - st.st_mtime) < 1e-6)
            if not unchanged:
                info = read_mp3(path)
                title, artist = info.title, info.artist
                if not title and not artist:
                    artist, title = _title_from_stem(path.stem)
                elif not title:
                    title = path.stem
                storage.add_library_file(LibraryFile(
                    path=path, source=LibrarySource.SCAN, track_id=info.track_id,
                    artist=artist, title=title, duration_s=info.duration_s,
                    audio_sha256=audio_sha256(path), size_bytes=st.st_size,
                    mtime=st.st_mtime))
                if existing is None:
                    added += 1
                else:
                    updated += 1
        except FileSystemError as exc:
            errors.append(f"{path}: {exc.user_message}")
            log.warning("Scan: skipped %s: %s", path, exc.detail or exc.user_message)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror or exc}")
            log.warning("Scan: skipped %s: %s", path, exc)
        if progress is not None:
            progress(done, total)

    check_cancel()
    removed = 0
    for known in storage.library_paths_under(root):
        if not known.exists() and storage.remove_library_file(known):
            removed += 1
    return ScanResult(files_seen=total, added=added, updated=updated,
                      removed_missing=removed, errors=tuple(errors))
