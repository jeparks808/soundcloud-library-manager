"""Post-download pipeline: tag -> content-hash check -> organize -> optional Beets."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from sclm.core.errors import JobCancelled
from sclm.core.models import (
    ConflictPolicy, DuplicateMatch, DuplicateReason, FinalizeOutcome, FinalizeResult,
    JobStatus, TrackInfo,
)
from sclm.core.settings import Settings
from sclm.core.storage import Storage
from sclm.metadata import beets_bridge
from sclm.metadata.artwork import fetch_artwork
from sclm.metadata.duplicates import audio_sha256
from sclm.metadata.genre import normalize_genre
from sclm.metadata.organizer import place_file, plan_destination
from sclm.metadata.tagger import write_track_tags
from sclm.metadata.titles import refine_artist_title

log = logging.getLogger(__name__)

ARTWORK_WARNING = "Artwork could not be downloaded."


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled()


def _existing_hash_match(storage: Storage, digest: str) -> DuplicateMatch | None:
    for f in storage.find_library_files(audio_sha256=digest):
        if Path(f.path).is_file():
            return DuplicateMatch(DuplicateReason.CONTENT_HASH, Path(f.path), f.track_id)
    return None


def _run_beets(path: Path, importer: Callable[[Path], object] | None) -> str | None:
    """Best-effort Beets import. Returns a warning message or None."""
    if importer is None:
        if not beets_bridge.is_available():
            log.warning("Beets integration is enabled but beets is not installed")
            return "Beets is enabled but not installed; skipped Beets import."
        importer = beets_bridge.import_file
    try:
        importer(path)
    except Exception as exc:  # noqa: BLE001 - beets must never fail the job
        log.warning("Beets import of %s failed: %s", path, exc, exc_info=True)
        return f"Beets import failed: {exc}"
    return None


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
    artwork_fetcher: Callable[[str], bytes | None] | None = None,
    beets_importer: Callable[[Path], object] | None = None,
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

    Artist/title used for tags and the file name come from
    :func:`sclm.metadata.titles.refine_artist_title` (conservative "Artist - Title"
    parsing for label/channel uploads). Keyword-only test seams: ``artwork_fetcher(url)``
    (default :func:`sclm.metadata.artwork.fetch_artwork`) and ``beets_importer(path)``
    (default :func:`sclm.metadata.beets_bridge.import_file`).
    """
    staged_file = Path(staged_file)
    warnings: list[str] = []
    policy = ConflictPolicy(settings.conflict_policy)
    keep_both = force or policy is ConflictPolicy.KEEP_BOTH

    _check_cancel(cancel)
    if on_stage is not None:
        on_stage(JobStatus.TAGGING)

    effective = refine_artist_title(track)
    genre = normalize_genre(track.genre)
    album = track.album or playlist_title or None
    artwork: bytes | None = None
    if settings.embed_artwork and track.artwork_url:
        fetcher = artwork_fetcher or fetch_artwork
        try:
            artwork = fetcher(track.artwork_url)
        except Exception as exc:  # noqa: BLE001 - artwork is optional
            log.warning("Artwork fetch for track %s failed: %s", track.track_id, exc)
            artwork = None
        if not artwork:
            artwork = None
            warnings.append(ARTWORK_WARNING)
    _check_cancel(cancel)
    write_track_tags(staged_file, effective, genre=genre, album=album, artwork=artwork)

    _check_cancel(cancel)
    digest = audio_sha256(staged_file)
    if not force and policy is not ConflictPolicy.KEEP_BOTH:
        match = _existing_hash_match(storage, digest)
        if match is not None:
            log.info("Track %s has the same audio as %s; skipped", track.track_id, match.path)
            return FinalizeResult(FinalizeOutcome.DUPLICATE, audio_sha256=digest,
                                  duplicate=match, warnings=tuple(warnings))

    _check_cancel(cancel)
    if on_stage is not None:
        on_stage(JobStatus.ORGANIZING)
    dest = plan_destination(settings, effective, playlist_title)
    _check_cancel(cancel)   # last point before the library is touched
    final = place_file(staged_file, dest, keep_both=keep_both)
    if final is None:
        existing = storage.get_library_file(dest)
        log.info("Destination %s already exists; skipped track %s", dest, track.track_id)
        return FinalizeResult(
            FinalizeOutcome.DUPLICATE, audio_sha256=digest,
            duplicate=DuplicateMatch(DuplicateReason.PATH_EXISTS, dest,
                                     existing.track_id if existing else None),
            warnings=tuple(warnings))

    if settings.beets_enabled:
        warning = _run_beets(final, beets_importer)
        if warning:
            warnings.append(warning)
    return FinalizeResult(FinalizeOutcome.PLACED, final_path=final, audio_sha256=digest,
                          warnings=tuple(warnings))
