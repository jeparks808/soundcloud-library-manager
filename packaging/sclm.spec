# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SoundCloud Library Manager (onedir, Linux x86_64).

Build through packaging/build_appimage.sh (which also assembles the AppImage), or directly:
    .venv/bin/pyinstaller --noconfirm --clean --distpath build/pyinstaller/dist \
        --workpath build/pyinstaller/work packaging/sclm.spec

Lean Qt: only QtCore/QtGui/QtWidgets (+ libQt6Svg for the SVG icon engine / image plugin),
the xcb + offscreen platform plugins, JPEG/SVG image plugins, the SVG icon engine and the
compose/ibus input-context plugins. PNG support is built into QtGui.
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "packaging"

# --- X11 helper libraries Qt's xcb plugin needs but the PySide6 wheel does not ship ---------
# libxcb-cursor0 is missing on a default Mint install; the rest are tiny and bundled for
# robustness. They go next to the Qt libs (RUNPATH $ORIGIN of libQt6XcbQpa) and are preloaded
# by packaging/pyi_rth_sclm.py.
XCB_HELPER_LIBS = [
    "libxcb-cursor.so.0",
    "libxcb-icccm.so.4",
    "libxcb-image.so.0",
    "libxcb-keysyms.so.1",
    "libxcb-render-util.so.0",
    "libxcb-util.so.1",
    "libxkbcommon-x11.so.0",
]
LIB_DIRS = [Path("/usr/lib/x86_64-linux-gnu"), Path("/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"), Path("/usr/lib")]


def _system_lib(name):
    for d in LIB_DIRS:
        if (d / name).exists():
            return str((d / name).resolve())
    raise SystemExit(f"sclm.spec: {name} not found on the build machine. "
                     "Install it, e.g.: sudo apt install libxcb-cursor0 libxcb-icccm4 "
                     "libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1 "
                     "libxkbcommon-x11-0")


# --- Python-level excludes ----------------------------------------------------------------
QT_KEEP = {"QtCore", "QtGui", "QtWidgets", "QtSvg"}
QT_ALL = [
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
    "QtAxContainer", "QtBluetooth", "QtCanvasPainter", "QtCharts", "QtConcurrent", "QtDBus",
    "QtDataVisualization", "QtDesigner", "QtGraphs", "QtGraphsWidgets", "QtHelp",
    "QtHttpServer", "QtLocation", "QtMultimedia", "QtMultimediaWidgets", "QtNetwork",
    "QtNetworkAuth", "QtNfc", "QtOpenGL", "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets",
    "QtPositioning", "QtPrintSupport", "QtQml", "QtQuick", "QtQuick3D", "QtQuickControls2",
    "QtQuickTest", "QtQuickWidgets", "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus",
    "QtSerialPort", "QtSpatialAudio", "QtSql", "QtStateMachine", "QtSvgWidgets", "QtTest",
    "QtTextToSpeech", "QtUiTools", "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick",
    "QtWebEngineWidgets", "QtWebSockets", "QtWebView", "QtXml",
]
EXCLUDES = [f"PySide6.{m}" for m in QT_ALL if m not in QT_KEEP] + [
    "tkinter", "_tkinter", "turtle", "idlelib", "lib2to3", "pydoc_data",
    "numpy", "lap", "scipy", "matplotlib", "PIL", "IPython",
    "pytest", "_pytest", "pytestqt", "pluggy", "iniconfig", "pygments",
    "beetsplug",  # beets plugins: SCLM's bridge only uses beets.library
]

# --- Optional beets: bundled when installed in the build venv (pure Python, ~5 MB) ----------
DATAS = [(str(PKG / "sclm.svg"), "sclm/resources")]
HIDDEN = ["sclm_selftest"]
try:
    import beets  # noqa: F401
except ImportError:
    print("sclm.spec: beets not installed; building without the optional beets step")
else:
    from PyInstaller.utils.hooks import collect_data_files, copy_metadata

    DATAS += collect_data_files("beets")  # config_default.yaml etc.
    DATAS += copy_metadata("beets")
    HIDDEN += ["beets", "beets.library", "beets.dbcore.query"]

a = Analysis(
    [str(PKG / "sclm_launcher.py")],
    pathex=[str(ROOT / "src"), str(PKG)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PKG / "pyi_rth_sclm.py")],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# --- Trim Qt plugins, translations and unused Qt libraries ----------------------------------
QT_PREFIX = os.path.join("PySide6", "Qt")
PLUGINS_KEEP = {
    "platforms/libqxcb.so",
    "platforms/libqoffscreen.so",        # headless runs / smoke tests
    "imageformats/libqjpeg.so",
    "imageformats/libqsvg.so",
    "iconengines/libqsvgicon.so",
    "platforminputcontexts/libcomposeplatforminputcontextplugin.so",  # dead keys / compose
    "platforminputcontexts/libibusplatforminputcontextplugin.so",     # IBus input methods
}
TRANSLATIONS_KEEP = {"qt_en.qm", "qtbase_en.qm"}
# Base-system libraries taken from the host instead of the bundle (AppImage "excludelist"
# practice): every X11 desktop has them, and an older bundled copy (e.g. libstdc++,
# fontconfig) can break newer host libraries loaded into the same process (GL drivers,
# input methods, font configs).
HOST_LIB_PATTERNS = (
    "libstdc++.so", "libgcc_s.so", "libz.so", "libexpat.so",
    "libX11.so", "libX11-xcb.so", "libxcb.so.", "libXau.so", "libXdmcp.so",
    "libfontconfig.so", "libfreetype.so", "libharfbuzz.so", "libdbus-1.so",
    "libglib-2.0.so", "libgobject-2.0.so", "libgio-2.0.so", "libgmodule-2.0.so",
    "libgthread-2.0.so",
)


def _keep(dest):
    dest = dest.replace(os.sep, "/")
    if dest.startswith("PySide6/Qt/plugins/"):
        return dest[len("PySide6/Qt/plugins/"):] in PLUGINS_KEEP
    if dest.startswith("PySide6/Qt/translations/"):
        return os.path.basename(dest) in TRANSLATIONS_KEEP
    if dest.startswith(("PySide6/Qt/qml/", "PySide6/Qt/resources/", "PySide6/Qt/libexec/",
                        "PySide6/Qt/metatypes/")):
        return False
    return not os.path.basename(dest).startswith(HOST_LIB_PATTERNS)


def _prune_unreachable(binaries):
    """Drop shared libraries that no kept extension module / plugin / helper lib needs.

    PyInstaller collected the dependencies of *every* Qt plugin (GTK3 for the gtk3 platform
    theme, Wayland, EGLFS, ...). After the plugin filter, walk DT_NEEDED from the roots and
    keep only what is reachable (plus symlinks to kept files).
    """
    from PyInstaller.depend.bindepend import get_imports

    by_name = {}
    for entry in binaries:
        by_name.setdefault(os.path.basename(entry[0]), []).append(entry)

    def is_root(entry):
        dest, _src, typecode = entry
        name = os.path.basename(dest)
        return (".so" not in name  # executables, e.g. imageio-ffmpeg's ffmpeg binary
                or typecode == "EXTENSION" or dest.replace(os.sep, "/").startswith("PySide6/Qt/plugins/")
                or name.startswith("libpython") or name in XCB_HELPER_LIBS)

    keep = set()
    todo = [e for e in binaries if is_root(e) and e[2] != "SYMLINK"]
    while todo:
        entry = todo.pop()
        if entry in keep:
            continue
        keep.add(entry)
        for name, _path in get_imports(entry[1]):
            for dep in by_name.get(os.path.basename(name), []):
                if dep not in keep:
                    if dep[2] == "SYMLINK":  # follow to the real file
                        keep.add(dep)
                        dep = next((e for e in binaries if e[0] == dep[1]), None)
                        if dep is None:
                            continue
                    todo.append(dep)
    kept_files = {e[0] for e in keep if e[2] != "SYMLINK"}
    result = [e for e in binaries
              if (e[2] != "SYMLINK" and e in keep) or (e[2] == "SYMLINK" and e[1] in kept_files)]
    dropped = sorted(e[0] for e in binaries if e not in result)
    print(f"sclm.spec: pruned {len(dropped)} unreachable libraries: {', '.join(dropped)}")
    return result


a.binaries = [entry for entry in a.binaries if _keep(entry[0])]
a.datas = [entry for entry in a.datas if _keep(entry[0])]
# Bundled X11 helpers live next to the Qt libs; replace PyInstaller's top-level copies with
# symlinks (same layout PyInstaller uses for the Qt libs themselves).
a.binaries = [e for e in a.binaries if os.path.basename(e[0]) not in XCB_HELPER_LIBS]
for _name in XCB_HELPER_LIBS:
    a.binaries.append((f"{QT_PREFIX}/lib/{_name}", _system_lib(_name), "BINARY"))
    a.binaries.append((_name, f"{QT_PREFIX}/lib/{_name}", "SYMLINK"))
a.binaries = _prune_unreachable(a.binaries)
# PyInstaller also records top-level symlinks in the datas TOC; drop the dangling ones.
_files = {e[0] for e in a.binaries + a.datas if e[2] != "SYMLINK"}
a.datas = [e for e in a.datas if e[2] != "SYMLINK" or e[1] in _files]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sclm",           # argv[0] basename -> X11 WM_CLASS "sclm" (desktop StartupWMClass)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sclm",
)
