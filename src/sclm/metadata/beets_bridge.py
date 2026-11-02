"""Optional Beets integration (imported lazily; beets is an optional dependency).

What it does: adds (or refreshes) the placed file as an item in the user's beets library
database, using the beets library API. It is non-interactive and does **no** autotagging,
so no MusicBrainz lookups or prompts happen on a download worker.

What it never does: move, copy, rename, delete or write tags to the file. We never call
``Item.move``/``Item.write``/``beet import``; only ``Item.from_path`` (reads tags) and
``Library.add``/``Item.store`` (writes the beets DB) are used. The file stays exactly where
SCLM placed it, so beets' own ``import.copy``/``import.move``/``import.write`` settings do
not apply.

Configuration comes from the user's normal beets config (``$BEETSDIR`` or
``~/.config/beets/config.yaml``); the library DB is beets' ``library`` setting unless a
path is passed explicitly.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()   # beets' global config object is not thread-safe
_config_loaded = False


def is_available() -> bool:
    """True if the ``beets`` package can be imported (without importing it)."""
    try:
        return importlib.util.find_spec("beets") is not None
    except (ImportError, ValueError):
        return False


def import_file(path: Path, *, library_db: Path | None = None) -> bool:
    """Add or update ``path`` in the beets library DB without touching the file.

    Returns True if a new item was added, False if an existing item was refreshed.
    Raises whatever beets raises (callers treat any exception as a warning).
    ``library_db`` overrides beets' configured ``library`` (used by tests).
    """
    from beets import config  # lazy: optional dependency
    from beets.dbcore.query import PathQuery
    from beets.library import Item, Library

    path = Path(path).absolute()
    global _config_loaded
    with _lock:
        if not _config_loaded:   # read() appends sources; do it once per process
            config.read(user=True, defaults=True)
            _config_loaded = True
        db_path = Path(library_db) if library_db else Path(config["library"].as_filename())
        db_path.parent.mkdir(parents=True, exist_ok=True)
        directory = config["directory"].as_filename()
        lib = Library(str(db_path), directory)
        try:
            raw = os.fsencode(str(path))
            existing = list(lib.items(PathQuery("path", raw)))
            if existing:
                item = existing[0]
                item.read(str(path))   # re-read tags from the file
                item.store()           # DB only; never item.write()/move()
                log.info("Beets: refreshed %s", path)
                return False
            item = Item.from_path(str(path))
            lib.add(item)
            log.info("Beets: added %s", path)
            return True
        finally:
            lib._close()
