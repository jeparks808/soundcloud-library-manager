"""Real beets integration, isolated in tmp_path (BEETSDIR + explicit library DB).

The autouse fixture in tests/conftest.py also points HOME at tmp, so nothing here can touch
the user's ~/.config/beets.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sclm.core.models import FinalizeOutcome
from sclm.core.settings import Settings
from sclm.core.storage import Storage
from sclm.metadata import beets_bridge
from sclm.metadata.pipeline import finalize_track


def test_is_available_returns_bool() -> None:
    assert isinstance(beets_bridge.is_available(), bool)


@pytest.fixture
def beets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("beets")
    beetsdir = tmp_path / "beetsdir"
    beetsdir.mkdir()
    # Even if the user config enabled copy/move/write, the bridge must not touch the file.
    (beetsdir / "config.yaml").write_text(
        f"directory: {tmp_path / 'beets-music'}\n"
        f"library: {beetsdir / 'library.db'}\n"
        "import:\n  copy: yes\n  move: yes\n  write: yes\n")
    monkeypatch.setenv("BEETSDIR", str(beetsdir))
    monkeypatch.setattr(beets_bridge, "_config_loaded", False)
    from beets import config
    config.clear()
    config._materialized = False  # type: ignore[attr-defined]
    return beetsdir


def _items(db: Path) -> list[bytes]:
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT path FROM items")]


def test_import_file_adds_then_refreshes_without_touching_file(
        beets_env: Path, tiny_mp3: Path, tmp_path: Path) -> None:
    db = tmp_path / "explicit.db"
    before = tiny_mp3.read_bytes()
    stat_before = tiny_mp3.stat()
    assert beets_bridge.import_file(tiny_mp3, library_db=db) is True
    assert beets_bridge.import_file(tiny_mp3, library_db=db) is False   # update, no dupe
    assert _items(db) == [os.fsencode(str(tiny_mp3))]
    assert tiny_mp3.read_bytes() == before
    assert tiny_mp3.stat().st_mtime == stat_before.st_mtime
    assert not (tmp_path / "beets-music").exists()       # nothing copied/moved


def test_finalize_with_real_beets(beets_env: Path, tiny_mp3: Path, make_track: Callable,
                                  settings: Settings, storage: Storage,
                                  tmp_path: Path) -> None:
    result = finalize_track(tiny_mp3, make_track("1"), playlist_title=None,
                            settings=settings.replace(beets_enabled=True,
                                                      embed_artwork=False),
                            storage=storage)
    assert result.outcome is FinalizeOutcome.PLACED
    assert result.warnings == ()
    assert result.final_path.is_file()
    # configured library from BEETSDIR/config.yaml
    assert _items(beets_env / "library.db") == [os.fsencode(str(result.final_path))]
    assert not (tmp_path / "beets-music").exists()
