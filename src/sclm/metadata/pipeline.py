"""Post-download pipeline: tag -> content-hash check -> organize (Stage 4 implements).

STUB. Signature and documented behavior are the contract (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from sclm.core.models import FinalizeResult, JobStatus, TrackInfo
from sclm.core.settings import Settings
from sclm.core.storage import Storage


def finalize_track(
    staged_file: Path,
    track: TrackInfo,
    *,
    playlist_title: str | None,
    settings: Settings,
    storage: Storage,
    force: bool = False,
    on_stage: Callable[[JobStatus], None] | None = None,
    cancel: threading.Event | None = None,
) -> FinalizeResult:
    """Tag a converted MP3 in the staging dir and place it in the library.

    Called by ``DownloadManager`` on its worker thread, after conversion. Steps:
      1. ``on_stage(TAGGING)``. Normalize genre; fetch artwork (if ``settings.embed_artwork``
         and ``track.artwork_url``; failure -> warning, not error); write ID3v2.3 tags + APIC
         to ``staged_file`` in place.
      2. ``audio_sha256(staged_file)``. Unless ``force``: a library file with the same hash
         -> return DUPLICATE (reason CONTENT_HASH), subject to ``settings.conflict_policy``
         (KEEP_BOTH continues).
      3. ``on_stage(ORGANIZING)``. Destination = ``settings.library_path`` / folder (from
         ``organize_mode``) / ``render_filename(...) + ".mp3"``. If it exists: SKIP/ASK ->
         return DUPLICATE (reason PATH_EXISTS); KEEP_BOTH or ``force`` -> add " (2)", " (3)"...
      4. Move without ever overwriting (``os.link`` + unlink on the same filesystem; on
         ``EXDEV`` copy to a hidden temp name in the destination dir, then ``os.link``).
      5. If ``settings.beets_enabled``: best-effort Beets import of the placed file (warnings
         only, never fails the job).
    Never writes to the DB (reads only, for the hash lookup). On PLACED, ``staged_file`` no
    longer exists; on DUPLICATE it is left for the caller to discard with the staging dir.

    Raises: FileSystemError (tag write / move failed), JobCancelled (``cancel`` set before
    the move). After the move succeeds it returns PLACED even if ``cancel`` is set.
    """
    raise NotImplementedError
