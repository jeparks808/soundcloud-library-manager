from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from sclm.core.models import JobStatus
from sclm.gui.main_window import (
    PAGE_DOWNLOAD,
    PAGE_HISTORY,
    PAGE_LIBRARY,
    PAGE_QUEUE,
    MainWindow,
)


@pytest.mark.parametrize("size", [(1440, 900), (1280, 800)])
def test_smoke_construct_at_target_sizes(qtbot, ctx, size):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.resize(*size)
    win.show()
    qtbot.waitExposed(win)
    assert win.current_page() == PAGE_DOWNLOAD
    assert win.windowTitle() == "SoundCloud Library Manager"
    for page in (PAGE_QUEUE, PAGE_LIBRARY, PAGE_HISTORY, PAGE_DOWNLOAD):
        win.show_page(page)
        assert win.current_page() == page
    assert win.queue_label.text() == "Queue: empty"
    assert win.library_label.text() == "Library: 0 file(s)"


def test_main_controls_have_accessible_names(qtbot, ctx):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    for widget in (win.preview_panel.url_edit, win.preview_panel.fetch_btn,
                   win.preview_panel.download_btn, win.queue_view.table,
                   win.queue_view.cancel_btn, win.library_view.search_edit,
                   win.history_view.table, win.settings_btn, win.nav):
        assert widget.accessibleName(), widget


def test_status_bar_tracks_queue_summary(qtbot, ctx, make_job):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    ctx.bridge.on_job_changed(make_job(1, JobStatus.DOWNLOADING))
    ctx.bridge.on_job_changed(make_job(2))
    ctx.bridge.on_job_changed(make_job(3, JobStatus.FAILED))
    qtbot.waitUntil(lambda: "1 active" in win.queue_label.text(), timeout=3000)
    assert win.queue_label.text() == "Queue: 1 active, 1 queued, 1 failed"


def test_enqueue_switches_to_queue_page(qtbot, ctx, make_track):
    from sclm.core.models import PlaylistInfo, PlaylistPreview, PreviewEntry

    win = MainWindow(ctx)
    qtbot.addWidget(win)
    track = make_track("5")
    preview = PlaylistPreview(PlaylistInfo(None, track.url, track.title, entries=(track,)),
                              (PreviewEntry(track),))
    win.preview_panel.show_preview(preview)
    win.preview_panel.download_btn.click()
    assert win.current_page() == PAGE_QUEUE
    assert win.statusBar().currentMessage().startswith("Queued 1")


def test_focus_url_shortcut_target(qtbot, ctx):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    win.show_page(PAGE_HISTORY)
    win.focus_url()
    assert win.current_page() == PAGE_DOWNLOAD
    assert win.focusWidget() is win.preview_panel.url_edit


# --- close confirmation -------------------------------------------------------------------

def test_close_when_idle_does_not_ask(qtbot, ctx, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1))
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    assert win.close() is True
    assert asked == []


def test_close_when_busy_asks_and_respects_no(qtbot, ctx, manager, monkeypatch):
    manager.idle = False
    asked = []

    def answer_no(parent, title, text, buttons, default):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", answer_no)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    assert win.close() is False
    assert win.isVisible() and len(asked) == 1
    assert "cancel" in asked[0].lower()


def test_close_when_busy_and_confirmed_closes(qtbot, ctx, manager, monkeypatch):
    manager.idle = False
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    assert win.close() is True
    assert not win.isVisible()


def test_window_survives_unfinished_download_engine(qtbot, unfinished_ctx, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: pytest.fail("must not prompt on a broken manager"))
    win = MainWindow(unfinished_ctx)
    qtbot.addWidget(win)
    win.show()
    assert win.queue_view.model.rowCount() == 0
    assert win.close() is True


def test_geometry_is_saved_on_close(qtbot, ctx):
    from PySide6.QtCore import QSettings

    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    win.resize(1111, 777)
    win.close()
    assert QSettings().value("window/geometry") is not None
    again = MainWindow(ctx)                 # restoring must not raise
    qtbot.addWidget(again)


class _Result:
    def __init__(self, updated, version):
        self.updated, self.version, self.previous = updated, version, "2026.10.26"


@pytest.mark.parametrize(
    ("result", "title_part"),
    [(_Result(True, "2027.01.01"), "updated"), (_Result(False, "2026.10.26"), "up to date")],
)
def test_update_ytdlp_action_reports_result(qtbot, ctx, monkeypatch, result, title_part):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda _parent, title, text: shown.append((title, text)))
    win._update_ytdlp(update_fn=lambda: result)
    qtbot.waitUntil(lambda: bool(shown), timeout=4000)
    assert title_part in shown[0][0]
    assert result.version in shown[0][1]
    assert win._update_ytdlp_action.isEnabled()


def test_update_ytdlp_failure_shows_warning_and_reenables(qtbot, ctx, monkeypatch):
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, text: shown.append((title, text)))

    def boom():
        raise OSError("network down")

    win._update_ytdlp(update_fn=boom)
    qtbot.waitUntil(lambda: bool(shown), timeout=4000)
    assert "failed" in shown[0][0]
    assert win._update_ytdlp_action.isEnabled()
