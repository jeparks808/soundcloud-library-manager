"""yt-dlp option builder and logger adapter (ARCHITECTURE.md §13).

One ``YoutubeDL`` instance is created per job or preview, inside the worker thread, from the
dicts built here. Options never enable anything that works around access restrictions.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sclm.core.errors import ConfigError, FileSystemError
from sclm.core.settings import Settings

ytdlp_log = logging.getLogger("sclm.ytdlp")

SOCKET_TIMEOUT_S = 20
HTTP_RETRIES = 3
COOKIES_COPY_NAME = ".cookies.txt"

ProgressHook = Callable[[dict[str, Any]], None]


class YtdlpLogger:
    """Adapter passed as yt-dlp's ``logger`` option.

    yt-dlp sends both debug and normal screen output to ``debug()`` (debug lines start with
    ``[debug] ``). Errors are re-raised to us as exceptions and logged by the caller with
    context, so here they only go to DEBUG to avoid duplicates.
    """

    def __init__(self, logger: logging.Logger = ytdlp_log) -> None:
        self._log = logger

    def debug(self, msg: str) -> None:
        self._log.debug("%s", msg.removeprefix("[debug] "))

    def info(self, msg: str) -> None:
        self._log.debug("%s", msg)

    def warning(self, msg: str) -> None:
        self._log.warning("%s", msg.removeprefix("WARNING: "))

    def error(self, msg: str) -> None:
        self._log.debug("%s", msg.removeprefix("ERROR: "))


def prepare_cookiefile(settings: Settings, workdir: Path) -> Path | None:
    """Copy the user's cookies file into ``workdir`` and return the copy (or None).

    yt-dlp writes its cookie jar back to ``cookiefile`` when it closes; pointing it at a
    private copy guarantees the user's own file is never modified. Raises ConfigError if a
    cookies file is configured but missing.
    """
    src = settings.cookies_path
    if src is None:
        return None
    if not src.is_file():
        raise ConfigError(f"The cookies file set in Settings was not found: {src}")
    dest = workdir / COOKIES_COPY_NAME
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(0o600)
    except OSError as exc:
        raise FileSystemError(f"Could not read the cookies file: {src}", detail=repr(exc)) from exc
    return dest


def common_opts(cache_dir: Path, cookiefile: Path | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": YtdlpLogger(),
        "socket_timeout": SOCKET_TIMEOUT_S,
        "retries": HTTP_RETRIES,
        "fragment_retries": HTTP_RETRIES,
        "extractor_retries": HTTP_RETRIES,
        "cachedir": str(cache_dir / "yt-dlp"),
        "color": "no_color",
    }
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    return opts


def preview_opts(cache_dir: Path, cookiefile: Path | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    opts = common_opts(cache_dir, cookiefile) | {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "format": "bestaudio/best",
    }
    if extra:
        opts.update(extra)
    return opts


def download_opts(
    settings: Settings,
    *,
    cache_dir: Path,
    staging_dir: Path,
    ffmpeg_path: Path,
    progress_hook: ProgressHook,
    postprocessor_hook: ProgressHook,
    cookiefile: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Options for one download job: bestaudio -> FFmpegExtractAudio(mp3, bitrate).

    ``extra`` is a test seam (e.g. ``{"enable_file_urls": True}`` for offline tests); the app
    never passes it.
    """
    opts = common_opts(cache_dir, cookiefile) | {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": {"default": str(staging_dir / "%(id)s.%(ext)s")},
        "ffmpeg_location": str(ffmpeg_path),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(settings.bitrate_kbps),
        }],
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "overwrites": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "updatetime": False,
    }
    if extra:
        opts.update(extra)
    return opts
