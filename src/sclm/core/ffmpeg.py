"""FFmpeg resolution shared by download/ (yt-dlp postprocessor) and metadata/ (if needed).

Lookup order (CLAUDE.md): settings path -> ``PATH`` -> imageio-ffmpeg bundled binary.
An invalid configured path is logged and skipped, not fatal.

yt-dlp note: pass ``str(info.path)`` as the ``ffmpeg_location`` option. yt-dlp accepts a file
path whose name contains "ffmpeg" (e.g. imageio's ``ffmpeg-linux-x86_64-v7.0.2``). The bundled
build ships no ffprobe; yt-dlp then falls back to parsing ``ffmpeg -i`` output, which is
enough for FFmpegExtractAudio.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sclm.core.errors import FFmpegNotFoundError

log = logging.getLogger(__name__)


class FfmpegSource(StrEnum):
    SETTINGS = "settings"
    PATH = "path"
    BUNDLED = "bundled"


@dataclass(frozen=True, slots=True)
class FfmpegInfo:
    path: Path
    source: FfmpegSource


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # RuntimeError when the binary is missing
        log.debug("imageio-ffmpeg has no usable binary: %s", exc)
        return None


def resolve_ffmpeg(
    configured: str | os.PathLike[str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    bundled: Callable[[], str | None] = _bundled_ffmpeg,
) -> FfmpegInfo:
    """Find an ffmpeg executable. Raises :class:`FFmpegNotFoundError` if none is usable.

    ``configured`` is ``Settings.ffmpeg_path`` ('' / None = auto). It may be a file or a
    directory containing ``ffmpeg``. ``which``/``bundled`` are injectable for tests.
    """
    if configured and str(configured).strip():
        candidate = Path(str(configured).strip()).expanduser()
        if candidate.is_dir():
            candidate = candidate / "ffmpeg"
        if _is_executable(candidate):
            return FfmpegInfo(candidate.absolute(), FfmpegSource.SETTINGS)
        log.warning("Configured ffmpeg path %s is not an executable file; trying PATH", candidate)

    found = which("ffmpeg")
    if found and _is_executable(Path(found)):
        return FfmpegInfo(Path(found).absolute(), FfmpegSource.PATH)

    bundled_path = bundled()
    if bundled_path and _is_executable(Path(bundled_path)):
        return FfmpegInfo(Path(bundled_path).absolute(), FfmpegSource.BUNDLED)

    raise FFmpegNotFoundError()


def ffmpeg_version(path: Path, *, timeout: float = 10.0) -> str | None:
    """First line of ``ffmpeg -version`` (e.g. 'ffmpeg version 7.0.2 ...'), or None on failure."""
    try:
        proc = subprocess.run([str(path), "-hide_banner", "-version"], capture_output=True,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg -version failed for %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    first = proc.stdout.strip().splitlines()
    return first[0] if first else None
