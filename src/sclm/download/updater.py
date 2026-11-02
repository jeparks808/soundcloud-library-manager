"""Self-update of yt-dlp (Help → "Update yt-dlp…").

SoundCloud changes its site now and then; a yt-dlp that is pinned forever eventually stops
working. The frozen AppImage can't be pip-upgraded, so the updater downloads the latest
yt-dlp wheel from PyPI, verifies its SHA-256 against PyPI's own metadata, and unpacks the
``yt_dlp`` package into ``<data_dir>/yt-dlp-override/``. At the next start,
:func:`activate_override` (called from ``sclm.app.main`` before anything imports yt_dlp)
prepends that directory to ``sys.path`` — but only when it holds a version *newer* than the
bundled one, so a rebuilt app with a fresher bundled yt-dlp quietly wins again.

Everything here is best effort and reversible: a missing, stale or broken override is
ignored (with a log line) and the bundled yt-dlp is used. Nothing outside
``yt-dlp-override`` is written. No binary code is involved beyond yt-dlp itself, which the
app already runs; the wheel is fetched from PyPI over HTTPS and hash-checked.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sclm.core.errors import ErrorCategory, SclmError
from sclm.core.paths import AppPaths

log = logging.getLogger(__name__)

OVERRIDE_DIRNAME = "yt-dlp-override"
PYPI_JSON_URL = "https://pypi.org/pypi/yt-dlp/json"
_TIMEOUT_S = 30.0
_MAX_WHEEL_BYTES = 60 * 1024 * 1024  # a yt-dlp wheel is ~6 MB; anything near this is wrong
_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)

#: ``fetch(url) -> bytes`` seam for tests.
Fetch = Callable[[str], bytes]


class UpdateError(SclmError):
    """The yt-dlp update could not be completed. The installed yt-dlp is unchanged."""

    category = ErrorCategory.NETWORK
    default_message = "Could not update yt-dlp."


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Outcome of :func:`update`."""

    updated: bool          #: True when a new version was installed (takes effect on restart)
    version: str           #: the version now pending (updated) or currently active
    previous: str | None   #: the previously active version, when known


def override_dir(paths: AppPaths) -> Path:
    return paths.data_dir / OVERRIDE_DIRNAME


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (https only)
        return resp.read(_MAX_WHEEL_BYTES + 1)


def _version_tuple(version: str) -> tuple[int, ...]:
    """yt-dlp versions are dotted dates like ``2026.10.26`` (sometimes with a 4th part)."""
    parts: list[int] = []
    for p in version.split("."):
        digits = re.match(r"\d+", p)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def _module_version(version_py: Path) -> str | None:
    """Read ``__version__`` out of a ``yt_dlp/version.py`` without importing it."""
    try:
        m = _VERSION_RE.search(version_py.read_text("utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def bundled_version_file() -> Path | None:
    """``version.py`` of the yt_dlp that ``import yt_dlp`` would use *without* the override.

    Uses ``find_spec``, which locates a top-level package without executing it.
    """
    try:
        spec = importlib.util.find_spec("yt_dlp")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "version.py"


def installed_version() -> str:
    """Version of the yt_dlp the app is running with (override included, once activated)."""
    from yt_dlp.version import __version__

    return str(__version__)


# --- startup activation -------------------------------------------------------------------


def activate_override(paths: AppPaths, *, bundled_version: str | None = None) -> str | None:
    """Prepend the override to ``sys.path`` when it is newer than the bundled yt-dlp.

    Must run before anything imports ``yt_dlp``. Returns the override version when it was
    activated and verified by a probe import, else None. Never raises: on any problem the
    override is deactivated again and the bundled yt-dlp stays in charge.
    """
    root = override_dir(paths)
    want = _module_version(root / "yt_dlp" / "version.py")
    if want is None:
        return None
    if "yt_dlp" in sys.modules:
        log.warning("yt-dlp override %s not activated: yt_dlp is already imported", want)
        return None
    if bundled_version is None:
        vfile = bundled_version_file()
        bundled_version = _module_version(vfile) if vfile else None
    if bundled_version and _version_tuple(want) <= _version_tuple(bundled_version):
        log.info("Ignoring yt-dlp override %s: bundled %s is the same or newer",
                 want, bundled_version)
        return None
    entry = str(root)
    sys.path.insert(0, entry)
    importlib.invalidate_caches()
    try:
        import yt_dlp.version as active  # probe: the override must actually import

        if str(active.__version__) != want:
            raise ImportError(
                f"override reports {want} but {active.__version__} was imported")
    except BaseException as exc:
        log.warning("yt-dlp override %s is unusable (%s); using the bundled version",
                    want, exc)
        _deactivate(entry)
        return None
    log.info("Using updated yt-dlp %s from %s (bundled: %s)", want, root,
             bundled_version or "unknown")
    return want


def _deactivate(entry: str) -> None:
    with contextlib.suppress(ValueError):
        sys.path.remove(entry)
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


# --- check + install ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Release:
    version: str
    wheel_url: str
    sha256: str


def _check_latest(fetch: Fetch) -> _Release:
    try:
        meta = json.loads(fetch(PYPI_JSON_URL).decode("utf-8"))
        version = str(meta["info"]["version"])
        for f in meta["urls"]:
            if f.get("packagetype") == "bdist_wheel" and f["filename"].endswith(".whl"):
                return _Release(version, str(f["url"]), str(f["digests"]["sha256"]))
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(detail=f"PyPI metadata: {type(exc).__name__}: {exc}") from exc
    raise UpdateError(detail=f"PyPI lists no wheel for yt-dlp {version}")


def _extract_wheel(wheel: bytes, dest: Path) -> None:
    """Extract only the ``yt_dlp/`` package (zip-slip-safe) into ``dest``."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
        names = [n for n in zf.namelist() if n.startswith("yt_dlp/")]
        if "yt_dlp/version.py" not in names or "yt_dlp/__init__.py" not in names:
            raise UpdateError(detail="wheel does not contain the yt_dlp package")
        for name in names:
            target = dest / name
            if not target.resolve().is_relative_to(dest.resolve()):
                raise UpdateError(detail=f"unsafe path in wheel: {name}")
            if name.endswith("/"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))


def _install(release: _Release, paths: AppPaths, fetch: Fetch) -> None:
    try:
        wheel = fetch(release.wheel_url)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(detail=f"download: {type(exc).__name__}: {exc}") from exc
    if len(wheel) > _MAX_WHEEL_BYTES:
        raise UpdateError(detail=f"wheel is implausibly large ({len(wheel)} bytes)")
    digest = hashlib.sha256(wheel).hexdigest()
    if digest != release.sha256:
        raise UpdateError(
            user_message="Could not update yt-dlp: the download failed verification.",
            detail=f"sha256 {digest} != PyPI {release.sha256}")

    root = override_dir(paths)
    root.parent.mkdir(parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix=f".{OVERRIDE_DIRNAME}.new-", dir=root.parent))
    old = root.parent / f".{OVERRIDE_DIRNAME}.old"
    try:
        _extract_wheel(wheel, fresh)
        got = _module_version(fresh / "yt_dlp" / "version.py")
        if got != release.version:
            raise UpdateError(detail=f"wheel contains {got}, expected {release.version}")
        shutil.rmtree(old, ignore_errors=True)
        if root.exists():
            root.rename(old)
        fresh.rename(root)
        shutil.rmtree(old, ignore_errors=True)
    except UpdateError:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(fresh, ignore_errors=True)
        raise UpdateError(detail=f"install: {exc}") from exc


def update(paths: AppPaths, *, fetch: Fetch = _fetch) -> UpdateResult:
    """Check PyPI and install the latest yt-dlp when it is newer than the active one.

    Blocking (network); run it off the GUI thread. The new version takes effect at the next
    app start, via :func:`activate_override`.
    """
    try:
        current = installed_version()
    except Exception:  # yt_dlp broken/absent: any successful update is an improvement
        current = None
    release = _check_latest(fetch)
    if current is not None and _version_tuple(release.version) <= _version_tuple(current):
        return UpdateResult(updated=False, version=current, previous=current)
    pending = _module_version(override_dir(paths) / "yt_dlp" / "version.py")
    if pending is not None and _version_tuple(release.version) <= _version_tuple(pending):
        return UpdateResult(updated=True, version=pending, previous=current)
    _install(release, paths, fetch)
    log.info("yt-dlp %s downloaded (active: %s); takes effect at the next start",
             release.version, current or "none")
    return UpdateResult(updated=True, version=release.version, previous=current)
