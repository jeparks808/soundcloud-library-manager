"""Main application window (Stage 2 implements).

STUB. The constructor signature is the contract with ``sclm.app.main``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow

from sclm.context import AppContext


class MainWindow(QMainWindow):
    """Top-level window: URL/preview panel, queue, history, library, settings access.

    Receives everything through ``ctx``; constructs no services itself. Connects
    ``ctx.bridge.job_changed`` / ``ctx.bridge.progress`` to its queue model. On close, asks
    for confirmation if ``not ctx.manager.is_idle()``; ``app.main`` performs the shutdown.
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        raise NotImplementedError
