from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

from sclm.app import build_core
from sclm.core.logging_setup import setup_logging
from sclm.core.models import JobStatus
from sclm.core.paths import APP_DIR_NAME, AppPaths
from sclm.core.storage import Storage


# --- paths ---------------------------------------------------------------------------

def test_from_env_defaults(tmp_path):
    p = AppPaths.from_env({"HOME": str(tmp_path)})
    assert p.config_dir == tmp_path / ".config" / APP_DIR_NAME
    assert p.data_dir == tmp_path / ".local/share" / APP_DIR_NAME
    assert p.cache_dir == tmp_path / ".cache" / APP_DIR_NAME
    assert p.state_dir == tmp_path / ".local/state" / APP_DIR_NAME


def test_from_env_xdg_vars_and_relative_ignored(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "/xdg/config",
           "XDG_DATA_HOME": "relative/data", "XDG_CACHE_HOME": "/xdg/cache",
           "XDG_STATE_HOME": ""}
    p = AppPaths.from_env(env)
    assert p.config_dir == Path("/xdg/config") / APP_DIR_NAME
    assert p.data_dir == tmp_path / ".local/share" / APP_DIR_NAME
    assert p.cache_dir == Path("/xdg/cache") / APP_DIR_NAME
    assert p.state_dir == tmp_path / ".local/state" / APP_DIR_NAME


def test_sclm_home_overrides_everything(tmp_path):
    p = AppPaths.from_env({"SCLM_HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": "/x"})
    assert p == AppPaths.under(tmp_path / "h")
    assert p.settings_file == tmp_path / "h/config/settings.json"
    assert p.db_file == tmp_path / "h/data/library.sqlite3"
    assert p.log_file == tmp_path / "h/state/logs/sclm.log"
    assert p.job_staging_dir(7) == tmp_path / "h/cache/staging/job-7"


def test_default_env_is_isolated_by_conftest(tmp_path):
    assert AppPaths.from_env() == AppPaths.under(tmp_path / "sclm-home")


def test_ensure_creates_dirs(tmp_path):
    p = AppPaths.under(tmp_path / "x").ensure()
    for d in (p.config_dir, p.data_dir, p.cache_dir, p.state_dir, p.log_dir, p.staging_dir):
        assert d.is_dir()
    p.ensure()  # idempotent


# --- logging -------------------------------------------------------------------------

def test_setup_logging_writes_file_and_is_idempotent(tmp_path):
    root = logging.getLogger("sclm")
    try:
        setup_logging(tmp_path / "logs", console=False)
        log_file = setup_logging(tmp_path / "logs", level=logging.DEBUG, console=True)
        marked = [h for h in root.handlers if getattr(h, "_sclm_handler", False)]
        assert len(marked) == 2
        logging.getLogger("sclm.test").debug("hello from %s", threading.current_thread().name)
        for h in marked:
            h.flush()
        assert "hello from MainThread" in log_file.read_text()
    finally:
        for h in list(root.handlers):
            if getattr(h, "_sclm_handler", False):
                root.removeHandler(h)
                h.close()
        root.propagate = True
        root.setLevel(logging.NOTSET)


# --- app -----------------------------------------------------------------------------

def test_build_core_creates_everything_and_marks_interrupted(tmp_path, make_track):
    paths = AppPaths.under(tmp_path / "app")
    paths.ensure()
    with Storage(paths.db_file) as db:
        stale = db.create_download(make_track("1"))
        db.update_download(stale, status=JobStatus.DOWNLOADING)

    core = build_core(paths)
    try:
        assert paths.staging_dir.is_dir()
        assert core.settings.get().bitrate_kbps == 320
        assert core.storage.get_download(stale).status == JobStatus.FAILED
    finally:
        core.storage.close()


def test_python_m_sclm_version():
    out = subprocess.run([sys.executable, "-m", "sclm", "--version"], capture_output=True,
                         text=True, timeout=60, check=True)
    assert out.stdout.strip().startswith("sclm ")


def test_download_bridge_delivers_worker_events_on_gui_thread(qtbot, make_track):
    from sclm.core.events import DownloadListener
    from sclm.core.models import DownloadJob, ProgressEvent
    from sclm.gui.bridge import DownloadBridge

    bridge = DownloadBridge()
    assert isinstance(bridge, DownloadListener)
    received: list[tuple[object, str]] = []
    bridge.job_changed.connect(lambda job: received.append((job, threading.current_thread().name)))
    bridge.progress.connect(lambda ev: received.append((ev, threading.current_thread().name)))

    job = DownloadJob(job_id=1, track=make_track())
    event = ProgressEvent(job_id=1, status=JobStatus.DOWNLOADING, fraction=0.5)
    worker = threading.Thread(target=lambda: (bridge.on_job_changed(job),
                                              bridge.on_progress(event)), name="worker")
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: len(received) == 2, timeout=2000)
    assert received == [(job, "MainThread"), (event, "MainThread")]
