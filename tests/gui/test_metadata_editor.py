from __future__ import annotations

import threading
from pathlib import Path

from sclm.core.errors import FileSystemError
from sclm.core.models import LibraryFile, LibrarySource, TagData
from sclm.gui.metadata_editor import MetadataEditorDialog


def test_editor_loads_edits_and_saves_through_save_tags(qtbot, ctx, tmp_path):
    path = tmp_path / "song.mp3"
    calls = {}
    original = TagData(title="Old", artist="A", album="Alb", genre="House", year=2020,
                       track_number=3, comment="hi", track_id="777",
                       source_url="https://soundcloud.com/a/b", duration_s=185.0,
                       has_artwork=True)

    def read_fn(p):
        calls["read_thread"] = threading.current_thread().name
        return original

    def save_fn(p, tags, storage, *, artwork=None, remove_artwork=False):
        calls.update(path=p, tags=tags, storage=storage, artwork=artwork,
                     remove=remove_artwork, save_thread=threading.current_thread().name)
        return LibraryFile(p, LibrarySource.DOWNLOAD, artist=tags.artist, title=tags.title)

    dialog = MetadataEditorDialog(ctx, path, read_fn=read_fn, save_fn=save_fn)
    qtbot.addWidget(dialog)
    assert not dialog.save_btn.isEnabled()                  # nothing editable while loading
    qtbot.waitUntil(lambda: dialog.save_btn.isEnabled(), timeout=3000)

    assert dialog.title_edit.text() == "Old"
    assert dialog.year_spin.value() == 2020 and dialog.track_spin.value() == 3
    assert dialog.ro_track_id.text() == "777"
    assert dialog.ro_url.text() == "https://soundcloud.com/a/b"
    assert dialog.ro_duration.text() == "3:05"
    assert calls["read_thread"] != threading.main_thread().name
    assert "save_thread" not in calls                       # no write before Save

    dialog.title_edit.setText("  New title ")
    dialog.genre_edit.setText("")
    dialog.year_spin.setValue(0)

    with qtbot.waitSignal(dialog.saved, timeout=3000) as sig:
        dialog.save_btn.click()
    assert calls["path"] == path and calls["storage"] is ctx.storage
    tags = calls["tags"]
    assert tags.title == "New title" and tags.genre == "" and tags.year is None
    assert tags.track_id == "777"                           # read-only fields carried over
    assert calls["artwork"] is None and calls["remove"] is False
    assert calls["save_thread"] != threading.main_thread().name
    assert sig.args[0].title == "New title"
    assert dialog.result() == dialog.DialogCode.Accepted


def test_save_error_is_shown_and_form_stays_editable(qtbot, ctx, tmp_path):
    def read_fn(p):
        return TagData(title="T")

    def save_fn(*a, **kw):
        raise FileSystemError("Could not write the tags.", detail="EACCES")

    dialog = MetadataEditorDialog(ctx, tmp_path / "x.mp3", read_fn=read_fn, save_fn=save_fn)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.save_btn.isEnabled(), timeout=3000)
    dialog.save_btn.click()
    qtbot.waitUntil(lambda: dialog.error_banner.isVisible(), timeout=3000)
    assert "Could not write" in dialog.error_banner.message
    assert dialog.save_btn.isEnabled() and dialog.title_edit.isEnabled()
    assert dialog.result() != dialog.DialogCode.Accepted


def test_load_error_leaves_form_disabled(qtbot, ctx, tmp_path):
    def read_fn(p):
        raise FileSystemError("Not an MP3 file.")

    dialog = MetadataEditorDialog(ctx, tmp_path / "x.txt", read_fn=read_fn)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.error_banner.isVisible(), timeout=3000)
    assert not dialog.save_btn.isEnabled()
    dialog.save()                                            # must be a no-op
    assert dialog.error_banner.message == "Not an MP3 file."


def test_stub_not_implemented_is_graceful(qtbot, ctx, tmp_path):
    def read_fn(p):
        raise NotImplementedError

    dialog = MetadataEditorDialog(ctx, tmp_path / "x.mp3", read_fn=read_fn)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.error_banner.isVisible(), timeout=3000)
    assert not dialog.save_btn.isEnabled()


def test_new_cover_is_passed_to_save(qtbot, ctx, tmp_path):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(0xFF5500)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    cover = tmp_path / "cover.png"
    cover.write_bytes(bytes(ba))

    got = {}

    def save_fn(p, tags, storage, *, artwork=None, remove_artwork=False):
        got["artwork"], got["remove"] = artwork, remove_artwork
        return LibraryFile(Path(p), LibrarySource.SCAN)

    dialog = MetadataEditorDialog(ctx, tmp_path / "x.mp3", read_fn=lambda p: TagData(),
                                  save_fn=save_fn)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.save_btn.isEnabled(), timeout=3000)
    dialog.set_cover_from_file(cover)
    qtbot.waitUntil(lambda: dialog._new_cover is not None, timeout=3000)
    with qtbot.waitSignal(dialog.saved, timeout=3000):
        dialog.save_btn.click()
    assert got["artwork"] == cover.read_bytes() and got["remove"] is False
