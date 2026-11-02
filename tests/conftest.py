"""Shared fixtures for all test packages.

Owned by Stage 1. Stage agents: don't edit this file; put package-specific fixtures in
``tests/<package>/conftest.py``.

* Qt runs offscreen.
* Every test gets an isolated ``HOME`` and ``SCLM_HOME`` under its tmp dir, so nothing can
  touch the real ~/.config, ~/Music, etc.
* ``@pytest.mark.network`` tests are skipped unless ``SCLM_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from sclm.core.errors import FFmpegNotFoundError  # noqa: E402
from sclm.core.ffmpeg import resolve_ffmpeg  # noqa: E402
from sclm.core.models import TrackInfo  # noqa: E402
from sclm.core.paths import AppPaths  # noqa: E402
from sclm.core.settings import Settings, SettingsStore  # noqa: E402
from sclm.core.storage import Storage  # noqa: E402


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SCLM_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="network test; set SCLM_NETWORK_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SCLM_HOME", str(tmp_path / "sclm-home"))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    """Isolated, created application directories."""
    return AppPaths.under(tmp_path / "sclm").ensure()


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    d = tmp_path / "library"
    d.mkdir()
    return d


@pytest.fixture
def settings(library_dir: Path) -> Settings:
    """Default settings pointing at the tmp library dir."""
    return Settings(library_dir=str(library_dir))


@pytest.fixture
def settings_store(app_paths: AppPaths, settings: Settings) -> SettingsStore:
    return SettingsStore(app_paths.settings_file, initial=settings)


@pytest.fixture
def storage(app_paths: AppPaths) -> Iterator[Storage]:
    db = Storage(app_paths.db_file)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def make_track() -> Callable[..., TrackInfo]:
    """Factory: ``make_track("123", title="X")`` -> full (non-partial) TrackInfo."""

    def factory(track_id: str = "1001", **overrides: object) -> TrackInfo:
        values: dict[str, object] = {
            "track_id": track_id,
            "url": f"https://soundcloud.com/test-artist/track-{track_id}",
            "title": f"Track {track_id}",
            "artist": "Test Artist",
            "duration_s": 180.0,
            "genre": "Deep House",
            "upload_date": "2024-05-17",
            "artwork_url": f"https://i1.sndcdn.com/artworks-{track_id}-t500x500.jpg",
            "uploader_id": "42",
        }
        values.update(overrides)
        return TrackInfo(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture(scope="session")
def ffmpeg_exe() -> Path:
    """Resolved ffmpeg (normally the bundled imageio-ffmpeg binary). Skips if unavailable."""
    try:
        return resolve_ffmpeg(None).path
    except FFmpegNotFoundError:
        pytest.skip("no ffmpeg available")


@pytest.fixture(scope="session")
def _tiny_mp3_master(tmp_path_factory: pytest.TempPathFactory, ffmpeg_exe: Path) -> Path:
    out = tmp_path_factory.mktemp("media") / "tiny.mp3"
    subprocess.run(
        [str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-ac", "2", "-ar", "44100",
         "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
        check=True, timeout=60)
    return out


@pytest.fixture
def tiny_mp3(tmp_path: Path, _tiny_mp3_master: Path) -> Path:
    """A fresh copy of a valid 1-second, untagged 128 kbps MP3 in a staging-like dir."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    dest = staging / "tiny.mp3"
    shutil.copyfile(_tiny_mp3_master, dest)
    return dest
