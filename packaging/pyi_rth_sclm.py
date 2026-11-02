"""PyInstaller runtime hook for the frozen SCLM build (runs before the entry script).

1. Preload the X11 helper libraries that packaging/sclm.spec bundles next to the Qt libs
   (libxcb-cursor.so.0 & co.). Once loaded, every later lookup by soname reuses them: Qt's
   xcb plugin and ``sclm.core.platform_check`` (``ctypes.CDLL("libxcb-cursor.so.0")``)
   both find them on systems without ``libxcb-cursor0`` installed, independent of the
   LD_LIBRARY_PATH that the PyInstaller bootloader sets (and that sclm_launcher.py removes
   from the environment again so child processes such as ffmpeg/xdg-open get a clean one).
2. TLS fallback: if neither SSL_CERT_FILE nor the system CA file exists, use certifi's
   bundle for the stdlib ``ssl`` default context (yt-dlp uses certifi on its own anyway).
"""


def _sclm_rthook() -> None:
    import ctypes
    import os
    import sys

    qt_lib = os.path.join(sys._MEIPASS, "PySide6", "Qt", "lib")
    # Dependency order: each library's bundled deps come first.
    for name in ("libxcb-util.so.1", "libxcb-render-util.so.0", "libxcb-image.so.0",
                 "libxcb-icccm.so.4", "libxcb-keysyms.so.1", "libxkbcommon-x11.so.0",
                 "libxcb-cursor.so.0"):
        path = os.path.join(qt_lib, name)
        if os.path.isfile(path):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:  # never fatal: the app's own pre-flight check explains it
                print(f"sclm: could not preload {name}: {exc}", file=sys.stderr)

    if not os.environ.get("SSL_CERT_FILE"):
        try:
            import ssl

            if not os.path.isfile(ssl.get_default_verify_paths().openssl_cafile or ""):
                import certifi

                os.environ["SSL_CERT_FILE"] = certifi.where()
        except Exception:
            pass


_sclm_rthook()
del _sclm_rthook
