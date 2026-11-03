"""Self-test of the frozen build: ``sclm --self-test [--network] [--download]``.

Packaging-only (bundled by packaging/sclm.spec, reached through packaging/sclm_launcher.py).
Never opens a window (forces QT_QPA_PLATFORM=offscreen) and never touches the user's real
data: everything runs in a temporary directory that is removed afterwards.

Checks:
  imports        sclm modules, PySide6, yt-dlp, mutagen, imageio-ffmpeg, certifi, sqlite3/ssl
  beets          optional: adds the test MP3 to a temp beets library; WARN, never FAIL
  tls-ca         certifi bundle present; OpenSSL default paths
  xdg-paths      where the real app would keep settings/DB/logs (computed, not touched)
  child-env      LD_LIBRARY_PATH / QT_PLUGIN_PATH of the bundle do not leak to subprocesses
  xcb-cursor     the bundled libxcb-cursor.so.0 is the one loaded for the Qt xcb plugin, and
                 sclm.core.platform_check sees it as loadable
  qt             offscreen QApplication, theme, app icon (SVG), JPEG/PNG/SVG image plugins
  ffmpeg         bundled imageio-ffmpeg binary runs (-version) and encodes MP3 (libmp3lame);
                 also reports what the default resolver picks (settings -> PATH -> bundled)
  multimedia     QtMultimedia's FFmpeg backend loads and decodes MP3 (local player)
  webkit-player  the website-player helper is bundled; WARN if the host lacks WebKitGTK,
                 PyGObject or the GStreamer AAC decoder (Listen page then offers the browser)
  --network      HTTPS GET via urllib, and a real yt-dlp preview through sclm.download.preview
  --download     yt-dlp download of a ~10 s public test track + MP3 conversion with the
                 bundled ffmpeg, read back with mutagen
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Public test material used by yt-dlp's own test suite (also used by tests/download/test_network.py).
TEST_TRACK_URL = "https://soundcloud.com/jaimemf/youtube-dl-test-video-a-y-baw/s-8Pjrp"

_results: list[tuple[str, str, str]] = []


def _check(name: str, fn, *, optional: bool = False) -> object:
    t0 = time.monotonic()
    try:
        detail = fn()
        status = "PASS"
    except Exception as exc:  # report every failure, keep going
        detail = f"{type(exc).__name__}: {exc}"
        status = "WARN" if optional else "FAIL"
        if os.environ.get("SCLM_SELFTEST_TRACEBACK"):
            traceback.print_exc()
    _results.append((status, name, f"{detail}  [{time.monotonic() - t0:.1f}s]"))
    return detail


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _imports() -> str:
    import sqlite3
    import ssl

    import certifi
    import imageio_ffmpeg
    import mutagen
    import PySide6
    import yt_dlp
    from yt_dlp.version import __version__ as ytdlp_version

    import sclm
    import sclm.app  # noqa: F401
    import sclm.download.manager  # noqa: F401
    import sclm.gui.main_window  # noqa: F401
    import sclm.metadata.pipeline  # noqa: F401

    return (f"python {sys.version.split()[0]}, sclm {sclm.__version__}, PySide6 "
            f"{PySide6.__version__}, yt-dlp {ytdlp_version}, mutagen {mutagen.version_string}, "
            f"imageio-ffmpeg {imageio_ffmpeg.__version__}, certifi {certifi.__version__}, "
            f"sqlite {sqlite3.sqlite_version}, {ssl.OPENSSL_VERSION}; yt_dlp at "
            f"{Path(yt_dlp.__file__).parent}")


def _beets(tmp: Path) -> str:
    from sclm.metadata import beets_bridge

    if not beets_bridge.is_available():
        raise RuntimeError("beets is not bundled (optional; SCLM warns and skips the beets step)")
    import beets

    mp3 = tmp / "sine.mp3"  # written by the ffmpeg check
    if not mp3.is_file():
        raise RuntimeError("no test MP3 (ffmpeg check failed)")
    os.environ["BEETSDIR"] = str(tmp / "beets")  # never read/write the user's beets config
    added = beets_bridge.import_file(mp3, library_db=tmp / "beets" / "library.db")
    return f"beets {beets.__version__}: import_file -> {added} (temp library)"


def _tls_ca() -> str:
    import ssl

    import certifi

    ca = Path(certifi.where())
    if not ca.is_file() or ca.stat().st_size < 10_000:
        raise RuntimeError(f"certifi bundle missing or empty: {ca}")
    paths = ssl.get_default_verify_paths()
    return (f"certifi {ca} ({ca.stat().st_size} B); SSL_CERT_FILE="
            f"{os.environ.get('SSL_CERT_FILE', '(unset)')}; openssl cafile={paths.cafile}")


def _xdg_paths(env: dict[str, str]) -> str:
    from sclm.core.paths import AppPaths

    paths = AppPaths.from_env(env)  # computed only; nothing is created or opened
    return (f"config={paths.config_dir} data={paths.data_dir} state={paths.state_dir} "
            f"(same resolver as the venv app; SCLM_HOME={'set' if env.get('SCLM_HOME') else 'unset'})")


def _child_env() -> str:
    bundle = str(_bundle_dir())
    leaks = {var: os.environ[var] for var in ("LD_LIBRARY_PATH", "QT_PLUGIN_PATH",
                                              "QML2_IMPORT_PATH")
             if bundle in os.environ.get(var, "")}
    if leaks:
        raise RuntimeError(f"bundle paths leak into the child environment: {leaks}")
    return "no LD_LIBRARY_PATH/QT_PLUGIN_PATH pointing into the bundle"


def _loaded_path(soname: str) -> str | None:
    with open("/proc/self/maps", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            path = line.rstrip("\n").split(maxsplit=5)[-1]
            if Path(path).name.startswith(soname):
                return path
    return None


def _xcb_cursor() -> str:
    import ctypes

    from sclm.core import platform_check

    plugin = _bundle_dir() / "PySide6" / "Qt" / "plugins" / "platforms" / "libqxcb.so"
    if not plugin.is_file():
        raise RuntimeError(f"xcb platform plugin not bundled: {plugin}")
    # Same lookup as platform_check's fallback branch (by soname, no ldconfig involved).
    ctypes.CDLL("libxcb-cursor.so.0")
    ctypes.CDLL(str(plugin))  # resolves every dependency of the plugin, no X connection
    loaded = _loaded_path("libxcb-cursor.so.0")
    if loaded is None:
        raise RuntimeError("libxcb-cursor.so.0 not loaded after loading libqxcb.so")
    bundle = str(_bundle_dir().resolve())
    if not os.path.realpath(loaded).startswith(bundle):
        raise RuntimeError(f"libxcb-cursor.so.0 resolved OUTSIDE the bundle: {loaded}")
    if not platform_check._default_loadable():
        raise RuntimeError("platform_check._default_loadable() is False")
    return f"libqxcb.so loads; libxcb-cursor.so.0 -> {loaded}; platform_check sees it"


def _qt() -> str:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage, QImageReader
    from PySide6.QtWidgets import QApplication

    from sclm.gui import theme

    app = QApplication.instance() or QApplication(["sclm"])
    theme.apply_theme(app)
    formats = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
    missing = {"jpeg", "jpg", "png", "svg"} - formats
    if missing:
        raise RuntimeError(f"image formats missing: {sorted(missing)} (have {sorted(formats)})")
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor("#ff5500"))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "JPG"):
        raise RuntimeError("JPEG encode failed")
    back = QImage()
    if not back.loadFromData(data, "JPG") or back.width() != 64:
        raise RuntimeError("JPEG decode failed")
    path = theme.icon_path()
    icon = theme.app_icon()
    if path is None or icon.isNull() or icon.pixmap(64, 64).isNull():
        raise RuntimeError(f"app icon not found/renderable (icon_path={path})")
    return (f"platform={app.platformName()}, style={app.style().metaObject().className()}, icon={path}, "
            f"formats={','.join(sorted(formats))}")


def _ffmpeg(tmp: Path) -> str:
    import subprocess

    from mutagen.mp3 import MP3

    from sclm.core.ffmpeg import FfmpegSource, ffmpeg_version, resolve_ffmpeg

    bundled = resolve_ffmpeg(None, which=lambda _name: None)
    if bundled.source is not FfmpegSource.BUNDLED:
        raise RuntimeError(f"expected the bundled ffmpeg, got {bundled}")
    version = ffmpeg_version(bundled.path)
    if not version:
        raise RuntimeError(f"{bundled.path} -version failed")
    out = tmp / "sine.mp3"
    proc = subprocess.run(
        [str(bundled.path), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
        capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"MP3 encode failed: {proc.stderr.strip()[:300]}")
    length = MP3(out).info.length
    default = resolve_ffmpeg(None)
    return (f"bundled: {bundled.path} ({version}); MP3 encode ok ({length:.2f}s); "
            f"default resolver picks {default.source}: {default.path}")


def _multimedia(tmp: Path) -> str:
    """Decode a generated MP3 with QtMultimedia's FFmpeg backend (no audio device needed)."""
    import subprocess

    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder
    from PySide6.QtWidgets import QApplication

    from sclm.core.ffmpeg import resolve_ffmpeg

    app = QApplication.instance() or QApplication(["sclm-selftest"])
    mp3 = tmp / "decode.mp3"
    subprocess.run([str(resolve_ffmpeg(None).path), "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(mp3)],
                   check=True, timeout=30)
    decoder = QAudioDecoder()
    frames, errors = [0], []
    loop = QEventLoop()

    def ready() -> None:
        frames[0] += decoder.read().frameCount()

    decoder.bufferReady.connect(ready)
    decoder.finished.connect(loop.quit)
    decoder.error.connect(lambda *_: (errors.append(decoder.errorString()), loop.quit()))
    decoder.setSource(QUrl.fromLocalFile(str(mp3)))
    decoder.start()
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    del app
    if errors or frames[0] < 40000:
        raise RuntimeError(f"MP3 decode failed: {errors or frames[0]} frames")
    return f"QtMultimedia (FFmpeg backend) decoded a 1 s MP3: {frames[0]} frames"


def _webkit_player() -> str:
    import subprocess

    from sclm.gui.listening_browser import HELPER, system_python

    if not HELPER.is_file():
        raise FileNotFoundError(HELPER)
    python = system_python()
    if python is None:
        raise RuntimeError("no system python3")
    probe = ("import gi; gi.require_version('Gtk','3.0'); gi.require_version('WebKit2','4.1');"
             "gi.require_version('Gst','1.0'); from gi.repository import WebKit2, Gst; "
             "Gst.init(None); assert Gst.ElementFactory.find('avdec_aac') or "
             "Gst.ElementFactory.find('fdkaacdec'), 'no GStreamer AAC decoder'; "
             "print(WebKit2.get_major_version(), WebKit2.get_minor_version(), "
             "WebKit2.get_micro_version())")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "QT_"))}
    out = subprocess.run([python, "-I", "-c", probe], capture_output=True, text=True,
                         timeout=30, env=env)
    if out.returncode:
        raise RuntimeError((out.stderr.strip().splitlines() or ["failed"])[-1])
    return f"helper {HELPER.name}; host {python}: WebKitGTK {'.'.join(out.stdout.split())}"


def _https() -> str:
    import urllib.request

    req = urllib.request.Request("https://soundcloud.com/", headers={"User-Agent": "sclm-selftest"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # default context: verifies certs
        return f"urllib HTTPS GET soundcloud.com -> {resp.status}"


def _preview(tmp: Path) -> str:
    from sclm.core.paths import AppPaths
    from sclm.core.settings import Settings
    from sclm.core.storage import Storage
    from sclm.download.preview import fetch_preview

    paths = AppPaths.under(tmp / "home").ensure()
    storage = Storage(paths.db_file)
    try:
        res = fetch_preview(TEST_TRACK_URL, Settings(), storage, paths=paths)
    finally:
        storage.close()
    [entry] = res.entries
    t = entry.track
    return f"yt-dlp preview ok: id={t.track_id} {t.artist!r} - {t.title!r} ({t.duration_s}s)"


def _download(tmp: Path) -> str:
    import yt_dlp
    from mutagen.mp3 import MP3

    from sclm.core.ffmpeg import resolve_ffmpeg

    ffmpeg = resolve_ffmpeg(None, which=lambda _name: None)  # force the bundled binary
    outdir = tmp / "dl"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "ffmpeg_location": str(ffmpeg.path),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "192"}],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([TEST_TRACK_URL])
    mp3s = list(outdir.glob("*.mp3"))
    if not mp3s:
        raise RuntimeError(f"no MP3 produced in {outdir}: {list(outdir.iterdir())}")
    info = MP3(mp3s[0]).info
    return (f"downloaded + converted with bundled ffmpeg: {mp3s[0].name}, "
            f"{info.length:.1f}s, {info.bitrate // 1000} kbps")


def run(*, network: bool = False, download: bool = False) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    print(f"sclm self-test (frozen={getattr(sys, 'frozen', False)}, bundle={_bundle_dir()})")
    user_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="sclm-selftest-") as tmpname:
        tmp = Path(tmpname)
        _check("xdg-paths", lambda: _xdg_paths(user_env))
        os.environ["SCLM_HOME"] = str(tmp / "home")  # never touch the real XDG dirs
        _check("imports", _imports)
        _check("tls-ca", _tls_ca)
        _check("child-env", _child_env)
        _check("xcb-cursor", _xcb_cursor)
        _check("qt", _qt)
        _check("ffmpeg", lambda: _ffmpeg(tmp))
        _check("multimedia", lambda: _multimedia(tmp))
        _check("webkit-player (host)", _webkit_player, optional=True)
        _check("beets (optional)", lambda: _beets(tmp), optional=True)
        if network or download:
            _check("network-https", _https)
            _check("network-preview", lambda: _preview(tmp))
        if download:
            _check("network-download", lambda: _download(tmp))
    width = max(len(name) for _, name, _ in _results)
    for status, name, detail in _results:
        print(f"[{status}] {name:<{width}}  {detail}")
    failed = [name for status, name, _ in _results if status == "FAIL"]
    print("SELF-TEST " + ("FAILED: " + ", ".join(failed) if failed else "PASSED"))
    sys.stdout.flush()
    return 1 if failed else 0
