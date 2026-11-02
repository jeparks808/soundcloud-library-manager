"""yt-dlp option builder and logger adapter (ARCHITECTURE.md §13).

One ``YoutubeDL`` instance is created per job or preview, inside the worker thread, from the
dicts built here. Options never enable anything that works around access restrictions.
"""

from __future__ import annotations

import functools
import logging
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sclm.core.errors import ConfigError, FileSystemError, tail_lines
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


_EXTRACT_AUDIO_KEY = "FFmpegExtractAudio"
_FFMPEG_CMDLINE_PREFIX = "ffmpeg command line:"


@functools.cache
def _extract_audio_pp_class() -> type:
    """``FFmpegExtractAudioPP`` that keeps the tail of ffmpeg's stderr when it fails.

    yt-dlp puts only the last stderr line into the error (often just "Conversion failed!");
    ARCHITECTURE.md §7 wants the last ``FFMPEG_STDERR_TAIL_LINES`` lines in the job detail.
    yt-dlp passes the full stderr to ``write_debug`` right before raising, so we keep the
    last debug text and append its tail to the error. If a future yt-dlp stops doing that,
    the error simply stays as yt-dlp made it.
    """
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP, FFmpegPostProcessorError

    class SclmExtractAudioPP(FFmpegExtractAudioPP):  # type: ignore[misc]
        _sclm_debug = ""

        @classmethod
        def pp_key(cls) -> str:     # same name in hooks/logs/postprocessor_args: "ExtractAudio"
            return str(FFmpegExtractAudioPP.pp_key())

        def write_debug(self, text: Any, *args: Any, **kwargs: Any) -> Any:
            self._sclm_debug = text if isinstance(text, str) else ""
            return super().write_debug(text, *args, **kwargs)

        def real_run_ffmpeg(self, *args: Any, **kwargs: Any) -> Any:
            self._sclm_debug = ""
            try:
                return super().real_run_ffmpeg(*args, **kwargs)
            except FFmpegPostProcessorError as err:
                stderr = self._sclm_debug
                tail = "" if stderr.startswith(_FFMPEG_CMDLINE_PREFIX) else tail_lines(stderr)
                if not tail or tail == (err.msg or "").strip():
                    raise
                raise FFmpegPostProcessorError(
                    f"{err.msg}\nffmpeg stderr (last lines):\n{tail}") from err

    return SclmExtractAudioPP


def build_download_ydl(opts: Mapping[str, Any]) -> Any:
    """``yt_dlp.YoutubeDL`` for :func:`download_opts`, with FFmpegExtractAudio replaced by
    the stderr-keeping subclass (other postprocessors are added as yt-dlp would)."""
    import yt_dlp
    from yt_dlp.postprocessor import get_postprocessor

    params = dict(opts)
    pp_defs = params.pop("postprocessors", [])
    ydl = yt_dlp.YoutubeDL(params)  # type: ignore[arg-type]
    for raw in pp_defs:
        pp_def = dict(raw)
        when = pp_def.pop("when", "post_process")
        key = pp_def.pop("key")
        cls = _extract_audio_pp_class() if key == _EXTRACT_AUDIO_KEY else get_postprocessor(key)
        ydl.add_post_processor(cls(ydl, **pp_def), when=when)
    return ydl


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
        "postprocessors": [{       # built by build_download_ydl
            "key": _EXTRACT_AUDIO_KEY,
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
