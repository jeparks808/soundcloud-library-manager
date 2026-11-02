"""SQLite persistence: tracks, playlists + membership, download history, library files.

Thread safety: ONE connection (``check_same_thread=False``) guarded by an ``RLock``; every
public method takes the lock, so a ``Storage`` instance can be shared by the GUI thread and
all workers. Calls are short; never hold results across threads as live cursors (all
methods return plain dataclasses).

Journal mode is WAL (crash-safe, fast commits). Schema version lives in
``PRAGMA user_version``; :data:`MIGRATIONS` maps target version -> SQL script and is applied
in order on open. A database newer than :data:`SCHEMA_VERSION` raises :class:`StorageError`.

Paths are stored as absolute ``str``. Timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from sclm.core.errors import ErrorCategory, ErrorInfo, StorageError
from sclm.core.models import (
    DownloadRecord,
    JobStatus,
    LibraryFile,
    LibrarySource,
    PlaylistInfo,
    PlaylistMember,
    PlaylistRecord,
    PlaylistSyncResult,
    TrackInfo,
)
from sclm.core.normalize import match_key, utc_now_iso

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
INTERRUPTED_MESSAGE = "Interrupted: the app was closed before this download finished."
_CHUNK = 500


class _Clear(Enum):
    CLEAR = "clear"


CLEAR = _Clear.CLEAR
"""Sentinel for :meth:`Storage.update_download`: ``file_path=CLEAR`` sets the stored path to
NULL (``None`` keeps the stored value)."""

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE tracks (
        track_id     TEXT PRIMARY KEY,          -- SoundCloud track id
        url          TEXT NOT NULL,
        title        TEXT NOT NULL DEFAULT '',
        artist       TEXT NOT NULL DEFAULT '',
        duration_s   REAL,
        genre_raw    TEXT,                      -- untrusted, as delivered by SoundCloud
        album        TEXT,
        upload_date  TEXT,
        artwork_url  TEXT,
        uploader_id  TEXT,
        is_partial   INTEGER NOT NULL DEFAULT 0,
        match_key    TEXT NOT NULL DEFAULT '',
        first_seen   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX idx_tracks_match_key ON tracks(match_key);

    CREATE TABLE playlists (
        playlist_id  TEXT PRIMARY KEY,
        url          TEXT NOT NULL,
        title        TEXT NOT NULL,
        uploader     TEXT,
        first_seen   TEXT NOT NULL,
        last_synced  TEXT NOT NULL
    );

    CREATE TABLE playlist_tracks (
        playlist_id  TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        track_id     TEXT NOT NULL REFERENCES tracks(track_id),
        position     INTEGER,
        first_seen   TEXT NOT NULL,
        removed_at   TEXT,                      -- NULL while the track is in the playlist
        PRIMARY KEY (playlist_id, track_id)
    );
    CREATE INDEX idx_playlist_tracks_track ON playlist_tracks(track_id);

    CREATE TABLE downloads (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,   -- == DownloadJob.job_id
        track_id        TEXT NOT NULL REFERENCES tracks(track_id),
        playlist_id     TEXT,                   -- context only, no FK
        status          TEXT NOT NULL,          -- JobStatus value
        attempts        INTEGER NOT NULL DEFAULT 0,
        bitrate_kbps    INTEGER,
        file_path       TEXT,
        error_category  TEXT,
        error_message   TEXT,
        error_detail    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        completed_at    TEXT
    );
    CREATE INDEX idx_downloads_track_status ON downloads(track_id, status);
    CREATE INDEX idx_downloads_created ON downloads(created_at);

    CREATE TABLE library_files (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        path          TEXT NOT NULL UNIQUE,
        source        TEXT NOT NULL,            -- 'download' | 'scan'
        track_id      TEXT,                     -- NULL if unknown (scanned without SC id)
        artist        TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        match_key     TEXT NOT NULL DEFAULT '',
        duration_s    REAL,
        audio_sha256  TEXT,
        size_bytes    INTEGER,
        mtime         REAL,
        added_at      TEXT NOT NULL
    );
    CREATE INDEX idx_library_files_track ON library_files(track_id);
    CREATE INDEX idx_library_files_match_key ON library_files(match_key);
    CREATE INDEX idx_library_files_sha ON library_files(audio_sha256);
    """,
}

_TERMINAL_VALUES = tuple(s.value for s in JobStatus if s.is_terminal)
_TRACK_COLUMNS = ("track_id", "url", "title", "artist", "duration_s", "genre_raw", "album",
                  "upload_date", "artwork_url", "uploader_id", "is_partial")


def _chunks(items: list[str], size: int = _CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _merge_track(existing: TrackInfo | None, new: TrackInfo) -> TrackInfo:
    """Merge rule: a non-empty incoming value wins, except that a partial (flat) record
    never overwrites values already known; empty values never erase known ones."""
    if existing is None:
        return new
    if new.is_partial and not existing.is_partial:
        return existing

    def pick(a: Any, b: Any) -> Any:  # a = existing, b = new
        if new.is_partial:
            return a if a not in (None, "") else b
        return b if b not in (None, "") else a

    return TrackInfo(
        track_id=existing.track_id,
        url=pick(existing.url, new.url),
        title=pick(existing.title, new.title),
        artist=pick(existing.artist, new.artist),
        duration_s=pick(existing.duration_s, new.duration_s),
        genre=pick(existing.genre, new.genre),
        album=pick(existing.album, new.album),
        upload_date=pick(existing.upload_date, new.upload_date),
        artwork_url=pick(existing.artwork_url, new.artwork_url),
        uploader_id=pick(existing.uploader_id, new.uploader_id),
        playlist_index=new.playlist_index,
        is_partial=existing.is_partial and new.is_partial,
    )


class Storage:
    """Repository over the app's SQLite database. See module docstring for threading."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        try:
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False,
                                         isolation_level=None, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 10000")
            if self._db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error as exc:
            raise StorageError(detail=f"open {self._db_path}: {exc}") from exc
        self._closed = False
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    # --- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise StorageError(
                "This library database was created by a newer version of SCLM.",
                detail=f"db version {current} > supported {SCHEMA_VERSION}")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            log.info("Migrating database to schema version %d", version)
            script = MIGRATIONS[version]
            with self._lock:
                try:
                    # executescript commits implicitly; wrap so a failure leaves no partial
                    # schema behind (SQLite DDL is transactional).
                    self._conn.executescript(
                        f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
                except sqlite3.Error as exc:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise StorageError(detail=f"migration to v{version}: {exc}") from exc

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StorageError(detail="database is closed")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._conn
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                raise StorageError(detail=str(exc)) from exc

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            if self._closed:
                raise StorageError(detail="database is closed")
            try:
                return self._conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(detail=str(exc)) from exc

    # --- tracks ----------------------------------------------------------------

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> TrackInfo:
        return TrackInfo(
            track_id=row["track_id"], url=row["url"], title=row["title"],
            artist=row["artist"], duration_s=row["duration_s"], genre=row["genre_raw"],
            album=row["album"], upload_date=row["upload_date"],
            artwork_url=row["artwork_url"], uploader_id=row["uploader_id"],
            is_partial=bool(row["is_partial"]),
        )

    def _get_track(self, conn: sqlite3.Connection, track_id: str) -> TrackInfo | None:
        row = conn.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        return self._row_to_track(row) if row else None

    def _upsert_track(self, conn: sqlite3.Connection, track: TrackInfo, now: str) -> TrackInfo:
        merged = _merge_track(self._get_track(conn, track.track_id), track)
        values = (merged.track_id, merged.url, merged.title, merged.artist, merged.duration_s,
                  merged.genre, merged.album, merged.upload_date, merged.artwork_url,
                  merged.uploader_id, int(merged.is_partial))
        conn.execute(
            f"""INSERT INTO tracks ({", ".join(_TRACK_COLUMNS)}, match_key, first_seen, updated_at)
                VALUES ({", ".join("?" * len(_TRACK_COLUMNS))}, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                  url=excluded.url, title=excluded.title, artist=excluded.artist,
                  duration_s=excluded.duration_s, genre_raw=excluded.genre_raw,
                  album=excluded.album, upload_date=excluded.upload_date,
                  artwork_url=excluded.artwork_url, uploader_id=excluded.uploader_id,
                  is_partial=excluded.is_partial, match_key=excluded.match_key,
                  updated_at=excluded.updated_at""",
            (*values, merged.match_key, now, now),
        )
        return merged

    def upsert_track(self, track: TrackInfo) -> TrackInfo:
        """Insert or merge ``track`` (see merge rule in ``_merge_track``); returns stored value."""
        with self._tx() as conn:
            return self._upsert_track(conn, track, utc_now_iso())

    def get_track(self, track_id: str) -> TrackInfo | None:
        rows = self._query("SELECT * FROM tracks WHERE track_id = ?", (track_id,))
        return self._row_to_track(rows[0]) if rows else None

    # --- playlists -------------------------------------------------------------

    def sync_playlist(self, playlist: PlaylistInfo) -> PlaylistSyncResult:
        """Record a freshly fetched playlist: upsert the playlist and its tracks, update
        membership. New members get ``first_seen=now``; members that disappeared get
        ``removed_at=now``; members that came back get ``removed_at=NULL`` (first_seen kept).
        Raises ValueError for single-track results (``playlist_id is None``)."""
        if playlist.playlist_id is None:
            raise ValueError("sync_playlist needs a playlist, not a single track")
        pid = playlist.playlist_id
        now = utc_now_iso()
        added: list[str] = []
        removed: list[str] = []
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO playlists (playlist_id, url, title, uploader, first_seen, last_synced)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(playlist_id) DO UPDATE SET url=excluded.url,
                     title=excluded.title, uploader=excluded.uploader,
                     last_synced=excluded.last_synced""",
                (pid, playlist.url, playlist.title, playlist.uploader, now, now))
            existing = {r["track_id"]: r["removed_at"] for r in conn.execute(
                "SELECT track_id, removed_at FROM playlist_tracks WHERE playlist_id = ?", (pid,))}
            seen: set[str] = set()
            for pos, track in enumerate(playlist.entries, start=1):
                if track.track_id in seen:
                    continue
                seen.add(track.track_id)
                self._upsert_track(conn, track, now)
                position = track.playlist_index or pos
                if track.track_id not in existing:
                    conn.execute(
                        """INSERT INTO playlist_tracks (playlist_id, track_id, position, first_seen)
                           VALUES (?, ?, ?, ?)""", (pid, track.track_id, position, now))
                    added.append(track.track_id)
                else:
                    if existing[track.track_id] is not None:
                        added.append(track.track_id)
                    conn.execute(
                        """UPDATE playlist_tracks SET position = ?, removed_at = NULL
                           WHERE playlist_id = ? AND track_id = ?""",
                        (position, pid, track.track_id))
            for track_id, removed_at in existing.items():
                if track_id not in seen and removed_at is None:
                    conn.execute(
                        """UPDATE playlist_tracks SET removed_at = ?
                           WHERE playlist_id = ? AND track_id = ?""", (now, pid, track_id))
                    removed.append(track_id)
        return PlaylistSyncResult(added=tuple(added), removed=tuple(removed))

    def _row_to_playlist(self, row: sqlite3.Row) -> PlaylistRecord:
        return PlaylistRecord(
            playlist_id=row["playlist_id"], url=row["url"], title=row["title"],
            uploader=row["uploader"], first_seen=row["first_seen"],
            last_synced=row["last_synced"], track_count=row["track_count"])

    _PLAYLIST_SELECT = """
        SELECT p.*, (SELECT COUNT(*) FROM playlist_tracks pt
                     WHERE pt.playlist_id = p.playlist_id AND pt.removed_at IS NULL) AS track_count
        FROM playlists p"""

    def get_playlist(self, playlist_id: str) -> PlaylistRecord | None:
        rows = self._query(self._PLAYLIST_SELECT + " WHERE p.playlist_id = ?", (playlist_id,))
        return self._row_to_playlist(rows[0]) if rows else None

    def list_playlists(self) -> list[PlaylistRecord]:
        rows = self._query(self._PLAYLIST_SELECT + " ORDER BY p.title COLLATE NOCASE")
        return [self._row_to_playlist(r) for r in rows]

    def playlist_members(self, playlist_id: str, *,
                         include_removed: bool = False) -> list[PlaylistMember]:
        """Members ordered by position; removed members (if included) come last."""
        sql = """SELECT pt.position, pt.first_seen, pt.removed_at, t.*
                 FROM playlist_tracks pt JOIN tracks t ON t.track_id = pt.track_id
                 WHERE pt.playlist_id = ?"""
        if not include_removed:
            sql += " AND pt.removed_at IS NULL"
        sql += " ORDER BY pt.removed_at IS NOT NULL, pt.position"
        return [PlaylistMember(playlist_id=playlist_id, track=self._row_to_track(r),
                               position=r["position"], first_seen=r["first_seen"],
                               removed_at=r["removed_at"])
                for r in self._query(sql, (playlist_id,))]

    # --- downloads -------------------------------------------------------------

    def create_download(self, track: TrackInfo, *, playlist_id: str | None = None,
                        bitrate_kbps: int | None = None) -> int:
        """Upsert ``track`` and insert a QUEUED download row. Returns its id (== job_id)."""
        now = utc_now_iso()
        with self._tx() as conn:
            self._upsert_track(conn, track, now)
            cur = conn.execute(
                """INSERT INTO downloads (track_id, playlist_id, status, attempts, bitrate_kbps,
                                          created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (track.track_id, playlist_id, JobStatus.QUEUED.value, bitrate_kbps, now, now))
            return int(cur.lastrowid)

    def update_download(self, download_id: int, *, status: JobStatus,
                        attempts: int | None = None,
                        file_path: Path | Literal[_Clear.CLEAR] | None = None,
                        bitrate_kbps: int | None = None,
                        error: ErrorInfo | None = None) -> None:
        """Set status (and optionally attempts/file_path/bitrate). ``file_path=None`` keeps the
        stored path, :data:`CLEAR` sets it to NULL. ``error`` replaces the stored error (None
        clears it). ``completed_at`` is set when status becomes COMPLETED."""
        now = utc_now_iso()
        sets = ["status = ?", "updated_at = ?",
                "error_category = ?", "error_message = ?", "error_detail = ?"]
        params: list[Any] = [JobStatus(status).value, now,
                             error.category.value if error else None,
                             error.user_message if error else None,
                             error.detail if error else None]
        if attempts is not None:
            sets.append("attempts = ?")
            params.append(attempts)
        if file_path is CLEAR:
            sets.append("file_path = NULL")
        elif file_path is not None:
            sets.append("file_path = ?")
            params.append(str(file_path))
        if bitrate_kbps is not None:
            sets.append("bitrate_kbps = ?")
            params.append(bitrate_kbps)
        if status == JobStatus.COMPLETED:
            sets.append("completed_at = ?")
            params.append(now)
        params.append(download_id)
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE downloads SET {', '.join(sets)} WHERE id = ?", params)
            if cur.rowcount == 0:
                raise StorageError(detail=f"no download with id {download_id}")

    @staticmethod
    def _row_to_download(row: sqlite3.Row) -> DownloadRecord:
        error = None
        if row["error_category"]:
            try:
                category = ErrorCategory(row["error_category"])
            except ValueError:
                category = ErrorCategory.UNKNOWN
            error = ErrorInfo(category, row["error_message"] or "", row["error_detail"] or "")
        return DownloadRecord(
            id=row["id"], track_id=row["track_id"], playlist_id=row["playlist_id"],
            status=JobStatus(row["status"]), attempts=row["attempts"],
            bitrate_kbps=row["bitrate_kbps"],
            file_path=Path(row["file_path"]) if row["file_path"] else None,
            error=error, created_at=row["created_at"], updated_at=row["updated_at"],
            completed_at=row["completed_at"], title=row["title"] or "",
            artist=row["artist"] or "")

    _DOWNLOAD_SELECT = """SELECT d.*, t.title, t.artist FROM downloads d
                          LEFT JOIN tracks t ON t.track_id = d.track_id"""

    def get_download(self, download_id: int) -> DownloadRecord | None:
        rows = self._query(self._DOWNLOAD_SELECT + " WHERE d.id = ?", (download_id,))
        return self._row_to_download(rows[0]) if rows else None

    def download_history(self, *, limit: int = 200, offset: int = 0,
                         statuses: Iterable[JobStatus] | None = None) -> list[DownloadRecord]:
        """Newest first."""
        sql = self._DOWNLOAD_SELECT
        params: list[Any] = []
        if statuses is not None:
            values = [JobStatus(s).value for s in statuses]
            if not values:
                return []
            sql += f" WHERE d.status IN ({', '.join('?' * len(values))})"
            params.extend(values)
        sql += " ORDER BY d.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [self._row_to_download(r) for r in self._query(sql, params)]

    def mark_interrupted_downloads(self) -> int:
        """At startup: mark non-terminal rows (left over from a crash/exit) FAILED.
        Returns the number of rows changed."""
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute(
                f"""UPDATE downloads SET status = ?, updated_at = ?, error_category = ?,
                       error_message = ?, error_detail = ''
                    WHERE status NOT IN ({', '.join('?' * len(_TERMINAL_VALUES))})""",
                (JobStatus.FAILED.value, now, ErrorCategory.UNKNOWN.value,
                 INTERRUPTED_MESSAGE, *_TERMINAL_VALUES))
            return cur.rowcount

    def existing_track_paths(self, track_ids: Iterable[str]) -> dict[str, Path]:
        """Map track_id -> an existing library file for that SoundCloud id.

        Candidates: file_path of COMPLETED downloads (newest first), then library_files rows
        with that track_id. Only paths that exist on disk right now are returned. Track ids
        absent from the result are "new" (the "only new tracks" filter)."""
        ids = list(dict.fromkeys(track_ids))
        candidates: dict[str, list[str]] = {}
        for chunk in _chunks(ids):
            marks = ", ".join("?" * len(chunk))
            rows = self._query(
                f"""SELECT track_id, file_path AS path, 0 AS pri, id FROM downloads
                    WHERE status = ? AND file_path IS NOT NULL AND track_id IN ({marks})
                    UNION ALL
                    SELECT track_id, path, 1 AS pri, id FROM library_files
                    WHERE track_id IN ({marks})
                    ORDER BY pri, id DESC""",
                (JobStatus.COMPLETED.value, *chunk, *chunk))
            for r in rows:
                candidates.setdefault(r["track_id"], []).append(r["path"])
        result: dict[str, Path] = {}
        for track_id, paths in candidates.items():
            for p in paths:
                if Path(p).is_file():
                    result[track_id] = Path(p)
                    break
        return result

    def completed_download_path(self, track_id: str) -> Path | None:
        """Existing library file for ``track_id`` (see :meth:`existing_track_paths`)."""
        return self.existing_track_paths([track_id]).get(track_id)

    # --- library files -----------------------------------------------------------

    @staticmethod
    def _row_to_library_file(row: sqlite3.Row) -> LibraryFile:
        return LibraryFile(
            path=Path(row["path"]), source=LibrarySource(row["source"]),
            track_id=row["track_id"], artist=row["artist"], title=row["title"],
            duration_s=row["duration_s"], audio_sha256=row["audio_sha256"],
            size_bytes=row["size_bytes"], mtime=row["mtime"], added_at=row["added_at"],
            id=row["id"])

    def add_library_file(self, f: LibraryFile) -> int:
        """Insert or update the row for ``f.path`` (absolute). On update a known track_id is
        never erased and source 'download' is kept. Returns the row id."""
        path = str(Path(f.path).absolute())
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO library_files (path, source, track_id, artist, title, match_key,
                       duration_s, audio_sha256, size_bytes, mtime, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     source = CASE WHEN library_files.source = 'download' THEN 'download'
                                   ELSE excluded.source END,
                     track_id = COALESCE(excluded.track_id, library_files.track_id),
                     artist = excluded.artist, title = excluded.title,
                     match_key = excluded.match_key, duration_s = excluded.duration_s,
                     audio_sha256 = COALESCE(excluded.audio_sha256, library_files.audio_sha256),
                     size_bytes = excluded.size_bytes, mtime = excluded.mtime""",
                (path, LibrarySource(f.source).value, f.track_id, f.artist, f.title,
                 f.match_key, f.duration_s, f.audio_sha256, f.size_bytes, f.mtime,
                 utc_now_iso()))
            return int(conn.execute("SELECT id FROM library_files WHERE path = ?",
                                    (path,)).fetchone()[0])

    def get_library_file(self, path: Path) -> LibraryFile | None:
        rows = self._query("SELECT * FROM library_files WHERE path = ?",
                           (str(Path(path).absolute()),))
        return self._row_to_library_file(rows[0]) if rows else None

    def find_library_files(self, *, track_id: str | None = None, match_key: str | None = None,
                           audio_sha256: str | None = None) -> list[LibraryFile]:
        """Rows matching exactly one criterion. Does NOT check that files exist on disk."""
        given = {k: v for k, v in (("track_id", track_id), ("match_key", match_key),
                                   ("audio_sha256", audio_sha256)) if v is not None}
        if len(given) != 1:
            raise ValueError("pass exactly one of track_id, match_key, audio_sha256")
        (column, value), = given.items()
        if value == "":
            return []
        rows = self._query(f"SELECT * FROM library_files WHERE {column} = ? ORDER BY id",
                           (value,))
        return [self._row_to_library_file(r) for r in rows]

    def list_library_files(self, *, search: str | None = None, limit: int = 500,
                           offset: int = 0) -> list[LibraryFile]:
        """For the library view. ``search`` matches artist/title/path (case-insensitive)."""
        sql = "SELECT * FROM library_files"
        params: list[Any] = []
        if search and search.strip():
            like = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%") \
                .replace("_", "\\_") + "%"
            sql += (" WHERE artist LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
                    " OR path LIKE ? ESCAPE '\\'")
            params.extend([like, like, like])
        sql += " ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [self._row_to_library_file(r) for r in self._query(sql, params)]

    def library_paths_under(self, root: Path) -> list[Path]:
        """Paths of all library_files rows located under ``root`` (for the scanner)."""
        prefix = str(Path(root).absolute()).rstrip("/") + "/"
        rows = self._query("SELECT path FROM library_files WHERE substr(path, 1, ?) = ?",
                           (len(prefix), prefix))
        return [Path(r["path"]) for r in rows]

    def remove_library_file(self, path: Path) -> bool:
        """Delete the DB row for ``path`` (never touches the file). True if a row was removed."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM library_files WHERE path = ?",
                               (str(Path(path).absolute()),))
            return cur.rowcount > 0

    def count_library_files(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM library_files")[0][0])
