from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt

from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import JobStatus, LibraryFile, LibrarySource, ScanResult
from sclm.gui.history_view import FILTERS, HistoryView
from sclm.gui.library_view import LibraryView


def _seed_history(storage, make_track):
    ids = {}
    for i, status in enumerate([JobStatus.COMPLETED, JobStatus.FAILED,
                                JobStatus.SKIPPED_DUPLICATE, JobStatus.COMPLETED], start=1):
        jid = storage.create_download(make_track(str(i)))
        err = ErrorInfo(ErrorCategory.NETWORK, "Network down", "socket timeout") \
            if status == JobStatus.FAILED else None
        storage.update_download(jid, status=status, attempts=1, error=err,
                                file_path=Path(f"/m/{i}.mp3") if err is None else None)
        ids[i] = jid
    return ids


def test_history_lists_filters_and_shows_error_details(qtbot, ctx, storage, make_track):
    _seed_history(storage, make_track)
    view = HistoryView(ctx)
    qtbot.addWidget(view)
    view.refresh()
    assert view.model.rowCount() == 4
    assert view.model.record_at(0).track_id == "4"          # newest first

    failed_idx = next(i for i, (label, _s) in enumerate(FILTERS) if label == "Failed")
    view.filter_combo.setCurrentIndex(failed_idx)
    assert view.model.rowCount() == 1
    view.table.selectRow(0)
    text = view.details.toPlainText()
    assert "Network down" in text and "socket timeout" in text
    assert not view.prev_btn.isEnabled() and not view.next_btn.isEnabled()


def test_history_pages(qtbot, ctx, storage, make_track, monkeypatch):
    import sclm.gui.history_view as hv

    monkeypatch.setattr(hv, "PAGE_SIZE", 2)
    _seed_history(storage, make_track)
    view = HistoryView(ctx)
    qtbot.addWidget(view)
    view.refresh()
    assert view.model.rowCount() == 2 and view.next_btn.isEnabled()
    view.next_btn.click()
    assert view.model.rowCount() == 2 and view.model.record_at(0).track_id == "2"
    assert view.prev_btn.isEnabled() and not view.next_btn.isEnabled()


def _seed_library(storage, n=5):
    for i in range(n):
        storage.add_library_file(LibraryFile(Path(f"/lib/s{i}.mp3"), LibrarySource.DOWNLOAD,
                                             artist=f"Artist {i % 2}", title=f"Song {i}",
                                             duration_s=60.0 + i))


def test_library_lists_and_searches(qtbot, ctx, storage):
    _seed_library(storage)
    view = LibraryView(ctx)
    qtbot.addWidget(view)
    view.refresh()
    assert view.model.rowCount() == 5
    assert "5 file(s)" in view.count_label.text()
    assert not view.edit_btn.isEnabled()
    view.table.selectRow(0)
    assert view.edit_btn.isEnabled() and view.folder_btn.isEnabled()

    view.search_edit.setText("Song 3")
    qtbot.waitUntil(lambda: view.model.rowCount() == 1, timeout=3000)
    assert view.model.index(0, 1).data() == "Song 3"


def test_library_scan_runs_in_background_with_progress(qtbot, ctx, storage):
    seen = {}

    def fake_scan(root, st, *, progress=None, cancel=None):
        seen["thread"] = threading.current_thread().name
        seen["root"] = root
        progress(1, 2)
        progress(2, 2)
        storage.add_library_file(LibraryFile(Path("/lib/new.mp3"), LibrarySource.SCAN,
                                             artist="New", title="Found"))
        return ScanResult(files_seen=2, added=1)

    view = LibraryView(ctx, scan_fn=fake_scan)
    qtbot.addWidget(view)
    changed = []
    view.library_changed.connect(lambda: changed.append(1))
    view.scan_btn.click()
    assert view.scanning and not view.scan_btn.isEnabled()
    qtbot.waitUntil(lambda: not view.scanning, timeout=3000)
    assert seen["thread"] != threading.main_thread().name
    assert seen["root"] == ctx.settings.get().library_path
    assert "2 file(s) seen, 1 added" in view.scan_label.text()
    assert view.model.rowCount() == 1 and changed
    assert view.scan_btn.isEnabled()


def test_library_scan_stub_is_graceful(qtbot, ctx):
    def stub(*a, **kw):
        raise NotImplementedError

    view = LibraryView(ctx, scan_fn=stub)
    qtbot.addWidget(view)
    view.show()
    view.scan_btn.click()
    qtbot.waitUntil(lambda: not view.scanning, timeout=3000)
    assert view.error_banner.isVisible()
    assert view.scan_btn.isEnabled()


def test_library_edit_opens_editor_for_selection(qtbot, ctx, storage):
    _seed_library(storage, 2)
    opened = []
    view = LibraryView(ctx, editor_factory=opened.append)
    qtbot.addWidget(view)
    view.refresh()
    view.table.selectRow(1)
    view.edit_btn.click()
    assert opened == [Path("/lib/s1.mp3")]
    assert view.model.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "Artist 0"
