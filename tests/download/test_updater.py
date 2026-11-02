"""Tests for the yt-dlp self-updater (download/updater.py)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from sclm.core.paths import AppPaths
from sclm.download import updater
from sclm.download.updater import (
    PYPI_JSON_URL,
    UpdateError,
    _version_tuple,
    activate_override,
    override_dir,
    update,
)

BUNDLED = "2026.10.26"


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.under(tmp_path).ensure()


def make_wheel(version: str, *, extra: dict[str, bytes] | None = None,
               broken: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        init = b"raise RuntimeError('broken update')\n" if broken else (
            b"from yt_dlp.version import __version__\n")
        zf.writestr("yt_dlp/__init__.py", init)
        zf.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n".encode())
        zf.writestr("yt_dlp-" + version + ".dist-info/METADATA", b"Name: yt-dlp\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def pypi_fetch(version: str, wheel: bytes, *, sha256: str | None = None):
    url = f"https://files.example/yt_dlp-{version}-py3-none-any.whl"
    meta = {
        "info": {"version": version},
        "urls": [
            {"packagetype": "sdist", "filename": "yt-dlp.tar.gz", "url": "x",
             "digests": {"sha256": "0" * 64}},
            {"packagetype": "bdist_wheel",
             "filename": f"yt_dlp-{version}-py3-none-any.whl", "url": url,
             "digests": {"sha256": sha256 or hashlib.sha256(wheel).hexdigest()}},
        ],
    }
    calls: list[str] = []

    def fetch(u: str) -> bytes:
        calls.append(u)
        if u == PYPI_JSON_URL:
            return json.dumps(meta).encode()
        assert u == url
        return wheel

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.fixture
def clean_modules():
    """Keep sys.path / fake yt_dlp modules from leaking into other tests."""
    path = list(sys.path)
    saved = {k: v for k, v in sys.modules.items()
             if k == "yt_dlp" or k.startswith("yt_dlp.")}
    for k in saved:  # activation refuses to run once yt_dlp is imported, so start clean
        del sys.modules[k]
    yield
    sys.path[:] = path
    for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def purge_yt_dlp() -> None:
    """update() imports yt_dlp to read the current version; activation only runs in a
    process where yt_dlp hasn't been imported yet, so purge it between the two."""
    for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
        del sys.modules[k]


def test_version_tuple_orders_release_dates():
    assert _version_tuple("2026.11.01") > _version_tuple("2026.10.26")
    assert _version_tuple("2026.10.26.232815") > _version_tuple("2026.10.26")
    assert _version_tuple("2026.10.26") == _version_tuple("2026.10.26")
    assert _version_tuple("weird") == (0,)


def test_update_installs_a_newer_wheel(paths):
    wheel = make_wheel("2027.01.01")
    fetch = pypi_fetch("2027.01.01", wheel)
    result = update(paths, fetch=fetch)
    assert result.updated and result.version == "2027.01.01"
    assert result.previous  # the venv's real yt-dlp
    assert (override_dir(paths) / "yt_dlp" / "version.py").exists()
    # only the package is extracted, not dist-info
    assert not list(override_dir(paths).glob("*.dist-info"))


def test_update_is_a_no_op_when_already_latest(paths):
    current = updater.installed_version()
    fetch = pypi_fetch(current, make_wheel(current))
    result = update(paths, fetch=fetch)
    assert not result.updated and result.version == current
    assert not override_dir(paths).exists()
    assert fetch.calls == [PYPI_JSON_URL]  # no wheel download


def test_update_skips_download_when_already_pending(paths):
    wheel = make_wheel("2027.01.01")
    assert update(paths, fetch=pypi_fetch("2027.01.01", wheel)).updated
    fetch = pypi_fetch("2027.01.01", wheel)
    result = update(paths, fetch=fetch)
    assert result.updated and result.version == "2027.01.01"
    assert fetch.calls == [PYPI_JSON_URL]


def test_sha_mismatch_installs_nothing(paths):
    wheel = make_wheel("2027.01.01")
    fetch = pypi_fetch("2027.01.01", wheel, sha256="f" * 64)
    with pytest.raises(UpdateError, match="verification"):
        update(paths, fetch=fetch)
    assert not override_dir(paths).exists()
    assert not list(override_dir(paths).parent.glob(".yt-dlp-override.*"))


def test_wheel_without_the_package_is_rejected(paths):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("not_yt_dlp/__init__.py", b"")
    fetch = pypi_fetch("2027.01.01", buf.getvalue())
    with pytest.raises(UpdateError):
        update(paths, fetch=fetch)
    assert not override_dir(paths).exists()


def test_version_mismatch_inside_wheel_is_rejected(paths):
    fetch = pypi_fetch("2027.01.01", make_wheel("2026.12.31"))
    with pytest.raises(UpdateError, match="expected"):
        update(paths, fetch=fetch)
    assert not override_dir(paths).exists()


def test_install_replaces_an_older_pending_update(paths):
    assert update(paths, fetch=pypi_fetch("2027.01.01", make_wheel("2027.01.01"))).updated
    assert update(paths, fetch=pypi_fetch("2027.02.02", make_wheel("2027.02.02"))).updated
    got = (override_dir(paths) / "yt_dlp" / "version.py").read_text()
    assert "2027.02.02" in got
    assert not (override_dir(paths).parent / ".yt-dlp-override.old").exists()


def test_network_failure_wraps_into_update_error(paths):
    def fetch(url: str) -> bytes:
        raise OSError("network down")

    with pytest.raises(UpdateError):
        update(paths, fetch=fetch)


def test_activate_prefers_a_newer_override(paths, clean_modules):
    update(paths, fetch=pypi_fetch("2027.01.01", make_wheel("2027.01.01")))
    purge_yt_dlp()
    got = activate_override(paths, bundled_version=BUNDLED)
    assert got == "2027.01.01"
    assert sys.path[0] == str(override_dir(paths))
    import yt_dlp.version

    assert yt_dlp.version.__version__ == "2027.01.01"


def test_activate_ignores_an_older_or_equal_override(paths, clean_modules):
    update(paths, fetch=pypi_fetch("2027.01.01", make_wheel("2027.01.01")))
    purge_yt_dlp()
    assert activate_override(paths, bundled_version="2027.01.01") is None
    assert activate_override(paths, bundled_version="2027.06.06") is None
    assert str(override_dir(paths)) not in sys.path


def test_activate_without_any_override_is_none(paths):
    assert activate_override(paths, bundled_version=BUNDLED) is None


def test_activate_falls_back_when_the_override_is_broken(paths, clean_modules):
    fetch = pypi_fetch("2027.01.01", make_wheel("2027.01.01", broken=True))
    update(paths, fetch=fetch)
    purge_yt_dlp()
    assert activate_override(paths, bundled_version=BUNDLED) is None
    assert str(override_dir(paths)) not in sys.path
    assert "yt_dlp" not in sys.modules  # the broken probe import was cleaned up


def test_real_bundled_version_is_detected():
    vfile = updater.bundled_version_file()
    assert vfile is not None and vfile.name == "version.py"
    assert updater._module_version(vfile) == updater.installed_version()


def test_activate_refuses_when_yt_dlp_is_already_imported(paths, clean_modules):
    update(paths, fetch=pypi_fetch("2027.01.01", make_wheel("2027.01.01")))
    purge_yt_dlp()
    sys.modules["yt_dlp"] = type(sys)("yt_dlp")
    try:
        assert activate_override(paths, bundled_version=BUNDLED) is None
        assert str(override_dir(paths)) not in sys.path
    finally:
        del sys.modules["yt_dlp"]
