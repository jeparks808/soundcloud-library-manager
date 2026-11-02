"""User settings: a frozen, validated dataclass persisted as JSON in the XDG config dir.

* :class:`Settings` validates in ``__post_init__`` and raises :class:`ConfigError`.
* :func:`load_settings` is lenient: unknown keys are ignored, invalid values fall back to
  defaults (logged), a corrupt file is renamed to ``settings.json.bad`` and defaults are used.
* :func:`save_settings` writes atomically (temp file + ``os.replace``).
* :class:`SettingsStore` holds the current settings for the running app (thread-safe reads,
  change notification for the GUI and DownloadManager).
"""

from __future__ import annotations

import json
import logging
import os
import string
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from sclm.core.errors import ConfigError
from sclm.core.models import ConflictPolicy, OrganizeMode

log = logging.getLogger(__name__)

SETTINGS_SCHEMA_VERSION = 1
BITRATES_KBPS: tuple[int, ...] = (128, 192, 256, 320)
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 4
DEFAULT_LIBRARY_DIR = "~/Music/SoundCloud"
DEFAULT_FILENAME_TEMPLATE = "{artist} - {title}"
# Fields usable in filename_template. Rendered by metadata.naming.render_filename.
FILENAME_FIELDS: frozenset[str] = frozenset({
    "artist", "title", "playlist", "index", "track_id", "genre", "year", "album",
})


def validate_filename_template(template: str) -> None:
    """Raise :class:`ConfigError` unless ``template`` is a valid filename template.

    Rules: non-empty; ``str.format`` syntax with only names from ``FILENAME_FIELDS``;
    no positional fields, conversions or format specs; must contain ``{title}``;
    must not contain path separators (folders come from ``organize_mode``).
    The ``.mp3`` extension is appended automatically and must not be included.
    """
    if not template or not template.strip():
        raise ConfigError("The filename template can't be empty.")
    if "/" in template or "\\" in template:
        raise ConfigError("The filename template can't contain '/' or '\\'.")
    if template.lower().endswith(".mp3"):
        raise ConfigError("Leave out the .mp3 extension; it is added automatically.")
    names: set[str] = set()
    try:
        for _literal, name, spec, conv in string.Formatter().parse(template):
            if name is None:
                continue
            if name == "" or name.isdigit():
                raise ConfigError("Use named fields like {artist}, not {}.")
            if spec or conv:
                raise ConfigError(f"Format options aren't supported in {{{name}}}.")
            if name not in FILENAME_FIELDS:
                allowed = ", ".join("{" + f + "}" for f in sorted(FILENAME_FIELDS))
                raise ConfigError(f"Unknown field {{{name}}}. Allowed: {allowed}.")
            names.add(name)
    except ValueError as exc:  # unbalanced braces
        raise ConfigError(f"Invalid filename template: {exc}") from exc
    if "title" not in names:
        raise ConfigError("The filename template must include {title}.")


@dataclass(frozen=True, slots=True)
class Settings:
    library_dir: str = DEFAULT_LIBRARY_DIR
    bitrate_kbps: int = 320
    concurrency: int = 2
    organize_mode: OrganizeMode = OrganizeMode.PLAYLIST
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    ffmpeg_path: str = ""                 # '' = auto (PATH, then bundled imageio-ffmpeg)
    beets_enabled: bool = False
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    only_new_tracks: bool = True          # preview pre-selects only tracks not in library
    embed_artwork: bool = True
    cookies_file: str = ""                # optional Netscape cookies.txt for the user's own
                                          # authorized content; '' = anonymous

    def __post_init__(self) -> None:
        # Coerce enum-typed fields given as strings (e.g. from JSON).
        try:
            object.__setattr__(self, "organize_mode", OrganizeMode(self.organize_mode))
        except ValueError:
            raise ConfigError(f"Unknown organize mode: {self.organize_mode!r}.") from None
        try:
            object.__setattr__(self, "conflict_policy", ConflictPolicy(self.conflict_policy))
        except ValueError:
            raise ConfigError(f"Unknown conflict policy: {self.conflict_policy!r}.") from None
        for name in ("library_dir", "filename_template", "ffmpeg_path", "cookies_file"):
            if not isinstance(getattr(self, name), str):
                raise ConfigError(f"{name} must be text.")
        for name in ("beets_enabled", "only_new_tracks", "embed_artwork"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"{name} must be true or false.")
        if not self.library_dir.strip():
            raise ConfigError("The library folder can't be empty.")
        if type(self.bitrate_kbps) is not int or self.bitrate_kbps not in BITRATES_KBPS:
            raise ConfigError(f"Bitrate must be one of {', '.join(map(str, BITRATES_KBPS))} kbps.")
        if (type(self.concurrency) is not int
                or not CONCURRENCY_MIN <= self.concurrency <= CONCURRENCY_MAX):
            raise ConfigError(
                f"Concurrent downloads must be between {CONCURRENCY_MIN} and {CONCURRENCY_MAX}.")
        validate_filename_template(self.filename_template)

    @property
    def library_path(self) -> Path:
        """``library_dir`` with ``~`` expanded, made absolute."""
        return Path(self.library_dir).expanduser().absolute()

    @property
    def ffmpeg_path_or_none(self) -> str | None:
        return self.ffmpeg_path.strip() or None

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies_file).expanduser() if self.cookies_file.strip() else None

    def replace(self, **changes: Any) -> Settings:
        """Validated copy with ``changes`` applied (raises ConfigError)."""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["organize_mode"] = str(self.organize_mode)
        data["conflict_policy"] = str(self.conflict_policy)
        return {"schema_version": SETTINGS_SCHEMA_VERSION, **data}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Lenient construction: ignore unknown keys, replace invalid values with defaults."""
        if not isinstance(data, dict):
            log.warning("Settings data is not an object; using defaults")
            return cls()
        known = {f.name for f in fields(cls)}
        result = cls()
        for key, value in data.items():
            if key not in known:
                continue
            try:
                result = replace(result, **{key: value})
            except (ConfigError, TypeError) as exc:
                log.warning("Ignoring invalid setting %s=%r: %s", key, value, exc)
        return result


def load_settings(path: Path) -> Settings:
    """Load settings from ``path``; missing file -> defaults (not written)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as exc:
        log.warning("Could not read settings %s: %s; using defaults", path, exc)
        return Settings()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        backup = path.with_name(path.name + ".bad")
        log.warning("Corrupt settings file %s (%s); moved to %s, using defaults",
                    path, exc, backup)
        try:
            os.replace(path, backup)
        except OSError:
            log.exception("Could not move corrupt settings file aside")
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path) -> None:
    """Atomically write ``settings`` as pretty JSON to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


SettingsListener = Callable[[Settings, Settings], None]   # (old, new)


class SettingsStore:
    """The running app's current settings.

    ``get()`` is safe from any thread. ``update()`` validates (by construction), saves, swaps
    the current value, then calls listeners synchronously on the caller's thread with
    ``(old, new)``. Listeners must be quick; exceptions in listeners are logged and swallowed.
    Workers should call ``get()`` once at job start and use that snapshot for the whole job.
    """

    def __init__(self, path: Path, initial: Settings | None = None) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._current = initial if initial is not None else load_settings(self._path)
        self._listeners: list[SettingsListener] = []

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> Settings:
        with self._lock:
            return self._current

    def update(self, new: Settings) -> None:
        save_settings(new, self._path)
        with self._lock:
            old, self._current = self._current, new
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(old, new)
            except Exception:
                log.exception("Settings listener failed")

    def subscribe(self, listener: SettingsListener) -> Callable[[], None]:
        """Register ``listener``; returns a function that unsubscribes it."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe
