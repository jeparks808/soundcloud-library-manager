"""Dark theme with SoundCloud-orange accents: a QPalette plus one QSS string.

``apply_theme(app)`` is idempotent and cheap. Colours are exposed as constants so custom
painting (progress bars, status text) matches the stylesheet.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from sclm.core.models import JobStatus

ACCENT = "#ff5500"
ACCENT_HOVER = "#ff7233"
ACCENT_PRESSED = "#d94800"
BG = "#17181b"            # window background
BG_ALT = "#1e1f23"        # panels, nav
SURFACE = "#25272c"       # inputs, table rows
SURFACE_ALT = "#2a2c32"   # alternating rows
BORDER = "#34363d"
TEXT = "#e8e9ec"
TEXT_MUTED = "#9a9da6"
TEXT_ON_ACCENT = "#ffffff"
SUCCESS = "#3fb950"
WARNING = "#e3b341"
DANGER = "#f85149"
INFO = "#58a6ff"

STATUS_COLORS: dict[JobStatus, str] = {
    JobStatus.QUEUED: TEXT_MUTED,
    JobStatus.RESOLVING: INFO,
    JobStatus.DOWNLOADING: ACCENT,
    JobStatus.CONVERTING: ACCENT,
    JobStatus.TAGGING: ACCENT,
    JobStatus.ORGANIZING: ACCENT,
    JobStatus.WAITING_RETRY: WARNING,
    JobStatus.COMPLETED: SUCCESS,
    JobStatus.SKIPPED_DUPLICATE: WARNING,
    JobStatus.FAILED: DANGER,
    JobStatus.CANCELLED: TEXT_MUTED,
}


def build_palette() -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: SURFACE_ALT,
        QPalette.ColorRole.ToolTipBase: BG_ALT,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: TEXT_ON_ACCENT,
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: TEXT_ON_ACCENT,
        QPalette.ColorRole.PlaceholderText: TEXT_MUTED,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_MUTED))
    return pal


STYLESHEET = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {BG_ALT}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}

QLabel[role="muted"] {{ color: {TEXT_MUTED}; }}
QLabel[role="title"] {{ font-size: 18pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 12pt; font-weight: 600; }}

/* navigation */
QFrame#nav {{ background: {BG_ALT}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ color: {ACCENT}; font-size: 13pt; font-weight: 700; padding: 14px 16px 10px 16px; }}
QListWidget#navList {{ background: transparent; border: none; outline: none; padding: 4px 8px; }}
QListWidget#navList::item {{ padding: 0 12px; border-radius: 6px; margin: 2px 0; }}
QListWidget#navList::item:hover {{ background: {SURFACE}; }}
QListWidget#navList::item:selected {{ background: {SURFACE}; color: {ACCENT};
    border-left: 3px solid {ACCENT}; }}

/* buttons */
QPushButton, QToolButton {{ background: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 6px 14px; min-height: 20px; }}
QPushButton:hover, QToolButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {BG_ALT}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {TEXT_MUTED}; border-color: {BG_ALT};
    background: {BG_ALT}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton[primary="true"] {{ background: {ACCENT}; border-color: {ACCENT};
    color: {TEXT_ON_ACCENT}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[primary="true"]:disabled {{ background: #5a3322; border-color: #5a3322;
    color: #b79a8c; }}
QPushButton[flat="true"], QToolButton[flat="true"] {{ background: transparent; border: none;
    color: {ACCENT}; padding: 2px 6px; }}

/* inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{ background: {SURFACE};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: {TEXT_ON_ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border-color: {ACCENT}; }}
QLineEdit[invalid="true"] {{ border-color: {DANGER}; }}
QLineEdit:read-only {{ color: {TEXT_MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; outline: none; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {BORDER}; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url("{{CHECK}}"); }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 12px;
    padding: 14px 12px 10px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {TEXT_MUTED}; }}

/* tables */
QTableView {{ background: {SURFACE}; alternate-background-color: {SURFACE_ALT};
    border: 1px solid {BORDER}; border-radius: 6px; gridline-color: transparent;
    selection-background-color: #5a2a12; selection-color: {TEXT}; outline: none; }}
QTableView::item {{ padding: 2px 6px; border: none; }}
QTableView::item:focus {{ border: none; outline: none; }}
QTableView::indicator {{ width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {BORDER}; background: {BG_ALT}; }}
QTableView::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url("{{CHECK}}"); }}
QHeaderView::section {{ background: {BG_ALT}; color: {TEXT_MUTED}; padding: 6px 8px;
    border: none; border-bottom: 1px solid {BORDER}; font-weight: 600; text-align: left; }}
QTableCornerButton::section {{ background: {BG_ALT}; border: none; }}

QProgressBar {{ background: {BG_ALT}; border: none; border-radius: 3px; max-height: 6px;
    text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {TEXT_MUTED}; }}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background: {BG_ALT}; }}
QMenuBar::item:selected {{ background: {SURFACE}; }}
QMenu {{ background: {BG_ALT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {TEXT_ON_ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QFrame#errorBanner {{ background: #3a1d1d; border: 1px solid {DANGER}; border-radius: 6px; }}
QFrame#errorBanner QLabel {{ color: #ffb4ae; }}
QFrame#infoBanner {{ background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#card {{ background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 8px; }}

QFrame#playerBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; }}
QLabel#playerTitle {{ font-weight: 600; }}
QLabel#playerArt {{ background: {SURFACE}; border-radius: 4px; color: {TEXT_MUTED};
    font-size: 16pt; }}
QFrame#playerBar QToolButton {{ border-radius: 6px; padding: 3px; }}
QFrame#playerBar QToolButton:hover {{ background: {SURFACE}; }}
QFrame#playerBar QSlider::groove:horizontal {{ height: 4px; background: {BORDER};
    border-radius: 2px; }}
QFrame#playerBar QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QFrame#playerBar QSlider::handle:horizontal {{ background: {TEXT}; width: 12px;
    margin: -4px 0; border-radius: 6px; }}
QFrame#queuePopup {{ background: {BG_ALT}; border: 1px solid {BORDER}; }}
"""


_check_dir: tempfile.TemporaryDirectory[str] | None = None


def _check_icon() -> str:
    """Path of a white check-mark PNG (QSS can only reference image files); '' on failure."""
    global _check_dir
    try:
        if _check_dir is None:
            _check_dir = tempfile.TemporaryDirectory(prefix="sclm-theme-")
        path = Path(_check_dir.name) / "check.png"
        if not path.exists():
            pix = QPixmap(16, 16)
            pix.fill(Qt.GlobalColor.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(TEXT_ON_ACCENT), 2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(3.8, 8.4), QPointF(6.8, 11.4), QPointF(12.2, 4.8)])
            p.end()
            pix.save(str(path))
        return str(path).replace("\\", "/")
    except Exception:
        return ""


def apply_theme(app: QApplication) -> None:
    """Install the Fusion style, the dark palette and the QSS on ``app``."""
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    font = app.font()
    if font.pointSizeF() < 10:
        font.setPointSizeF(10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET.replace("{CHECK}", _check_icon()))


def icon_path() -> Path | None:
    """The application SVG icon, or None.

    Frozen builds (PyInstaller/AppImage) ship it as ``sclm/resources/sclm.svg`` inside the
    bundle (``sys._MEIPASS``); dev / editable installs use ``packaging/sclm.svg`` next to the
    source tree.
    """
    here = Path(__file__).resolve()
    candidates = [here.parents[1] / "resources" / "sclm.svg"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "sclm" / "resources" / "sclm.svg")
    candidates += [base / "packaging" / "sclm.svg" for base in (here.parents[3], Path.cwd())]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def app_icon() -> QIcon:
    """The application icon: the packaged SVG, else the 'sclm' theme icon, else empty."""
    path = icon_path()
    if path is not None:
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return QIcon.fromTheme("sclm")
