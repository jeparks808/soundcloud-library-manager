from __future__ import annotations

import threading

from sclm.core.errors import ErrorCategory, InvalidUrlError
from sclm.gui.bridge import NOT_AVAILABLE_MESSAGE, run_in_background


def test_runs_off_gui_thread_and_delivers_on_gui_thread(qtbot):
    seen: dict[str, str] = {}
    results = []

    def work():
        seen["worker"] = threading.current_thread().name
        return 42

    def done(value):
        seen["callback"] = threading.current_thread().name
        results.append(value)

    handle = run_in_background(work, done, lambda e: results.append(e))
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [42]
    assert seen["worker"] != threading.main_thread().name
    assert seen["callback"] == threading.main_thread().name
    assert handle.finished


def test_exception_becomes_error_info(qtbot):
    errors = []

    def boom():
        raise InvalidUrlError()

    run_in_background(boom, lambda r: errors.append(("ok", r)), errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=3000)
    assert errors[0].category == ErrorCategory.INVALID_URL


def test_not_implemented_is_reported_not_raised(qtbot):
    errors = []

    def stub():
        raise NotImplementedError

    run_in_background(stub, lambda r: None, errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=3000)
    assert errors[0].user_message == NOT_AVAILABLE_MESSAGE


def test_discard_suppresses_callbacks(qtbot):
    got = []
    gate = threading.Event()

    def work():
        gate.wait(2)
        return 1

    handle = run_in_background(work, got.append, got.append)
    handle.discard()
    gate.set()
    qtbot.waitUntil(lambda: handle.finished, timeout=3000)
    assert got == []
