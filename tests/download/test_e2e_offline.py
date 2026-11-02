"""Real yt-dlp + bundled ffmpeg, fully offline: a local WAV is "downloaded" through a
``file://`` URL (``enable_file_urls`` is a test-only option) and converted to MP3 by the
FFmpegExtractAudio postprocessor at the configured bitrate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mutagen.mp3 import MP3

from sclm.core.models import DownloadRequest, JobStatus
from sclm.download.manager import DownloadManager

S = JobStatus


@pytest.fixture(scope="module")
def wav_master(tmp_path_factory: pytest.TempPathFactory, ffmpeg_exe: Path) -> Path:
    out = tmp_path_factory.mktemp("e2e-media") / "4242.wav"
    subprocess.run([str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=3", "-ac", "2", "-ar", "44100",
                    str(out)], check=True, timeout=60)
    return out


@pytest.fixture
def real_manager(storage, settings_store, app_paths, listener, fake_finalize,
                 fake_find_duplicate):
    managers: list[DownloadManager] = []

    def make(**kw):
        m = DownloadManager(storage, settings_store, app_paths, listener,
                            finalize=fake_finalize, find_duplicate=fake_find_duplicate,
                            extra_ydl_opts={"enable_file_urls": True}, **kw)
        managers.append(m)
        return m

    yield make
    for m in managers:
        m.shutdown(wait=True, timeout=30)


@pytest.mark.parametrize("bitrate", [128, 192])
def test_real_ytdlp_wav_to_mp3_at_bitrate(real_manager, wav_master, make_track, fake_finalize,
                                         settings_store, settings, listener, storage,
                                         app_paths, wait_until, bitrate):
    settings_store.update(settings.replace(bitrate_kbps=bitrate))
    seen: dict[str, object] = {}

    def inspect(staged: Path) -> None:
        audio = MP3(staged)
        seen.update(bitrate=audio.info.bitrate, length=audio.info.length,
                    name=staged.name, parent=staged.parent, siblings=sorted(
                        p.name for p in staged.parent.iterdir()))

    fake_finalize.inspect = inspect
    track = make_track("4242", url=wav_master.as_uri(), title="Sine", artist="Test Tone")
    m = real_manager()
    [job] = m.enqueue([DownloadRequest(track)])
    wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=60)
    final = m.get_job(job.job_id)
    assert final.status == S.COMPLETED, final.error

    # the staged file was a real MP3 at the configured constant bitrate
    assert seen["name"] == "4242.mp3"
    assert seen["parent"] == app_paths.job_staging_dir(job.job_id)
    assert round(seen["bitrate"] / 1000) == bitrate
    assert seen["length"] == pytest.approx(3.0, abs=0.15)
    assert "4242.wav" not in seen["siblings"]      # yt-dlp removed the intermediate file

    statuses = listener.statuses(job.job_id)
    for s in ("resolving", "downloading", "converting", "tagging", "organizing", "completed"):
        assert s in statuses
    prog = [p for p in listener.progress if p.job_id == job.job_id]
    assert any(p.status == S.DOWNLOADING and p.fraction == 1.0 for p in prog)
    assert any(p.status == S.CONVERTING for p in prog)

    # resolved values win (generic extractor titles the file "4242"); what the resolver
    # doesn't know (artist) is kept from the queued track
    assert final.track.track_id == "4242" and final.track.title == "4242"
    assert final.track.artist == "Test Tone"
    assert round(MP3(final.final_path).info.bitrate / 1000) == bitrate
    assert storage.get_download(job.job_id).bitrate_kbps == bitrate
    assert not app_paths.job_staging_dir(job.job_id).exists()


def test_real_ytdlp_mp3_source(real_manager, tiny_mp3, make_track, fake_finalize,
                               settings_store, settings, wait_until, storage):
    """An MP3 source is already in the target codec; yt-dlp keeps it as a valid MP3."""
    settings_store.update(settings.replace(bitrate_kbps=320))
    rates: list[int] = []
    fake_finalize.inspect = lambda p: rates.append(MP3(p).info.bitrate)
    src = tiny_mp3.with_name("777.mp3")
    tiny_mp3.rename(src)
    m = real_manager()
    [job] = m.enqueue([DownloadRequest(make_track("777", url=src.as_uri()))])
    wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=60)
    assert m.get_job(job.job_id).status == S.COMPLETED, m.get_job(job.job_id).error
    assert rates and round(rates[0] / 1000) in (128, 320)
    # the source MP3 is kept as-is (not re-encoded to 320); the recorded bitrate is the real one
    assert round(rates[0] / 1000) == 128
    assert storage.get_download(job.job_id).bitrate_kbps == 128
    assert m.get_job(job.job_id).actual_bitrate_kbps == 128
    assert src.is_file()                      # the source is never touched


def test_real_ytdlp_cancel_from_progress_hook(real_manager, wav_master, make_track, listener,
                                              fake_finalize, storage, app_paths, wait_until):
    """Cancelling on the first progress event aborts inside real yt-dlp (hook raises
    JobCancelled; yt-dlp may wrap it) and the job ends CANCELLED with staging removed."""
    m = real_manager()

    class CancelOnProgress:
        def on_job_changed(self, job):
            listener.on_job_changed(job)

        def on_progress(self, event):
            listener.on_progress(event)
            m.cancel(event.job_id)

    m.set_listener(CancelOnProgress())
    [job] = m.enqueue([DownloadRequest(make_track("4242", url=wav_master.as_uri()))])
    wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=60)
    assert m.get_job(job.job_id).status == S.CANCELLED
    assert storage.get_download(job.job_id).status == S.CANCELLED
    assert fake_finalize.calls == []
    assert not app_paths.job_staging_dir(job.job_id).exists()
    assert wav_master.is_file()


def test_file_urls_disabled_without_test_option(storage, settings_store, app_paths, listener,
                                                fake_finalize, fake_find_duplicate,
                                                wav_master, make_track, wait_until):
    """Production options never enable file:// access: the same job fails."""
    m = DownloadManager(storage, settings_store, app_paths, listener, finalize=fake_finalize,
                        find_duplicate=fake_find_duplicate)
    try:
        [job] = m.enqueue([DownloadRequest(make_track("4242", url=wav_master.as_uri()))])
        wait_until(lambda: m.get_job(job.job_id).status.is_terminal, timeout=60)
        assert m.get_job(job.job_id).status == S.FAILED
    finally:
        m.shutdown(wait=True, timeout=30)
