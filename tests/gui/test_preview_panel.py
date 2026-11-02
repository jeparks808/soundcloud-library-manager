from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt

from sclm.core.errors import InvalidUrlError
from sclm.core.models import (
    PlaylistInfo,
    PlaylistPreview,
    PlaylistSyncResult,
    PreviewEntry,
)
from sclm.gui.bridge import NOT_AVAILABLE_MESSAGE
from sclm.gui.preview_panel import COL_CHECK, PreviewPanel


def make_preview(make_track, n=6, existing=(1, 3), single=False) -> PlaylistPreview:
    tracks = tuple(make_track(str(i), playlist_index=i + 1) for i in range(n))
    entries = tuple(PreviewEntry(t, Path(f"/lib/{t.track_id}.mp3") if i in existing else None)
                    for i, t in enumerate(tracks))
    if single:
        info = PlaylistInfo(None, tracks[0].url, tracks[0].title, entries=(tracks[0],))
        return PlaylistPreview(info, entries[:1])
    info = PlaylistInfo("pl-1", "https://soundcloud.com/a/sets/mix", "Mix Tape", "DJ X", tracks)
    return PlaylistPreview(info, entries, PlaylistSyncResult(added=("1", "2"), removed=("9",)))


def test_preview_populates_table_and_preselects_only_new(qtbot, ctx, make_track):
    panel = PreviewPanel(ctx)
    qtbot.addWidget(panel)
    panel.show_preview(make_preview(make_track))

    assert panel.model.rowCount() == 6
    assert panel.only_new_check.isChecked()               # settings default
    assert panel.model.checked_count() == 4               # 6 minus 2 already in library
    idx = panel.model.index(1, COL_CHECK)
    assert panel.model.data(idx, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert "Mix Tape" in panel.summary_label.text()
    assert "2 added, 1 removed" in panel.sync_label.text()
    assert panel.download_btn.isEnabled()
    assert "4" in panel.download_btn.text()


def test_selection_produces_download_requests(qtbot, ctx, make_track):
    panel = PreviewPanel(ctx)
    qtbot.addWidget(panel)
    preview = make_preview(make_track)
    panel.show_preview(preview)

    reqs = panel.selected_requests()
    assert [r.track.track_id for r in reqs] == ["0", "2", "4", "5"]
    assert all(r.playlist_id == "pl-1" and r.playlist_title == "Mix Tape" for r in reqs)
    assert all(not r.force for r in reqs)

    panel.only_new_check.setChecked(False)                # all entries
    assert len(panel.selected_requests()) == 6
    panel.select_none_btn.click()
    assert panel.selected_requests() == []
    assert not panel.download_btn.isEnabled()

    # toggling a single checkbox through the model
    panel.model.setData(panel.model.index(3, COL_CHECK), Qt.CheckState.Checked.value,
                        Qt.ItemDataRole.CheckStateRole)
    assert [r.track.track_id for r in panel.selected_requests()] == ["3"]


def test_single_track_has_no_playlist_context(qtbot, ctx, make_track):
    panel = PreviewPanel(ctx)
    qtbot.addWidget(panel)
    panel.show_preview(make_preview(make_track, single=True))
    panel.only_new_check.setChecked(False)
    (req,) = panel.selected_requests()
    assert req.playlist_id is None and req.playlist_title is None


def test_download_selected_enqueues_and_signals(qtbot, ctx, manager, make_track):
    panel = PreviewPanel(ctx)
    qtbot.addWidget(panel)
    panel.show_preview(make_preview(make_track))
    with qtbot.waitSignal(panel.enqueued) as sig:
        panel.download_btn.click()
    assert sig.args == [4]
    assert [r.track.track_id for r in manager.calls[0][1]] == ["0", "2", "4", "5"]


def test_enqueue_not_implemented_shows_message_not_crash(qtbot, unfinished_ctx, make_track):
    panel = PreviewPanel(unfinished_ctx)
    qtbot.addWidget(panel)
    panel.show_preview(make_preview(make_track))
    panel.download_btn.click()
    assert panel.error_banner.isVisibleTo(panel)
    assert "isn't available" in panel.error_banner.message


def test_fetch_runs_in_background_and_fills_table(qtbot, ctx, make_track):
    calls = {}
    preview = make_preview(make_track)

    def fake_fetch(url, settings, storage, *, cancel=None):
        calls["thread"] = threading.current_thread().name
        calls["url"] = url
        calls["cancel"] = cancel
        return preview

    panel = PreviewPanel(ctx, fetch_fn=fake_fetch)
    qtbot.addWidget(panel)
    panel.show()
    panel.url_edit.setText("  https://soundcloud.com/a/sets/mix  ")
    qtbot.keyClick(panel.url_edit, Qt.Key.Key_Return)          # Enter fetches
    assert panel.fetching and panel.busy.isVisible()
    qtbot.waitUntil(lambda: panel.model.rowCount() == 6, timeout=3000)
    assert calls["thread"] != threading.main_thread().name
    assert calls["url"] == "https://soundcloud.com/a/sets/mix"
    assert isinstance(calls["cancel"], threading.Event)
    assert not panel.fetching and not panel.busy.isVisible()


def test_fetch_error_is_shown_inline(qtbot, ctx):
    def failing(url, settings, storage, *, cancel=None):
        raise InvalidUrlError("That doesn't look like a SoundCloud link.", detail="trace")

    panel = PreviewPanel(ctx, fetch_fn=failing)
    qtbot.addWidget(panel)
    panel.show()
    panel.url_edit.setText("https://example.com/x")
    panel.fetch_btn.click()
    qtbot.waitUntil(lambda: panel.error_banner.isVisible(), timeout=3000)
    assert "SoundCloud link" in panel.error_banner.message
    assert panel.error_banner.details == "trace"
    assert panel.fetch_btn.isEnabled()


def test_fetch_stub_not_implemented_is_graceful(qtbot, ctx):
    def stub(url, settings, storage, *, cancel=None):
        raise NotImplementedError

    panel = PreviewPanel(ctx, fetch_fn=stub)
    qtbot.addWidget(panel)
    panel.show()
    panel.url_edit.setText("https://soundcloud.com/a/b")
    panel.fetch()
    qtbot.waitUntil(lambda: panel.error_banner.isVisible(), timeout=3000)
    assert panel.error_banner.message == NOT_AVAILABLE_MESSAGE


def test_empty_url_does_not_fetch(qtbot, ctx):
    called = []
    panel = PreviewPanel(ctx, fetch_fn=lambda *a, **k: called.append(1))
    qtbot.addWidget(panel)
    panel.fetch()
    assert not called and not panel.fetching
    assert not panel.fetch_btn.isEnabled()
