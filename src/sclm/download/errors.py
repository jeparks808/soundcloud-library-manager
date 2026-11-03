"""Map yt-dlp / network / OS exceptions onto the SCLM error hierarchy (ARCHITECTURE.md §7).

yt-dlp reports most failures as ``yt_dlp.utils.DownloadError`` whose message is the
user-facing text (``"ERROR: [soundcloud] 123: ..."``) and whose ``exc_info`` holds the
original exception (often an ``ExtractorError`` or a networking ``HTTPError``). We walk that
chain (``exc_info``, ``cause``, ``__cause__``, ``__context__``) and classify by exception
type first, then by HTTP status, then by message text.

Only NETWORK is retried automatically. Access restrictions (private, geo, sign-in / Go+,
DRM) are reported and never worked around. DRM is its own category: signing in does not
help, because SoundCloud encrypts some free tracks for every listener.
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
from collections.abc import Iterator

from sclm.core.errors import (
    AuthRequiredError,
    DrmProtectedError,
    FFmpegError,
    FFmpegNotFoundError,
    FileSystemError,
    GeoBlockedError,
    JobCancelled,
    NetworkError,
    PrivateTrackError,
    SclmError,
    TrackUnavailableError,
)

_MAX_DETAIL = 2000
_MAX_CHAIN = 12

DRM_MESSAGE = DrmProtectedError.default_message
PREVIEW_ONLY_MESSAGE = ("Only a 30-second preview of this track is available (SoundCloud Go+). "
                        "SCLM does not save previews as full tracks.")

_GEO = re.compile(r"geo[ -]?restrict|not available (?:from|in) your (?:location|country)"
                  r"|not available in your region|blocked in your country", re.I)
_DRM = re.compile(r"\bdrm\b", re.I)
_PRIVATE = re.compile(r"\bprivate\b", re.I)
_AUTH = re.compile(r"sign[ -]?in|log[ -]?in\b|login|registered users|go\+|subscription"
                   r"|authenticat|unauthori[sz]ed|premium|--cookies", re.I)
_HTTP_CODE = re.compile(r"HTTP Error (\d{3})", re.I)
_NETWORK = re.compile(
    r"timed out|timeout|connection (?:reset|refused|aborted)|remote end closed"
    r"|network is unreachable|no route to host|temporary failure in name resolution"
    r"|name or service not known|nodename nor servname|getaddrinfo failed"
    r"|failed to resolve|temporarily unavailable|too many requests|eof occurred"
    r"|incomplete ?read|bytes read|ssl:|unable to connect|connection broken", re.I)
_UNAVAILABLE = re.compile(
    r"not found|has been removed|not available|unavailable|no video formats"
    r"|requested format is not available|no formats|unsupported url|does not exist"
    r"|is not streamable|deleted", re.I)
_FFMPEG_MISSING = re.compile(r"ffmpeg (?:is )?not found|ffprobe and ffmpeg not found"
                             r"|ffmpeg could not be found", re.I)
_ERROR_PREFIX = re.compile(r"^(?:ERROR:\s*)+")


def _clean(text: str) -> str:
    text = _ERROR_PREFIX.sub("", (text or "").strip())
    # yt-dlp appends a long "please report this issue" note to unexpected errors.
    cut = text.find("; please report this issue")
    if cut != -1:
        text = text[:cut]
    return text[:_MAX_DETAIL]


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and everything it wraps, without repeats."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo and len(seen) < _MAX_CHAIN:
        cur = todo.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        exc_info = getattr(cur, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) >= 2 \
                and isinstance(exc_info[1], BaseException):
            todo.append(exc_info[1])
        cause = getattr(cur, "cause", None)
        if isinstance(cause, BaseException):
            todo.append(cause)
        if cur.__cause__ is not None:
            todo.append(cur.__cause__)
        if cur.__context__ is not None and not cur.__suppress_context__:
            todo.append(cur.__context__)


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    return None


def _from_status(status: int, text: str, detail: str) -> SclmError | None:
    if status == 429 or 500 <= status <= 599 or status == 408:
        return NetworkError(detail=detail)
    if status in (404, 410):
        return TrackUnavailableError(detail=detail)
    if status == 401:
        return AuthRequiredError(detail=detail)
    if status == 403:
        if _GEO.search(text):
            return GeoBlockedError(detail=detail)
        return PrivateTrackError(detail=detail)
    if 400 <= status <= 499:
        return TrackUnavailableError(detail=detail)
    return None


def _is_network_type(exc: BaseException) -> bool:
    try:
        from yt_dlp.networking.exceptions import TransportError
        from yt_dlp.utils import ContentTooShortError
    except ImportError:  # pragma: no cover - yt-dlp is a hard dependency
        TransportError = ContentTooShortError = ()  # type: ignore[assignment,misc]
    if isinstance(exc, (TransportError, ContentTooShortError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return False  # handled by status
    return isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError,
                            ConnectionError, socket.gaierror, ssl.SSLError))


def classify_ytdlp_error(exc: BaseException) -> SclmError:
    """Return the :class:`SclmError` that best describes ``exc`` (never raises).

    An ``SclmError`` anywhere in the chain (e.g. ``JobCancelled`` raised by our progress hook
    and wrapped by yt-dlp) is returned as is.
    """
    chain = list(_chain(exc))
    detail = _clean(str(exc)) or type(exc).__name__
    text = " | ".join(_clean(str(e)) for e in chain)

    try:
        from yt_dlp.utils import DownloadCancelled, GeoRestrictedError, PostProcessingError
    except ImportError:  # pragma: no cover
        DownloadCancelled = GeoRestrictedError = PostProcessingError = ()  # type: ignore

    for e in chain:
        if isinstance(e, SclmError):
            return e
    for e in chain:
        if isinstance(e, DownloadCancelled):
            return JobCancelled(detail=detail)
    for e in chain:
        if isinstance(e, GeoRestrictedError):
            return GeoBlockedError(detail=detail)
    for e in chain:
        if isinstance(e, PostProcessingError):
            if _FFMPEG_MISSING.search(text):
                return FFmpegNotFoundError(detail=detail)
            return FFmpegError(detail=detail)

    # Access restrictions expressed in text win over the HTTP status (a 403 "private" or
    # "geo" message is more specific than the bare code).
    if _GEO.search(text):
        return GeoBlockedError(detail=detail)
    if _DRM.search(text):
        return DrmProtectedError(detail=detail)

    for e in chain:
        status = _http_status(e)
        if status is not None and (isinstance(e, urllib.error.HTTPError)
                                   or type(e).__name__ == "HTTPError"):
            result = _from_status(status, text, detail)
            if result is not None:
                return result
    match = _HTTP_CODE.search(text)
    if match:
        result = _from_status(int(match.group(1)), text, detail)
        if result is not None:
            return result

    if _PRIVATE.search(text):
        return PrivateTrackError(detail=detail)
    if _AUTH.search(text):
        return AuthRequiredError(detail=detail)
    if _FFMPEG_MISSING.search(text):
        return FFmpegNotFoundError(detail=detail)
    if any(_is_network_type(e) for e in chain) or _NETWORK.search(text):
        return NetworkError(detail=detail)
    if _UNAVAILABLE.search(text):
        return TrackUnavailableError(detail=detail)

    for e in chain:
        if isinstance(e, OSError):
            where = f": {e.filename}" if getattr(e, "filename", None) else ""
            reason = e.strerror or str(e)
            return FileSystemError(f"File error: {reason}{where}", detail=repr(e))
    err = SclmError(detail=f"{type(exc).__name__}: {detail}")
    return err
