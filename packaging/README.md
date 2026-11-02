# Packaging

## Desktop launcher for the venv install

`packaging/install.sh` installs `sclm.desktop` (from `sclm.desktop.in`) and the icon for the
current user. The launcher runs the project's `.venv`.

## Standalone AppImage (no venv, no terminal)

```sh
packaging/build_appimage.sh
# -> dist/SoundCloud_Library_Manager-<version>-x86_64.AppImage
```

The script uses the project `.venv` (it creates the venv from `requirements-dev.txt` if it is
missing, and installs the pinned PyInstaller if that is missing). It then:

1. runs PyInstaller in onedir mode from `sclm.spec`,
2. checks with `ldd` that every bundled library resolves and that `libqxcb.so` resolves
   `libxcb-cursor.so.0` inside the bundle,
3. assembles `build/AppDir` (`appimage/AppRun`, `appimage/sclm.desktop`, `sclm.svg`, and a
   256 px PNG rendered by `render_icon.py`),
4. downloads the pinned `appimagetool` and type2 runtime into `build/tools` and checks their
   SHA-256,
5. builds the AppImage, then runs the offline `--self-test` of the frozen app.

Re-running it rebuilds everything from scratch. The downloaded tools are reused when their
checksum matches.

Requirements: x86_64, `curl`, `sha256sum`, `ldd`, and the X11 helper libraries that get
bundled (`libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0
libxcb-util1 libxkbcommon-x11-0`). Without one of them the spec stops with an apt hint.
`desktop-file-validate` is optional.

To use the app, make the AppImage executable and double-click it. The AppImage needs FUSE
(fusermount or fusermount3). Without FUSE, run it with `--appimage-extract-and-run`.
Settings, library DB and logs live in the same XDG directories as the venv app
(`~/.config`, `~/.local/share`, `~/.local/state` under `soundcloud-library-manager/`), so
both builds share them. `SCLM_HOME` still overrides them.

### What is bundled

- Qt: QtCore, QtGui, QtWidgets and libQt6Svg. Plugins: xcb, offscreen, jpeg, svg, the svg
  icon engine, and the compose/ibus input contexts. Plus the English Qt translations.
- `libxcb-cursor.so.0` and the other X11 helpers, placed next to the Qt libraries.
  `pyi_rth_sclm.py` preloads them.
- yt-dlp, mutagen, certifi, imageio-ffmpeg's static ffmpeg 7.0.2 (used as the fallback after
  the Settings path and `PATH`), and beets (optional). Beets is bundled only when it is
  installed in the venv. Without it, the beets step warns and is skipped.
- Base system libraries (glibc, libstdc++, X11/xcb core, fontconfig/freetype, glib, dbus,
  zlib) are not bundled. They come from the host.

### Frozen-only hooks (in `sclm_launcher.py`, not in `src/`)

- `sclm --self-test [--network] [--download]` checks the frozen environment and exits 0 or
  1. It never opens a window and uses only temporary directories. The checks cover imports,
  beets, the CA bundle, the XDG paths, the child environment, libxcb-cursor, Qt image and
  icon plugins, and ffmpeg (`-version` plus an MP3 encode). `--network` adds an HTTPS GET
  and a real yt-dlp preview. `--download` adds a 10 s test-track download converted with the
  bundled ffmpeg.
- `SCLM_SMOKE_QUIT_AFTER_MS=<n>` quits the event loop n ms after startup. It is meant for
  headless smoke tests, e.g.
  `QT_QPA_PLATFORM=offscreen SCLM_HOME=$(mktemp -d) SCLM_SMOKE_QUIT_AFTER_MS=5000 ./SoundCloud_Library_Manager-*.AppImage`.

### Limitations

- yt-dlp is frozen at the version pinned in `requirements.txt`. SoundCloud changes break
  old yt-dlp versions, so rebuild the AppImage after `pip install -U yt-dlp`. The app
  cannot update yt-dlp itself.
- The AppImage is built against the build machine's glibc (Mint 22.x / Ubuntu 24.04, glibc
  2.39). It will not start on older distributions. Build on the oldest system you want to
  support.
- There is no AppStream metainfo (built with `--no-appstream`) and no update information.
