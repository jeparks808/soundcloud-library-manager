from __future__ import annotations

import io
import subprocess
from unittest import mock

from sclm.core import platform_check as pc

XCB_ENV = {"DISPLAY": ":0"}


def _run(env, present):
    notes: list[str] = []
    err = io.StringIO()
    ok = pc.check_xcb_cursor(env, loadable=lambda: present, notify=notes.append, stderr=err)
    return ok, notes, err.getvalue()


def test_library_present_proceeds():
    ok, notes, err = _run(XCB_ENV, True)
    assert ok and not notes and not err


def test_missing_on_xcb_prints_message_and_notifies():
    ok, notes, err = _run(XCB_ENV, False)
    assert not ok
    assert "sudo apt install libxcb-cursor0" in err
    assert notes == [pc.MESSAGE]


def test_skipped_for_offscreen_wayland_and_no_display():
    for env in ({"QT_QPA_PLATFORM": "offscreen", "DISPLAY": ":0"},
                {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
                {"QT_QPA_PLATFORM": "wayland", "DISPLAY": ":0"}, {}):
        ok, notes, err = _run(env, False)
        assert ok and not notes and not err


def test_explicit_xcb_is_checked():
    assert _run({"QT_QPA_PLATFORM": "xcb", "DISPLAY": ":0"}, False)[0] is False


def test_check_error_falls_through():
    def boom():
        raise RuntimeError("x")
    assert pc.check_xcb_cursor(XCB_ENV, loadable=boom, notify=lambda m: None) is True


def test_zenity_called_with_expected_args():
    with mock.patch.object(pc.shutil, "which", side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None), \
            mock.patch.object(pc.subprocess, "run") as run:
        pc._default_notify(pc.MESSAGE)
    args, kwargs = run.call_args
    assert args[0][:2] == ["zenity", "--error"]
    assert f"--text={pc.MESSAGE}" in args[0]
    assert kwargs["timeout"] and not kwargs.get("shell")


def test_notify_send_fallback_and_failure_ignored():
    with mock.patch.object(pc.shutil, "which", side_effect=lambda n: "/x" if n == "notify-send" else None), \
            mock.patch.object(pc.subprocess, "run", side_effect=subprocess.TimeoutExpired("n", 1)) as run:
        pc._default_notify("m")
    assert run.call_args[0][0][0] == "notify-send"


def test_no_tools_available_is_silent():
    with mock.patch.object(pc.shutil, "which", return_value=None), \
            mock.patch.object(pc.subprocess, "run") as run:
        pc._default_notify("m")
    run.assert_not_called()
