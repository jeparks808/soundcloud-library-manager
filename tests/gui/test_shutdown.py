"""Stage 5: Qt threading at shutdown, with a real DownloadManager and a fake yt-dlp.

* Worker events that arrive after the MainWindow was closed and destroyed must not crash.
* ``app.main`` stops the manager before anything is torn down, detaches the bridge, and
  closes the DB only when no worker is left (a stuck worker still records its final state).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from sclm.context import AppContext
from sclm.core.ffmpeg import FfmpegInfo, FfmpegSource
from sclm.core.models import DownloadJob, DownloadRequest, JobStatus, TrackInfo
from sclm.core.paths import AppPaths
from sclm.core.storage import Storage
from sclm.download.manager import DownloadManager
from sclm.gui.bridge import DownloadBridge
from sclm.gui.main_window import MainWindow

S = JobStatus
URL = "https://soundcloud.com/a/track-1"


class FakeYdl:
    """Minimal yt-dlp stand-in. ``download`` keeps reporting progress until the job is
    cancelled (the progress hook raises). ``stuck`` blocks the resolve with no hook at all,
    like a stalled network read that cancellation can't interrupt."""

    stuck: threading.Event | None = None
    downloading = threading.Event()

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    def __enter__(self) -> FakeYdl:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False, **_: Any) -> dict[str, Any]:
        if FakeYdl.stuck is not None:
            assert FakeYdl.stuck.wait(30), "test never released the stuck resolve"
        return {"id": "1", "title": "Song", "uploader": "Artist", "duration": 60.0,
                "webpage_url": url, "extractor_key": "Soundcloud",
                "formats": [{"format_id": "http_mp3", "url": "https://x/1.mp3",
                             "acodec": "mp3"}]}

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        while True:                      # until the hook raises JobCancelled
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
            FakeYdl.downloading.set()
            threading.Event().wait(0.01)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeYdl.stuck = None
    FakeYdl.downloading = threading.Event()


def fake_manager_kwargs() -> dict[str, Any]:
    return {"ydl_factory": FakeYdl, "finalize": lambda *a, **k: pytest.fail("no finalize"),
            "find_duplicate": lambda storage, track: None,
            "ffmpeg_resolver": lambda _p: FfmpegInfo(Path("/bin/true"), FfmpegSource.PATH),
            "measure_bitrate": lambda _p: None}


def test_worker_events_after_the_window_is_destroyed_do_not_crash(
        qtbot, app_paths, settings_store, storage, monkeypatch):
    bridge = DownloadBridge()
    manager = DownloadManager(storage, settings_store, app_paths, bridge,
                              **fake_manager_kwargs())
    try:
        win = MainWindow(AppContext(paths=app_paths, settings=settings_store, storage=storage,
                                    manager=manager, bridge=bridge))
        win.show()
        [job] = manager.enqueue([DownloadRequest(TrackInfo(track_id="1", url=URL))])
        assert FakeYdl.downloading.wait(10)
        qtbot.waitUntil(lambda: win.queue_view.model.job(job.job_id) is not None
                        and win.queue_view.model.job(job.job_id).status == S.DOWNLOADING,
                        timeout=5000)

        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Yes)
        assert win.close() is True                     # user confirms quitting mid-download
        destroyed = []
        win.destroyed.connect(lambda *_: destroyed.append(True))
        win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qtbot.waitUntil(lambda: bool(destroyed), timeout=5000)
        del win

        # The worker now cancels and emits CANCELLED from its thread into the bridge whose
        # receivers are gone; more events arrive from the GUI thread and another thread.
        assert manager.shutdown(wait=True, timeout=10) is True
        late = DownloadJob(job_id=job.job_id, track=job.track, status=S.CANCELLED)
        bridge.on_job_changed(late)
        t = threading.Thread(target=lambda: (bridge.on_job_changed(late),
                                             bridge.on_progress(None)))  # type: ignore[arg-type]
        t.start()
        t.join(5)
        for _ in range(5):
            QApplication.processEvents()
        assert storage.get_download(job.job_id).status == S.CANCELLED
    finally:
        manager.shutdown(wait=True, timeout=10)


# --- app.main -------------------------------------------------------------------------------

@pytest.fixture
def run_main(monkeypatch, tmp_path, qapp):
    """Run ``sclm.app.main`` with fakes: a recording Storage, a DownloadManager on FakeYdl,
    and an event loop that enqueues one download, waits until it is running and quits."""
    import sclm.app as app_module
    import sclm.download.manager as manager_module

    events: list[str] = []
    made: dict[str, Any] = {}

    class RecordingStorage(Storage):
        def __init__(self, path: Any) -> None:
            super().__init__(path)
            made["storage_path"] = path

        def close(self) -> None:
            events.append("storage.close")
            super().close()

    class TestManager(DownloadManager):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.update(fake_manager_kwargs())
            super().__init__(*args, **kwargs)
            made["manager"] = self

        def shutdown(self, **kwargs: Any) -> bool:
            result = super().shutdown(**kwargs)
            events.append(f"manager.shutdown -> {result}")
            return result

        def set_listener(self, listener: Any) -> None:
            if listener is None:
                events.append("listener detached")
            super().set_listener(listener)

    def fake_exec(self: QApplication) -> int:
        manager = made["manager"]
        [win] = [w for w in QApplication.topLevelWidgets()
                 if isinstance(w, MainWindow) and w._ctx.manager is manager]
        assert win.isVisible()
        [job] = manager.enqueue([DownloadRequest(TrackInfo(track_id="1", url=URL))])
        made["job_id"] = job.job_id
        if FakeYdl.stuck is None:
            assert FakeYdl.downloading.wait(10)
        else:
            qtbot_wait(lambda: manager.get_job(job.job_id).status == S.RESOLVING)
        return 0

    def qtbot_wait(pred: Any, timeout: float = 10.0) -> None:
        import time
        deadline = time.monotonic() + timeout
        while not pred():
            assert time.monotonic() < deadline
            QApplication.processEvents()
            time.sleep(0.01)

    monkeypatch.setattr(app_module, "Storage", RecordingStorage)
    monkeypatch.setattr(app_module, "setup_logging", lambda *a, **k: tmp_path / "sclm.log")
    monkeypatch.setattr(app_module, "install_excepthook", lambda: None)
    monkeypatch.setattr(manager_module, "DownloadManager", TestManager)
    monkeypatch.setattr(QApplication, "exec", fake_exec)
    saved = (qapp.applicationName(), qapp.applicationDisplayName(),
             qapp.organizationName(), qapp.desktopFileName())

    def run() -> int:
        return app_module.main(["sclm"])

    yield run, events, made
    qapp.setApplicationName(saved[0])
    qapp.setApplicationDisplayName(saved[1])
    qapp.setOrganizationName(saved[2])
    qapp.setDesktopFileName(saved[3])


def test_app_main_stops_downloads_before_closing_db_and_window(run_main):
    run, events, made = run_main
    assert run() == 0
    assert events == ["manager.shutdown -> True", "listener detached", "storage.close"]
    with Storage(made["storage_path"]) as db:
        assert db.get_download(made["job_id"]).status == S.CANCELLED   # final state recorded
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_app_main_keeps_db_open_for_a_worker_that_is_still_stopping(run_main, monkeypatch,
                                                                    caplog):
    """A worker stuck in a resolve outlives the shutdown timeout. The DB must stay open so
    its final CANCELLED is recorded when it finally returns, and no late event may reach the
    (now gone) Qt bridge."""
    import sclm.app as app_module

    run, events, made = run_main
    FakeYdl.stuck = threading.Event()
    monkeypatch.setattr(app_module, "SHUTDOWN_TIMEOUT_S", 0.2)
    with caplog.at_level(logging.WARNING, logger="sclm.app"):
        assert run() == 0
    assert events == ["manager.shutdown -> False", "listener detached"]   # DB left open
    assert any("still stopping" in r.getMessage() for r in caplog.records)
    manager: DownloadManager = made["manager"]
    FakeYdl.stuck.set()                                # the stalled read finally returns
    assert manager.shutdown(wait=True, timeout=10) is True
    assert manager.get_job(made["job_id"]).status == S.CANCELLED
    storage = manager._storage
    try:
        assert storage.get_download(made["job_id"]).status == S.CANCELLED
    finally:
        storage.close()


# --- SIGTERM / SIGINT -----------------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_signal_runs_the_normal_shutdown(tmp_path, sig):
    """``kill`` / logout (SIGTERM) or Ctrl+C (SIGINT) must quit through the event loop so
    app.main's shutdown runs: exit code 0, "Shutting down" logged, staging cleaned."""
    home, sclm_home = tmp_path / "child-home", tmp_path / "child-sclm-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env.update(HOME=str(home), SCLM_HOME=str(sclm_home), QT_QPA_PLATFORM="offscreen")
    paths = AppPaths.from_env(env)
    proc = subprocess.Popen([sys.executable, "-m", "sclm"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while not (paths.log_file.is_file() and "Ready" in paths.log_file.read_text()):
            assert proc.poll() is None, proc.stdout.read().decode()
            assert time.monotonic() < deadline, "app never became ready"
            time.sleep(0.05)
        leftover = paths.job_staging_dir(7)              # e.g. a job's half-written files
        leftover.mkdir(parents=True)
        (leftover / "7.m4a.part").write_bytes(b"x")
        proc.send_signal(sig)
        out = proc.communicate(timeout=30)[0].decode()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    log_text = paths.log_file.read_text()
    assert proc.returncode == 0, out
    assert f"Received {sig.name}" in log_text
    assert "Shutting down" in log_text
    assert not leftover.exists()                          # manager.shutdown() ran
    assert "Traceback" not in out
