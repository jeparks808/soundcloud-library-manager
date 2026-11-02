"""classify_ytdlp_error against exceptions shaped exactly like yt-dlp raises them."""

from __future__ import annotations

import errno
import io
import socket
import sys
import urllib.error

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import (
    ContentTooShortError,
    DownloadCancelled,
    DownloadError,
    ExtractorError,
    GeoRestrictedError,
    PostProcessingError,
)

from sclm.core.errors import (
    AuthRequiredError,
    ErrorCategory,
    FFmpegNotFoundError,
    JobCancelled,
    NetworkError,
)
from sclm.download.errors import classify_ytdlp_error


def http_error(status: int, reason: str) -> HTTPError:
    return HTTPError(Response(io.BytesIO(b""), "https://api-v2.soundcloud.com/resolve",
                              {}, status=status, reason=reason))


def as_download_error(inner: BaseException | None, msg: str, *, ie: str = "soundcloud",
                      video_id: str = "artist/track", expected: bool = True) -> DownloadError:
    """Mimic yt-dlp: ExtractorError created inside ``except inner``, then
    ``YoutubeDL.trouble`` wraps it into DownloadError(message, exc_info)."""
    if inner is not None:
        try:
            raise inner
        except BaseException:
            ee = ExtractorError(msg, cause=inner, expected=expected, video_id=video_id, ie=ie)
    else:
        ee = ExtractorError(msg, expected=expected, video_id=video_id, ie=ie)
    try:
        raise ee
    except ExtractorError:
        exc_info = ee.exc_info if ee.exc_info and ee.exc_info[0] else sys.exc_info()
        return DownloadError(f"ERROR: {ee}", exc_info)


def pp_download_error(msg: str) -> DownloadError:
    try:
        raise PostProcessingError(msg)
    except PostProcessingError as pe:
        return DownloadError(f"ERROR: Postprocessing: {pe}", sys.exc_info())


def geo_download_error() -> DownloadError:
    try:
        raise GeoRestrictedError(
            "This video is not available from your location due to geo restriction",
            countries=["US"])
    except GeoRestrictedError:
        return DownloadError(
            "ERROR: This video is not available from your location due to geo restriction\n"
            "You might want to use a VPN or a proxy server (with --proxy) to workaround.",
            sys.exc_info())


CASES = [
    # (id, exception factory, expected category)
    ("http404", lambda: as_download_error(
        http_error(404, "Not Found"), "Unable to download JSON metadata: HTTP Error 404: Not Found"),
     ErrorCategory.UNAVAILABLE),
    ("http410", lambda: http_error(410, "Gone"), ErrorCategory.UNAVAILABLE),
    ("http403-private-set", lambda: as_download_error(
        http_error(403, "Forbidden"), "Unable to download JSON metadata: HTTP Error 403: Forbidden",
        ie="soundcloud:set"), ErrorCategory.PRIVATE),
    ("http401", lambda: as_download_error(
        http_error(401, "Unauthorized"),
        "Unable to download JSON metadata: HTTP Error 401: Unauthorized"),
     ErrorCategory.AUTH_REQUIRED),
    ("http429", lambda: as_download_error(
        http_error(429, "Too Many Requests"),
        "Unable to download JSON metadata: HTTP Error 429: Too Many Requests"),
     ErrorCategory.NETWORK),
    ("http503", lambda: as_download_error(
        http_error(503, "Service Unavailable"),
        "Unable to download webpage: HTTP Error 503: Service Unavailable"), ErrorCategory.NETWORK),
    ("http502-text-only", lambda: DownloadError(
        "ERROR: [soundcloud:user] 42: Unable to download JSON metadata: HTTP Error 502: Bad Gateway"),
     ErrorCategory.NETWORK),
    ("dns", lambda: as_download_error(
        TransportError(cause=urllib.error.URLError(
            socket.gaierror(-3, "Temporary failure in name resolution"))),
        "Unable to download JSON metadata: <urlopen error [Errno -3] Temporary failure in "
        "name resolution>"), ErrorCategory.NETWORK),
    ("read-timeout", lambda: as_download_error(
        TransportError("Read timed out."), "Unable to download JSON metadata: Read timed out."),
     ErrorCategory.NETWORK),
    ("conn-reset", lambda: ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
     ErrorCategory.NETWORK),
    ("socket-timeout", lambda: socket.timeout("timed out"), ErrorCategory.NETWORK),
    ("urlerror", lambda: urllib.error.URLError("unreachable"), ErrorCategory.NETWORK),
    ("content-too-short", lambda: ContentTooShortError(1000, 5000), ErrorCategory.NETWORK),
    ("geo", geo_download_error, ErrorCategory.GEO_BLOCKED),
    ("geo-text", lambda: DownloadError("ERROR: [soundcloud] 1: This track is not available in "
                                       "your country"), ErrorCategory.GEO_BLOCKED),
    ("login-required", lambda: as_download_error(
        None, "This video is only available for registered users. Use --cookies-from-browser "
              "or --cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/"
              "FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies"),
     ErrorCategory.AUTH_REQUIRED),
    ("go-plus", lambda: DownloadError("ERROR: This track requires a Go+ subscription"),
     ErrorCategory.AUTH_REQUIRED),
    ("drm", lambda: as_download_error(None, "This video is DRM protected"),
     ErrorCategory.AUTH_REQUIRED),
    ("private-text", lambda: as_download_error(None, "This track is private"),
     ErrorCategory.PRIVATE),
    ("no-formats", lambda: as_download_error(
        None, "Requested format is not available. Use --list-formats for a list of available "
              "formats", video_id="123"), ErrorCategory.UNAVAILABLE),
    ("removed", lambda: DownloadError("ERROR: [soundcloud] 1: This track has been removed"),
     ErrorCategory.UNAVAILABLE),
    ("unsupported", lambda: DownloadError(
        "ERROR: Unsupported URL: https://soundcloud.com/pages/contact"), ErrorCategory.UNAVAILABLE),
    ("ffmpeg-conversion", lambda: pp_download_error(
        "audio conversion failed: Error opening input files: Invalid data found when "
        "processing input"), ErrorCategory.FFMPEG),
    ("ffmpeg-missing", lambda: pp_download_error(
        "ffprobe and ffmpeg not found. Please install or provide the path using "
        "--ffmpeg-location"), ErrorCategory.FFMPEG),
    ("disk-full", lambda: OSError(errno.ENOSPC, "No space left on device", "/x/y.mp3"),
     ErrorCategory.FILESYSTEM),
    ("permission", lambda: PermissionError(errno.EACCES, "Permission denied", "/lib"),
     ErrorCategory.FILESYSTEM),
    ("yt-cancelled", lambda: DownloadCancelled(), ErrorCategory.CANCELLED),
    ("unknown", lambda: RuntimeError("boom"), ErrorCategory.UNKNOWN),
]


@pytest.mark.parametrize("make_exc,category", [(c[1], c[2]) for c in CASES],
                         ids=[c[0] for c in CASES])
def test_classification(make_exc, category):
    err = classify_ytdlp_error(make_exc())
    assert err.category == category
    assert err.user_message
    assert err.retryable == (category == ErrorCategory.NETWORK)


def test_only_network_is_retryable():
    assert classify_ytdlp_error(TransportError("Read timed out.")).retryable
    assert not classify_ytdlp_error(as_download_error(None, "This track is private")).retryable


def test_detail_is_cleaned():
    err = classify_ytdlp_error(as_download_error(
        http_error(404, "Not Found"), "Unable to download JSON metadata: HTTP Error 404: Not Found"))
    assert not err.detail.startswith("ERROR:")
    assert "404" in err.detail


def test_sclm_error_in_chain_is_returned_as_is():
    cancelled = JobCancelled()
    try:
        raise cancelled
    except JobCancelled:
        wrapped = DownloadError("ERROR: Cancelled.", sys.exc_info())
    assert classify_ytdlp_error(wrapped) is cancelled
    auth = AuthRequiredError()
    assert classify_ytdlp_error(auth) is auth


def test_ffmpeg_missing_is_not_found_subclass():
    err = classify_ytdlp_error(pp_download_error("ffprobe and ffmpeg not found. Please install"))
    assert isinstance(err, FFmpegNotFoundError)


def test_network_wrapped_in_cause_chain():
    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            raise ValueError("wrapper") from inner
    except ValueError as outer:
        assert isinstance(classify_ytdlp_error(outer), NetworkError)
