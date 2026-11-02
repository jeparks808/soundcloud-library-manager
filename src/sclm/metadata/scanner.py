"""Library scanner: index existing MP3s for duplicate detection (Stage 4 implements).

STUB. Signature and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from sclm.core.models import ScanResult
from sclm.core.storage import Storage


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
    """
    raise NotImplementedError
