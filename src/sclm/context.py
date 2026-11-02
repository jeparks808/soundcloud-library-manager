"""Composition-root containers passed from ``sclm.app`` to the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sclm.core.paths import AppPaths
from sclm.core.settings import SettingsStore
from sclm.core.storage import Storage

if TYPE_CHECKING:
    from sclm.download.manager import DownloadManager
    from sclm.gui.bridge import DownloadBridge


@dataclass(frozen=True, slots=True)
class CoreServices:
    """Qt-free services, built by ``sclm.app.build_core``."""

    paths: AppPaths
    settings: SettingsStore
    storage: Storage


@dataclass(frozen=True, slots=True)
class AppContext:
    """Everything the GUI needs. Built once in ``sclm.app.main``; passed to ``MainWindow``."""

    paths: AppPaths
    settings: SettingsStore
    storage: Storage
    manager: DownloadManager
    bridge: DownloadBridge
