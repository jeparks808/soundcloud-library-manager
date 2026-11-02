"""Stage 5: FFmpeg failures through real yt-dlp (offline, ``file://`` source).

``settings.ffmpeg_path`` points at small shell scripts standing in for a broken ffmpeg. A
failed conversion must end the job FAILED (category ffmpeg) with ffmpeg's stderr tail in the
detail, without retrying, without anything reaching the library and with staging removed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from sclm.core.errors import ErrorCategory
from sclm.core.ffmpeg import resolve_ffmpeg
from sclm.core.models import DownloadRequest, JobStatus
from sclm.download.manager import DownloadManager

S = JobStatus


@pytest.fixture(scope="module")
def wav_master(tmp_path_factory: pytest.TempPathFactory, ffmpeg_exe: Path) -> Path:
    out = tmp_path_factory.mktemp("ffmpeg-fail-media") / "4242.wav"
    subprocess.run([str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-ac", "2", "-ar", "44100",
                    str(out)], check=True, timeout=60)
    return out


def fake_ffmpeg(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "fake-bin" / "ffmpeg"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    return exe


@pytest.fixture
def conversion_fails(tmp_path: Path, ffmpeg_exe: Path) -> Path:
    """Probing works (delegates to the real ffmpeg); the MP3 conversion exits 1 after 20
    lines of stderr, like a real encoder failure."""
    return fake_ffmpeg(tmp_path, f"""\
for a in "$@"; do case "$a" in *.mp3) convert=1;; esac; done
[ -z "$convert" ] && exec "{ffmpeg_exe}" "$@"
i=1
while [ $i -le 20 ]; do echo "encoder diagnostic line $i" >&2; i=$((i+1)); done
echo "Conversion failed!" >&2
exit 1
""")


@pytest.fixture
def broken_binary(tmp_path: Path) -> Path:
    """An ffmpeg that can't even start (e.g. a missing shared library)."""
    return fake_ffmpeg(tmp_path, """\
echo "ffmpeg: error while loading shared libraries: libavdevice.so.60" >&2
exit 127
""")


@pytest.fixture
def run_real(storage, settings_store, settings, app_paths, listener, fake_finalize,
             fake_find_duplicate, wav_master, make_track, wait_until):
    managers: list[DownloadManager] = []

    def run(ffmpeg_path: str, **kw):
        settings_store.update(settings.replace(ffmpeg_path=ffmpeg_path))
        m = DownloadManager(storage, settings_store, app_paths, listener,
                            finalize=fake_finalize, find_duplicate=fake_find_duplicate,
                            extra_ydl_opts={"enable_file_urls": True}, backoff_base_s=0.01,
                            **kw)
        managers.append(m)
        [job] = m.enqueue([DownloadRequest(make_track("4242", url=wav_master.as_uri()))])
        wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=60)
        return m.get_job(job.job_id)

    yield run
    for m in managers:
        m.shutdown(wait=True, timeout=30)


def assert_failed_cleanly(job, storage, app_paths, library_dir, fake_finalize, listener):
    assert job.status == S.FAILED, job
    assert job.error.category == ErrorCategory.FFMPEG
    assert job.attempts == 1                                   # never retried
    assert "waiting_retry" not in listener.statuses(job.job_id)
    assert fake_finalize.calls == []
    assert list(library_dir.rglob("*")) == []                  # nothing partial in the library
    assert not app_paths.job_staging_dir(job.job_id).exists()  # staging removed
    rec = storage.get_download(job.job_id)
    assert rec.status == S.FAILED and rec.error == job.error and rec.file_path is None


def test_conversion_failure_keeps_ffmpeg_stderr_tail(run_real, conversion_fails, storage,
                                                     app_paths, library_dir, fake_finalize,
                                                     listener, wav_master):
    job = run_real(str(conversion_fails))
    assert_failed_cleanly(job, storage, app_paths, library_dir, fake_finalize, listener)
    detail = job.error.detail
    assert "Conversion failed!" in detail
    # the last 15 stderr lines (ARCHITECTURE §7), not just yt-dlp's last line
    assert "encoder diagnostic line 20" in detail and "encoder diagnostic line 7" in detail
    assert "encoder diagnostic line 6\n" not in detail + "\n"
    assert str(conversion_fails) in detail                     # which ffmpeg failed
    assert wav_master.is_file()                                # the source is never touched


def test_broken_ffmpeg_binary_fails_without_retry(run_real, broken_binary, storage, app_paths,
                                                  library_dir, fake_finalize, listener):
    job = run_real(str(broken_binary))
    assert_failed_cleanly(job, storage, app_paths, library_dir, fake_finalize, listener)
    assert str(broken_binary) in job.error.detail
    assert "from settings" in job.error.detail


def test_missing_configured_ffmpeg_falls_back_by_design(run_real, tmp_path, caplog):
    """A configured path that doesn't exist is logged and skipped (ARCHITECTURE §11): the
    resolver falls back to PATH / the bundled binary and the download still works."""
    gone = tmp_path / "nowhere" / "ffmpeg"
    with caplog.at_level(logging.WARNING, logger="sclm.core.ffmpeg"):
        job = run_real(str(gone))
    assert job.status == S.COMPLETED, job.error
    assert any(str(gone) in r.getMessage() for r in caplog.records)


def test_no_ffmpeg_anywhere_fails_before_any_network_access(run_real, tmp_path, storage,
                                                            app_paths, library_dir,
                                                            fake_finalize, listener):
    def nothing_installed(configured):
        return resolve_ffmpeg(configured, which=lambda _name: None, bundled=lambda: None)

    job = run_real(str(tmp_path / "nowhere" / "ffmpeg"), ffmpeg_resolver=nothing_installed)
    assert_failed_cleanly(job, storage, app_paths, library_dir, fake_finalize, listener)
    assert "not found" in job.error.user_message.lower()
    assert listener.statuses(job.job_id) == ["queued", "resolving", "failed"]
