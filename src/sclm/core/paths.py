"""XDG base directories for the application, overridable for tests.

Resolution order for each directory:
1. ``SCLM_HOME`` env var set -> everything lives under that one directory
   (``config/``, ``data/``, ``cache/``, ``state/``). Used by tests and dev runs.
2. ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` / ``XDG_CACHE_HOME`` / ``XDG_STATE_HOME``
   (only if absolute, per the XDG spec), each joined with ``APP_DIR_NAME``.
3. ``~/.config``, ``~/.local/share``, ``~/.cache``, ``~/.local/state``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "soundcloud-library-manager"

_XDG_DEFAULTS = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
    "XDG_STATE_HOME": ".local/state",
}


def _xdg_dir(env: Mapping[str, str], var: str, home: Path) -> Path:
    value = env.get(var, "")
    if value and os.path.isabs(value):
        return Path(value) / APP_DIR_NAME
    return home / _XDG_DEFAULTS[var] / APP_DIR_NAME


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Application directories. Construct with :meth:`from_env` or :meth:`under`."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppPaths:
        env = os.environ if env is None else env
        sclm_home = env.get("SCLM_HOME", "")
        if sclm_home:
            return cls.under(Path(sclm_home).expanduser())
        home = Path(env.get("HOME") or Path.home())
        return cls(
            config_dir=_xdg_dir(env, "XDG_CONFIG_HOME", home),
            data_dir=_xdg_dir(env, "XDG_DATA_HOME", home),
            cache_dir=_xdg_dir(env, "XDG_CACHE_HOME", home),
            state_dir=_xdg_dir(env, "XDG_STATE_HOME", home),
        )

    @classmethod
    def under(cls, root: Path) -> AppPaths:
        """All directories under one root (tests, portable runs)."""
        root = Path(root)
        return cls(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            state_dir=root / "state",
        )

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "library.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "sclm.log"

    @property
    def staging_dir(self) -> Path:
        """Parent of all per-job staging dirs. Owned by the app; safe to wipe at startup."""
        return self.cache_dir / "staging"

    def job_staging_dir(self, job_id: int) -> Path:
        """Per-job staging directory (not created by this call)."""
        return self.staging_dir / f"job-{job_id}"

    def ensure(self) -> AppPaths:
        """Create all directories (idempotent). Returns self for chaining."""
        for d in (self.config_dir, self.data_dir, self.cache_dir, self.state_dir,
                  self.log_dir, self.staging_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self
