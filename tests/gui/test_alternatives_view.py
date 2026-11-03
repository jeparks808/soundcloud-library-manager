"""Alternatives page with fake search/check/load functions (no network)."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QMessageBox

from sclm.core.errors import ErrorCategory, ErrorInfo
from sclm.core.models import JobStatus
from sclm.download.alternatives import (
    Availability,
    Candidate,
    FailedTrack,
    MatchLevel,
    SourceCheck,
)
from sclm.gui import alternatives_view as av
from sclm.gui.main_window import PAGE_ALTERNATIVES, PAGE_LISTEN, MainWindow

OK_URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
DRM_URL = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
GONE_URL = "https://www.youtube.com/watch?v=ccccccccccc"
CHECKS = {OK_URL: SourceCheck(Availability.AUDIO, "Opus 147 kbps"),
          DRM_URL: SourceCheck(Availability.DRM_ONLY, "DRM only"),
          GONE_URL: SourceCheck(Availability.UNAVAILABLE, "This video is private")}


def _cand(url: str, title: str, delta: float, match: MatchLevel) -> Candidate:
    return Candidate(video_id=url[-11:], url=url, title=title, channel="Chris Travis",
                     channel_verified=True, duration_s=132.7 + delta, view_count=4_882_365,
                     delta_s=delta, match=match)


class Fakes:
    def __init__(self, items: list[FailedTrack]) -> None:
        self.items = items
        self.searches: list[tuple[str, float | None]] = []
        self.checks: list[str] = []
        self.threads: set[str] = set()
        self.search_error: Exception | None = None

    def load(self) -> list[FailedTrack]:
        self.threads.add(threading.current_thread().name)
        return list(self.items)

    def search(self, query: str, reference: float | None) -> list[Candidate]:
        self.threads.add(threading.current_thread().name)
        self.searches.append((query, reference))
        if self.search_error is not None:
            raise self.search_error
        return [_cand(OK_URL, "9k Freestyle (Official)", 2.3, MatchLevel.MATCH),
                _cand(DRM_URL, "9k Freestyle (label upload)", 0.4, MatchLevel.MATCH),
                _cand(GONE_URL, "9k Freestyle live", -34.7, MatchLevel.DIFFERENT)]

    def check(self, url: str) -> SourceCheck:
        self.threads.add(threading.current_thread().name)
        self.checks.append(url)
        return CHECKS[url]


@pytest.fixture
def failed(make_track):
    def make(track_id="109665566", title="9k Freestyle"):
        track = make_track(track_id, title=title, artist="Chris Travis", duration_s=132.7)
        return FailedTrack(track=track, error=ErrorInfo(ErrorCategory.DRM_PROTECTED,
                                                        "DRM-protected", "d"), download_id=1)
    return make


def _view(qtbot, ctx, fakes):
    view = av.AlternativesView(ctx, search_fn=fakes.search, check_fn=fakes.check,
                               load_fn=fakes.load)
    qtbot.addWidget(view)
    view.resize(1230, 800)
    view.refresh()
    return view


def _row_for(view, url):
    for row in range(view.candidate_model.rowCount()):
        if view.candidate_model.row_at(row).url == url:
            return row
    raise AssertionError(url)


def _all_checked(view):
    return all(view.candidate_model.checks.get(u) for u in CHECKS)


def test_failed_track_is_searched_checked_and_labelled(qtbot, ctx, failed):
    fakes = Fakes([failed()])
    view = _view(qtbot, ctx, fakes)
    qtbot.waitUntil(lambda: _all_checked(view))
    assert fakes.searches == [("Chris Travis 9k Freestyle", 132.7)]
    assert "MainThread" not in fakes.threads                # network work never on the GUI
    assert view.failed_model.data(view.failed_model.index(0, av.F_REASON)) == "DRM-protected"
    labels = {u: view.candidate_model.data(view.candidate_model.index(_row_for(view, u),
                                                                      av.C_SOURCE))
              for u in CHECKS}
    assert labels == {OK_URL: "OK · Opus 147 kbps", DRM_URL: "DRM only — can't download",
                      GONE_URL: "Unavailable: This video is private"}
    model = view.candidate_model
    assert model.data(model.index(0, av.C_DELTA)) == "+2.3 s"
    assert model.data(model.index(0, av.C_VIEWS)) == "4.9M"
    assert model.data(model.index(0, av.C_CHANNEL)) == "Chris Travis ✓"


def test_drm_only_and_unavailable_candidates_cannot_be_downloaded(qtbot, ctx, failed,
                                                                   monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a)
                        or QMessageBox.StandardButton.Yes)
    view = _view(qtbot, ctx, Fakes([failed()]))
    qtbot.waitUntil(lambda: _all_checked(view))
    for url in (DRM_URL, GONE_URL):
        view.candidate_table.selectRow(_row_for(view, url))
        assert not view.download_btn.isEnabled()
        view.download()                                     # even if called directly
    assert asked == [] and ctx.manager.calls == []
    view.candidate_table.selectRow(_row_for(view, DRM_URL))
    assert "DRM only" in view.download_btn.toolTip()


def test_confirmed_download_enqueues_the_youtube_source(qtbot, ctx, failed, monkeypatch):
    answers = [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes]
    texts = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _p, _t, text, *a, **k: texts.append(text) or answers.pop(0))
    item = failed()
    view = _view(qtbot, ctx, Fakes([item]))
    qtbot.waitUntil(lambda: _all_checked(view))
    view.candidate_table.selectRow(_row_for(view, OK_URL))
    assert view.download_btn.isEnabled()
    view.download_btn.click()                               # cancelled
    assert ctx.manager.calls == []
    view.download_btn.click()
    [(_, [request])] = ctx.manager.calls
    assert request.track is item.track and request.audio_source_url == OK_URL
    assert "lossy" in texts[0] and "allowed" in texts[0]
    assert not view.download_btn.isEnabled()                # queued: no second job
    [job] = ctx.manager.jobs()
    ctx.bridge.job_changed.emit(replace(job, status=JobStatus.FAILED))
    assert view.download_btn.isEnabled()                    # finished: may try another


def test_search_is_cached_per_track_and_can_be_edited(qtbot, ctx, failed):
    fakes = Fakes([failed("1", "One"), failed("2", "Two")])
    view = _view(qtbot, ctx, fakes)
    qtbot.waitUntil(lambda: len(fakes.searches) == 1 and view.search_btn.isEnabled())
    view.failed_table.selectRow(1)
    qtbot.waitUntil(lambda: len(fakes.searches) == 2 and view.search_btn.isEnabled())
    view.failed_table.selectRow(0)
    assert len(fakes.searches) == 2 and view.query.text() == "Chris Travis One"
    view.query.setText("Chris Travis One remix")
    view.search_btn.click()
    qtbot.waitUntil(lambda: len(fakes.searches) == 3 and view.search_btn.isEnabled())
    assert fakes.searches[-1][0] == "Chris Travis One remix"
    qtbot.waitUntil(lambda: _all_checked(view))
    assert sorted(fakes.checks) == sorted(CHECKS)           # each video checked once


def test_search_failure_and_empty_list(qtbot, ctx, failed):
    fakes = Fakes([failed()])
    fakes.search_error = RuntimeError("offline")
    view = _view(qtbot, ctx, fakes)
    qtbot.waitUntil(lambda: "search failed" in view.status.text())
    fakes.items = []
    view.refresh()
    qtbot.waitUntil(lambda: view.failed_model.rowCount() == 0)
    assert "No failed downloads" in view.status.text()
    assert not view.search_btn.isEnabled() and not view.download_btn.isEnabled()


def test_open_video_and_listen(qtbot, ctx, failed, monkeypatch):
    opened = []
    monkeypatch.setattr(av.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    view = _view(qtbot, ctx, Fakes([failed()]))
    qtbot.waitUntil(lambda: view.candidate_model.rowCount() == 3)
    heard = []
    view.listen_requested.connect(heard.append)
    view.open_btn.click()
    view.listen_btn.click()
    assert opened == [OK_URL] and heard == [failed().track.url]


def test_page_sits_under_listen_in_the_window(qtbot, ctx):
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    assert PAGE_ALTERNATIVES == PAGE_LISTEN + 1
    assert window.nav.item(PAGE_ALTERNATIVES).text() == "Alternatives"
    window.show_page(PAGE_ALTERNATIVES)
    assert window.stack.currentWidget() is window.alternatives_view
    qtbot.waitUntil(lambda: window.alternatives_view.refresh_btn.isEnabled())
    window.close()
