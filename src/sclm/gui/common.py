"""Small helpers shared by the GUI views (formatting, folder opening, error banner)."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sclm.core.errors import ErrorInfo
from sclm.core.models import JobStatus

log = logging.getLogger(__name__)

STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RESOLVING: "Resolving",
    JobStatus.DOWNLOADING: "Downloading",
    JobStatus.CONVERTING: "Converting",
    JobStatus.TAGGING: "Tagging",
    JobStatus.ORGANIZING: "Organizing",
    JobStatus.WAITING_RETRY: "Waiting to retry",
    JobStatus.COMPLETED: "Completed",
    JobStatus.SKIPPED_DUPLICATE: "Duplicate (skipped)",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}


def format_duration(seconds: float | None) -> str:
    """``3:07`` / ``1:02:03``; empty string when unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return ""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(num_bytes: float | None) -> str:
    if num_bytes is None or num_bytes < 0:
        return ""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""  # pragma: no cover


def format_speed(bps: float | None) -> str:
    return f"{format_size(bps)}/s" if bps else ""


def open_containing_folder(path: Path) -> bool:
    """Open the folder holding ``path`` in the desktop file manager (GUI-thread safe: it only
    hands a URL to the desktop)."""
    folder = path if path.is_dir() else path.parent
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class ErrorBanner(QFrame):
    """Inline error message with an optional collapsible "Details" section."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("errorBanner")
        self.setAccessibleName("Error message")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)
        top = QHBoxLayout()
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._details_btn = QPushButton("Details")
        self._details_btn.setProperty("flat", True)
        self._details_btn.setCheckable(True)
        self._details_btn.setAccessibleName("Show error details")
        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._details.setMaximumHeight(110)
        self._details.hide()
        self._details_btn.toggled.connect(self._details.setVisible)
        top.addWidget(self._label, 1)
        top.addWidget(self._details_btn, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(top)
        outer.addWidget(self._details)
        self.hide()

    @property
    def message(self) -> str:
        return self._label.text()

    @property
    def details(self) -> str:
        return self._details.toPlainText()

    def show_error(self, error: ErrorInfo | str) -> None:
        if isinstance(error, ErrorInfo):
            text, detail = error.user_message, error.detail
        else:
            text, detail = str(error), ""
        self._label.setText(text)
        self._details.setPlainText(detail)
        self._details_btn.setVisible(bool(detail))
        if not detail:
            self._details_btn.setChecked(False)
        self.show()

    def clear(self) -> None:
        self._label.clear()
        self._details.clear()
        self._details_btn.setChecked(False)
        self.hide()


def set_primary(button: QPushButton, primary: bool = True) -> None:
    """Mark a button as the accent-coloured primary action (QSS ``[primary="true"]``)."""
    button.setProperty("primary", primary)
    button.style().unpolish(button)
    button.style().polish(button)


def set_role(label: QLabel, role: str) -> QLabel:
    """Set the QSS ``role`` property (``muted``, ``title``, ``heading``) and return the label."""
    label.setProperty("role", role)
    return label
