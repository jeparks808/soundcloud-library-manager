"""Fixtures for metadata tests. No network: the default artwork fetcher is replaced."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

# Minimal byte strings with valid JPEG/PNG magic (mutagen does not decode images).
JPEG_BYTES = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
              + b"\x00" * 64 + b"\xff\xd9")
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _no_network_artwork(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any finalize_track call that doesn't inject ``artwork_fetcher`` gets a fake that
    records the URL and returns None, so the suite never touches the network."""
    calls: list[str] = []

    def fake(url: str) -> bytes | None:
        calls.append(url)
        return None

    monkeypatch.setattr("sclm.metadata.pipeline.fetch_artwork", fake)
    return calls


@pytest.fixture
def jpeg_bytes() -> bytes:
    return JPEG_BYTES


@pytest.fixture
def png_bytes() -> bytes:
    return PNG_BYTES


@pytest.fixture(scope="session")
def _other_mp3_master(tmp_path_factory: pytest.TempPathFactory, ffmpeg_exe: Path) -> Path:
    """A second valid MP3 with different audio (880 Hz, 2 s) -> different audio hash."""
    out = tmp_path_factory.mktemp("media2") / "other.mp3"
    subprocess.run(
        [str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=880:duration=2", "-ac", "2", "-ar", "44100",
         "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
        check=True, timeout=60)
    return out


@pytest.fixture
def mp3_copy(_tiny_mp3_master: Path, _other_mp3_master: Path) -> Callable[..., Path]:
    """``mp3_copy(dest, other=False)`` copies a valid untagged MP3 to ``dest``."""

    def factory(dest: Path, *, other: bool = False) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_other_mp3_master if other else _tiny_mp3_master, dest)
        return dest

    return factory
