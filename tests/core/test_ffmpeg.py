from __future__ import annotations

import os
from pathlib import Path

import pytest

from sclm.core.errors import FFmpegNotFoundError
from sclm.core.ffmpeg import FfmpegSource, ffmpeg_version, resolve_ffmpeg


def _fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho 'ffmpeg version 9.9-fake'\n")
    path.chmod(0o755)
    return path


def _none() -> None:
    return None


def test_configured_path_wins(tmp_path):
    exe = _fake_exe(tmp_path / "custom" / "ffmpeg")
    info = resolve_ffmpeg(str(exe), which=lambda _: "/usr/bin/ffmpeg", bundled=_none)
    assert info.path == exe and info.source is FfmpegSource.SETTINGS


def test_configured_directory_is_accepted(tmp_path):
    exe = _fake_exe(tmp_path / "bin" / "ffmpeg")
    info = resolve_ffmpeg(tmp_path / "bin", which=lambda _: None, bundled=_none)
    assert info.path == exe and info.source is FfmpegSource.SETTINGS


def test_invalid_configured_path_falls_back_to_path(tmp_path, caplog):
    on_path = _fake_exe(tmp_path / "usr" / "ffmpeg")
    not_exec = tmp_path / "noexec"
    not_exec.write_text("")
    for bad in (str(tmp_path / "missing"), str(not_exec)):
        info = resolve_ffmpeg(bad, which=lambda _: str(on_path), bundled=_none)
        assert info.path == on_path and info.source is FfmpegSource.PATH
    assert "not an executable" in caplog.text


def test_blank_configured_path_means_auto(tmp_path):
    on_path = _fake_exe(tmp_path / "ffmpeg")
    info = resolve_ffmpeg("   ", which=lambda _: str(on_path), bundled=_none)
    assert info.source is FfmpegSource.PATH


def test_bundled_fallback(tmp_path):
    bundled = _fake_exe(tmp_path / "ffmpeg-linux-x86_64-v7.0.2")
    info = resolve_ffmpeg(None, which=lambda _: None, bundled=lambda: str(bundled))
    assert info.path == bundled and info.source is FfmpegSource.BUNDLED


def test_nothing_found_raises():
    with pytest.raises(FFmpegNotFoundError):
        resolve_ffmpeg("", which=lambda _: None, bundled=_none)


def test_ffmpeg_version_of_fake_and_missing(tmp_path):
    exe = _fake_exe(tmp_path / "ffmpeg")
    assert ffmpeg_version(exe) == "ffmpeg version 9.9-fake"
    assert ffmpeg_version(tmp_path / "missing") is None


def test_real_resolution_on_this_machine():
    """The venv ships imageio-ffmpeg, so resolution must succeed without system ffmpeg."""
    info = resolve_ffmpeg(None)
    assert info.path.is_file() and os.access(info.path, os.X_OK)
    version = ffmpeg_version(info.path)
    assert version and version.startswith("ffmpeg version")


def test_tiny_mp3_fixture(tiny_mp3):
    assert tiny_mp3.stat().st_size > 1000
    assert tiny_mp3.read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
