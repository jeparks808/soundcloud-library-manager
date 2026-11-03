"""Fixtures for the download package: realistic SoundCloud info dicts, a fake YoutubeDL that
drives the real progress / postprocessor hooks, fake metadata functions, and a manager
factory wired to them.

Helpers are exposed as fixtures (pytest runs with ``--import-mode=importlib``, so test
modules can't import from this conftest).
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sclm.core.events import RecordingListener
from sclm.core.models import (
    DownloadRequest,
    DuplicateMatch,
    FinalizeOutcome,
    FinalizeResult,
    JobStatus,
    TrackInfo,
)
from sclm.core.paths import AppPaths
from sclm.core.settings import Settings, SettingsStore
from sclm.core.storage import Storage
from sclm.download.manager import DownloadManager

# --- realistic SoundCloud info dicts ---------------------------------------------------

_ART = "https://i1.sndcdn.com/artworks-000030896212-o16m9v"


def sc_thumbnails(base: str = _ART) -> list[dict[str, Any]]:
    sizes = {"mini": 16, "tiny": 20, "small": 32, "badge": 47, "t67x67": 67, "large": 100,
             "t300x300": 300, "crop": 400, "t500x500": 500, "original": None}
    thumbs = []
    for image_id, size in sizes.items():
        t: dict[str, Any] = {"id": image_id, "url": f"{base}-{image_id}.jpg"}
        if size:
            t.update(width=size, height=size)
        else:
            t["preference"] = 10
        thumbs.append(t)
    return thumbs


def sc_track_info(track_id: str = "1001", **overrides: Any) -> dict[str, Any]:
    """Shape of ``YoutubeDL.extract_info(track_url, download=False)`` for a SoundCloud track."""
    info: dict[str, Any] = {
        "id": track_id,
        "uploader": "Test Artist",
        "uploader_id": "42",
        "uploader_url": "https://soundcloud.com/test-artist",
        "timestamp": 1715904000,
        "title": f"Track {track_id}",
        "track": f"Track {track_id}",
        "description": "Free download. Follow me!",
        "duration": 180.5,
        "webpage_url": f"https://soundcloud.com/test-artist/track-{track_id}",
        "original_url": f"https://soundcloud.com/test-artist/track-{track_id}",
        "license": "all-rights-reserved",
        "view_count": 1234,
        "genres": ["Deep House"],
        "tags": ["house"],
        "thumbnails": sc_thumbnails(),
        "thumbnail": f"{_ART}-original.jpg",
        "upload_date": "20240517",
        "extractor": "soundcloud",
        "extractor_key": "Soundcloud",
        "formats": [
            {"format_id": "hls_aac_160k", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none",
             "abr": 160, "protocol": "m3u8_native",
             "url": f"https://playback.media-streaming.soundcloud.cloud/{track_id}/aac_160k/x/playlist.m3u8"},
            {"format_id": "http_mp3_1_0", "ext": "mp3", "acodec": "mp3", "vcodec": "none",
             "abr": 128, "protocol": "http",
             "url": f"https://cf-media.sndcdn.com/{track_id}.128.mp3?Policy=x"},
        ],
    }
    info.update(overrides)
    return info


def sc_preview_only_info(track_id: str = "2001") -> dict[str, Any]:
    """A Go+ track as seen without a subscription: only 30 s snippet formats."""
    return sc_track_info(track_id, formats=[
        {"format_id": "hls_mp3_1_0_preview", "ext": "mp3", "vcodec": "none", "abr": 128,
         "preference": -10, "protocol": "m3u8_native",
         "url": "https://cf-preview-hls-media.sndcdn.com/playlist/0/30/abc.128.mp3/playlist.m3u8"},
        {"format_id": "http_mp3_1_0_preview", "ext": "mp3", "vcodec": "none", "abr": 128,
         "preference": -10, "protocol": "http",
         "url": "https://cf-preview-media.sndcdn.com/preview/0/30/abc.128.mp3"},
    ])


def sc_flat_entry(track_id: str, *, title: str | None = None, permalink: bool = True,
                  album: str | None = None, album_type: str = "playlist") -> dict[str, Any]:
    """One entry of a flat (``extract_flat="in_playlist"``) set extraction."""
    url = (f"https://soundcloud.com/test-artist/track-{track_id}" if permalink
           else f"https://api-v2.soundcloud.com/tracks/{track_id}")
    entry: dict[str, Any] = {"_type": "url", "ie_key": "Soundcloud", "id": track_id, "url": url,
                             "webpage_url": url, "extractor_key": "Soundcloud"}
    if title is not None:
        entry["title"] = title
    if album is not None:
        entry.update(album=album, album_artist="Test Artist", album_type=album_type)
    return entry


def sc_playlist_info(entries: list[dict[str, Any]], *, playlist_id: str = "2284613",
                     title: str = "Summer Mix",
                     url: str = "https://soundcloud.com/test-artist/sets/summer-mix",
                     extractor_key: str = "SoundcloudSet") -> dict[str, Any]:
    return {"_type": "playlist", "id": playlist_id, "title": title, "uploader": "Test Artist",
            "uploader_id": "42", "webpage_url": url, "original_url": url,
            "extractor": "soundcloud:set", "extractor_key": extractor_key, "entries": entries}


@pytest.fixture
def sc() -> SimpleNamespace:
    """Builders for SoundCloud-shaped yt-dlp info dicts."""
    return SimpleNamespace(track=sc_track_info, preview_only=sc_preview_only_info,
                           flat_entry=sc_flat_entry, playlist=sc_playlist_info,
                           thumbnails=sc_thumbnails)


# --- fake YoutubeDL ----------------------------------------------------------------------

@dataclass
class FakeScript:
    """What the fake YoutubeDL does for one URL."""

    info: dict[str, Any] | None = None
    extract_errors: list[BaseException] = field(default_factory=list)  # one per call, in order
    download_errors: list[BaseException] = field(default_factory=list)
    progress_steps: int = 3
    total_bytes: int = 3000
    gate: threading.Event | None = None     # if given, keep "downloading" until it's set
    hold: threading.Event | None = None     # block (no hooks) in extract until set
    wrap_hook_errors: bool = False          # wrap hook exceptions like yt-dlp may do
    payload: bytes = b"ID3fake-mp3-payload"
    produce_mp3: bool = True


class FakeYoutubeDL:
    def __init__(self, factory: FakeYdlFactory, opts: dict[str, Any]) -> None:
        self.factory = factory
        self.params = opts

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _script(self, url: str) -> FakeScript:
        try:
            return self.factory.scripts[url]
        except KeyError:
            raise AssertionError(f"no fake script for {url}") from None

    def extract_info(self, url: str, download: bool = False, **_: Any) -> dict[str, Any]:
        assert download is False, "manager must resolve first (download=False)"
        with self.factory.lock:
            self.factory.extract_calls.append(url)
        script = self._script(url)
        if script.hold is not None:
            script.hold.wait(10)
        if script.extract_errors:
            raise script.extract_errors.pop(0)
        assert script.info is not None
        return dict(script.info)

    def _call_hook(self, hook: Callable[[dict[str, Any]], None], d: dict[str, Any],
                   script: FakeScript) -> None:
        if not script.wrap_hook_errors:
            hook(d)
            return
        try:
            hook(d)
        except Exception as exc:
            from yt_dlp.utils import DownloadError

            raise DownloadError(f"ERROR: {exc}", (type(exc), exc, None)) from exc

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        assert download is True
        f = self.factory
        url = info.get("webpage_url") or info.get("original_url")
        script = self._script(url)
        staging = Path(self.params["outtmpl"]["default"]).parent
        with f.lock:
            f.active += 1
            f.max_active = max(f.max_active, f.active)
            f.download_calls.append(url)
            f.opts.append(self.params)
        try:
            hooks = self.params["progress_hooks"]
            total = script.total_bytes
            part = staging / f"{info['id']}.m4a.part"
            part.write_bytes(b"partial")
            for i in range(1, script.progress_steps + 1):
                for hook in hooks:
                    self._call_hook(hook, {"status": "downloading", "downloaded_bytes":
                                           total * i // (script.progress_steps + 1),
                                           "total_bytes": total, "speed": 1000.0, "eta": 1},
                                    script)
            if script.gate is not None:
                while not script.gate.is_set():
                    for hook in hooks:
                        self._call_hook(hook, {"status": "downloading",
                                               "downloaded_bytes": total // 2,
                                               "total_bytes": total}, script)
                    time.sleep(0.01)
            if script.download_errors:
                raise script.download_errors.pop(0)
            part.unlink()
            src = staging / f"{info['id']}.m4a"
            src.write_bytes(b"m4a")
            for hook in hooks:
                self._call_hook(hook, {"status": "finished", "downloaded_bytes": total,
                                       "total_bytes": total, "filename": str(src)}, script)
            for hook in self.params.get("postprocessor_hooks", []):
                hook({"status": "started", "postprocessor": "ExtractAudio", "info_dict": info})
            result = dict(info)
            if script.produce_mp3:
                mp3 = staging / f"{info['id']}.mp3"
                mp3.write_bytes(script.payload)
                src.unlink()
                result["requested_downloads"] = [{"filepath": str(mp3), "ext": "mp3"}]
            for hook in self.params.get("postprocessor_hooks", []):
                hook({"status": "finished", "postprocessor": "ExtractAudio", "info_dict": info})
            return result
        finally:
            with f.lock:
                f.active -= 1


class FakeYdlFactory:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scripts: dict[str, FakeScript] = {}
        self.extract_calls: list[str] = []
        self.download_calls: list[str] = []
        self.opts: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    def __call__(self, opts: dict[str, Any]) -> FakeYoutubeDL:
        return FakeYoutubeDL(self, opts)

    def add(self, track: TrackInfo, info: dict[str, Any] | None = None,
            **script_kwargs: Any) -> FakeScript:
        """Script for ``track.url``; ``info`` defaults to a realistic SoundCloud dict."""
        if info is None:
            info = sc_track_info(track.track_id, webpage_url=track.url, original_url=track.url,
                                 title=track.title or f"Track {track.track_id}")
        script = FakeScript(info=info, **script_kwargs)
        self.scripts[track.url] = script
        return script


@pytest.fixture
def fake_ydl() -> FakeYdlFactory:
    return FakeYdlFactory()


@pytest.fixture
def fake_script_cls() -> type[FakeScript]:
    return FakeScript


# --- fake metadata functions --------------------------------------------------------------

class FakeFinalize:
    """Stands in for ``metadata.pipeline.finalize_track``: moves the staged file into the
    library (or returns DUPLICATE / raises, as configured)."""

    def __init__(self, library: Path) -> None:
        self.library = library
        self.calls: list[dict[str, Any]] = []
        self.outcome = FinalizeOutcome.PLACED
        self.duplicate: DuplicateMatch | None = None
        self.error: BaseException | None = None
        self.warnings: tuple[str, ...] = ()
        self.inspect: Callable[[Path], None] | None = None
        self.lock = threading.Lock()

    def __call__(self, staged_file: Path, track: TrackInfo, *, playlist_title: str | None,
                 settings: Settings, storage: Storage, force: bool = False,
                 on_stage: Callable[[JobStatus], None] | None = None,
                 cancel: threading.Event | None = None) -> FinalizeResult:
        with self.lock:
            self.calls.append({"staged": staged_file, "track": track, "force": force,
                               "playlist_title": playlist_title, "settings": settings,
                               "exists": staged_file.is_file()})
        assert staged_file.is_file()
        if self.inspect is not None:
            self.inspect(staged_file)
        if on_stage:
            on_stage(JobStatus.TAGGING)
        if self.error is not None:
            raise self.error
        if self.outcome == FinalizeOutcome.DUPLICATE:
            return FinalizeResult(FinalizeOutcome.DUPLICATE, duplicate=self.duplicate)
        if on_stage:
            on_stage(JobStatus.ORGANIZING)
        dest = self.library / f"{track.artist} - {track.title} [{track.track_id}].mp3"
        shutil.copyfile(staged_file, dest)
        staged_file.unlink()
        return FinalizeResult(FinalizeOutcome.PLACED, final_path=dest,
                              audio_sha256="ab" * 32, warnings=self.warnings)


class FakeFindDuplicate:
    def __init__(self) -> None:
        self.matches: dict[str, DuplicateMatch] = {}
        self.calls: list[str] = []

    def __call__(self, storage: Storage, track: TrackInfo) -> DuplicateMatch | None:
        self.calls.append(track.track_id)
        return self.matches.get(track.track_id)


@pytest.fixture
def fake_finalize(library_dir: Path) -> FakeFinalize:
    return FakeFinalize(library_dir)


@pytest.fixture
def fake_find_duplicate() -> FakeFindDuplicate:
    return FakeFindDuplicate()


# --- manager factory ------------------------------------------------------------------------

@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def make_manager(storage: Storage, settings_store: SettingsStore, app_paths: AppPaths,
                 listener: RecordingListener, fake_ydl: FakeYdlFactory,
                 fake_finalize: FakeFinalize, fake_find_duplicate: FakeFindDuplicate,
                 ) -> Iterator[Callable[..., DownloadManager]]:
    managers: list[DownloadManager] = []

    def factory(**overrides: Any) -> DownloadManager:
        kwargs: dict[str, Any] = {
            "listener": listener, "finalize": fake_finalize,
            "find_duplicate": fake_find_duplicate, "ydl_factory": fake_ydl,
            "backoff_base_s": 0.05,
            # fake staged files are not real MP3s; report the configured bitrate
            "measure_bitrate": lambda _p: settings_store.get().bitrate_kbps,
        }
        kwargs.update(overrides)
        manager = DownloadManager(storage, settings_store, app_paths, **kwargs)
        managers.append(manager)
        return manager

    yield factory
    for m in managers:
        m.shutdown(wait=True, timeout=10)


@pytest.fixture
def wait_until() -> Callable[..., None]:
    def wait(predicate: Callable[[], bool], timeout: float = 5.0, message: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting: {message or predicate}")

    return wait


def terminal(manager, job_id) -> bool:
    job = manager.get_job(job_id)
    return job is not None and job.status.is_terminal


@pytest.fixture
def run_one(make_manager, fake_ydl, wait_until):
    """Enqueue one track and wait until it is terminal. Returns (manager, job)."""
    def run(track, *, manager=None, request_kwargs=None, timeout=5.0, **script):
        if track.url not in fake_ydl.scripts:
            fake_ydl.add(track, **script)
        m = manager or make_manager()
        [job] = m.enqueue([DownloadRequest(track, **(request_kwargs or {}))])
        wait_until(lambda: terminal(m, job.job_id), timeout=timeout)
        return m, m.get_job(job.job_id)
    return run
