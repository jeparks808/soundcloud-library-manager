"""Exception hierarchy and the transportable :class:`ErrorInfo`.

Every failure that reaches the user is an :class:`SclmError` subclass (or is converted
into an :class:`ErrorInfo` by :func:`error_info_from_exception`). Exceptions stay inside
the thread that raised them; what crosses threads (events, DB rows) is ``ErrorInfo``.

Retry policy is attached to the category: only ``NETWORK`` is retried automatically.
Access restrictions (unavailable, geo-blocked, private, auth required) are reported and
never retried or worked around.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

FFMPEG_STDERR_TAIL_LINES = 15
NETWORK_MAX_ATTEMPTS = 3          # total attempts per job for NETWORK errors (1 + 2 retries)
NETWORK_BACKOFF_BASE_S = 2.0      # delay before retry n (n>=1) = BASE * 2**(n-1) -> 2 s, 4 s


class ErrorCategory(StrEnum):
    UNAVAILABLE = "unavailable"        # removed, not found, not streamable
    GEO_BLOCKED = "geo_blocked"
    PRIVATE = "private"
    AUTH_REQUIRED = "auth_required"    # needs sign-in / Go+ / not authorized
    DRM_PROTECTED = "drm_protected"    # SoundCloud only offers DRM-encrypted streams
    NETWORK = "network"                # timeouts, DNS, connection reset, HTTP 5xx/429
    FFMPEG = "ffmpeg"                  # conversion failed or ffmpeg missing
    FILESYSTEM = "filesystem"          # permissions, disk full, bad path
    CANCELLED = "cancelled"
    INVALID_URL = "invalid_url"        # not a SoundCloud URL / unsupported
    CONFIG = "config"                  # invalid settings
    STORAGE = "storage"                # SQLite failure
    UNKNOWN = "unknown"


RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset({ErrorCategory.NETWORK})


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Immutable, thread-safe description of a failure (for events, UI and DB)."""

    category: ErrorCategory
    user_message: str
    detail: str = ""      # technical detail for logs / "details" expander (e.g. stderr tail)

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


class SclmError(Exception):
    """Base class. ``user_message`` is short, plain-language and safe to show in the UI."""

    category: ClassVar[ErrorCategory] = ErrorCategory.UNKNOWN
    default_message: ClassVar[str] = "Something went wrong."

    def __init__(self, user_message: str | None = None, *, detail: str = "") -> None:
        self.user_message = user_message or self.default_message
        self.detail = detail
        super().__init__(self.user_message if not detail else f"{self.user_message} ({detail})")

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(self.category, self.user_message, self.detail)


# --- download / access ---------------------------------------------------------------

class DownloadError(SclmError):
    """Base for failures while resolving or downloading a track."""

    default_message = "The download failed."


class TrackUnavailableError(DownloadError):
    category = ErrorCategory.UNAVAILABLE
    default_message = "This track is unavailable or has been removed."


class GeoBlockedError(DownloadError):
    category = ErrorCategory.GEO_BLOCKED
    default_message = "This track is not available in your country."


class PrivateTrackError(DownloadError):
    category = ErrorCategory.PRIVATE
    default_message = "This track or playlist is private."


class AuthRequiredError(DownloadError):
    category = ErrorCategory.AUTH_REQUIRED
    default_message = ("This track requires a SoundCloud sign-in or subscription. "
                       "SCLM does not bypass access restrictions.")


class DrmProtectedError(DownloadError):
    """Every stream SoundCloud offers is DRM-encrypted. Not an account problem: SoundCloud
    encrypts many free, label-distributed tracks as well as Go+ tracks, for everyone."""

    category = ErrorCategory.DRM_PROTECTED
    default_message = ("SoundCloud only streams this track DRM-encrypted (common for "
                       "label-released tracks, free ones included, not only Go+). It can't be "
                       "downloaded; SCLM does not bypass DRM. Listen to it on SoundCloud.")


class NetworkError(DownloadError):
    category = ErrorCategory.NETWORK
    default_message = "Network error. Check your internet connection and try again."


class InvalidUrlError(SclmError):
    category = ErrorCategory.INVALID_URL
    default_message = "That doesn't look like a SoundCloud track or playlist URL."


class JobCancelled(SclmError):
    """Raised inside a worker (e.g. from the yt-dlp progress hook) when a job is cancelled."""

    category = ErrorCategory.CANCELLED
    default_message = "Cancelled."


# --- ffmpeg ------------------------------------------------------------------------

def tail_lines(text: str, n: int = FFMPEG_STDERR_TAIL_LINES) -> str:
    """Last ``n`` non-empty lines of ``text``."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


class FFmpegError(SclmError):
    category = ErrorCategory.FFMPEG
    default_message = "Audio conversion with FFmpeg failed."

    def __init__(self, user_message: str | None = None, *, stderr: str = "",
                 detail: str = "") -> None:
        self.stderr_tail = tail_lines(stderr)
        super().__init__(user_message, detail=detail or self.stderr_tail)


class FFmpegNotFoundError(FFmpegError):
    default_message = ("FFmpeg was not found. Set its path in Settings, install it "
                       "(sudo apt install ffmpeg), or reinstall imageio-ffmpeg.")


# --- local -------------------------------------------------------------------------

class FileSystemError(SclmError):
    category = ErrorCategory.FILESYSTEM
    default_message = "A file could not be read or written."


class ConfigError(SclmError):
    category = ErrorCategory.CONFIG
    default_message = "Invalid setting."


class StorageError(SclmError):
    category = ErrorCategory.STORAGE
    default_message = "The library database could not be accessed."


def error_info_from_exception(exc: BaseException) -> ErrorInfo:
    """Convert any exception into an :class:`ErrorInfo` (never raises)."""
    if isinstance(exc, SclmError):
        return exc.to_info()
    if isinstance(exc, OSError):
        where = f": {exc.filename}" if getattr(exc, "filename", None) else ""
        reason = exc.strerror or str(exc)
        return ErrorInfo(ErrorCategory.FILESYSTEM, f"File error: {reason}{where}", repr(exc))
    return ErrorInfo(ErrorCategory.UNKNOWN, SclmError.default_message,
                     f"{type(exc).__name__}: {exc}")
