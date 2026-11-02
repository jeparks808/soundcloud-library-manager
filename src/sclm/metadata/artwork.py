"""Cover artwork download. Failure is never an error: no artwork just means no APIC frame."""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from sclm import __version__

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0
MAX_ARTWORK_BYTES = 8 * 1024 * 1024

ArtworkFetcher = Callable[[str], "bytes | None"]


def image_mime(data: bytes) -> str | None:
    """``image/jpeg`` or ``image/png`` from the magic bytes, else ``None``."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def fetch_artwork(url: str, *, timeout: float = DEFAULT_TIMEOUT_S,
                  opener: Callable[..., Any] = urllib.request.urlopen) -> bytes | None:
    """Download a JPEG/PNG cover. Returns ``None`` (and logs) on any failure: non-http(s)
    URL, network error, timeout, oversize response, or data that isn't JPEG/PNG.
    ``opener`` is ``urllib.request.urlopen``-compatible (injectable for tests)."""
    scheme = urlsplit(url or "").scheme.lower()
    if scheme not in ("http", "https"):
        log.info("Artwork skipped: unsupported URL %r", url)
        return None
    request = urllib.request.Request(url, headers={"User-Agent": f"sclm/{__version__}"})
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read(MAX_ARTWORK_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - any failure means "no artwork"
        log.warning("Artwork could not be downloaded from %s: %s", url, exc)
        return None
    if len(data) > MAX_ARTWORK_BYTES:
        log.warning("Artwork at %s is larger than %d bytes; skipped", url, MAX_ARTWORK_BYTES)
        return None
    if image_mime(data) is None:
        log.warning("Artwork at %s is not a JPEG/PNG image; skipped", url)
        return None
    return data
