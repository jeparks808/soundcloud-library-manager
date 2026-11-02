from __future__ import annotations

from PySide6.QtGui import QPalette

from sclm.gui import theme


def test_theme_applies(qapp):
    theme.apply_theme(qapp)
    assert qapp.palette().color(QPalette.ColorRole.Highlight).name() == theme.ACCENT
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() < 60
    assert theme.ACCENT in qapp.styleSheet()


def test_apply_theme_is_idempotent(qapp):
    theme.apply_theme(qapp)
    sheet = qapp.styleSheet()
    theme.apply_theme(qapp)
    assert qapp.styleSheet() == sheet


def test_every_status_has_a_colour():
    from sclm.core.models import JobStatus
    assert set(theme.STATUS_COLORS) == set(JobStatus)


def test_app_icon_from_packaging_svg():
    assert theme.icon_path() is not None
    assert not theme.app_icon().isNull()
