# SoundCloud Library Manager

Linux desktop application for authorized SoundCloud downloads, MP3 conversion,
metadata/artwork, playlists, duplicate detection and library organization.
Built with Python 3.12+, PySide6, yt-dlp, FFmpeg, Mutagen and SQLite; Beets is optional.

## Which implementation is this?

This is the source from `/home/john/soundcloud-library-manager` (the original
non-Codex project). Its built AppImage exactly matches the desktop file
`/home/john/Desktop/SoundCloud_Library_Manager.AppImage`:

- Source commit: `2177b26c79486c0e10261484c01d8d17d5918adb`
- AppImage SHA-256: `2556587c3ba1045305eb4dc9afd0855a147ec7f701bb4b6959b8d261bec7197e`
- Original history: eight commits, preserved with original hashes.

The independently developed Codex implementation is separate and is not included.
In particular, the embedded SoundCloud listening-tab prototype was built in that
separate project; it has not been ported into this source or the desktop AppImage.
The existing desktop AppImage was not changed by this repository import.

## Develop on another machine

```sh
git clone https://github.com/jeparks808/soundcloud-library-manager.git
cd soundcloud-library-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e .
.venv/bin/python -m sclm
```

Use Linux Mint 22.x / Ubuntu 24.04 or a compatible x86_64 Linux desktop. Python
3.12+ is required. Install FFmpeg and the Qt/X11 system dependencies listed in
[packaging/README.md](packaging/README.md). Download only audio you are authorized
to download. The app does not bypass DRM or SoundCloud access restrictions.

## Validate and build

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
packaging/build_appimage.sh
```

The AppImage build script uses pinned packaging tools and checksum verification.
See [packaging instructions](packaging/README.md) for dependencies and offline
self-tests. Builds are generated locally; music, private settings, browser
profiles, databases and virtualenvs are not repository content.

Historical test results and known limitations are recorded in
[DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) and
[REQUIREMENTS_CHECKLIST.md](REQUIREMENTS_CHECKLIST.md). They describe checks run by
the original implementation sessions, not new tests performed during this import.
The import verified the desktop/built AppImage hashes, clean working tree, source
file manifest and full Git bundle history. It did not rebuild the AppImage.

## Track changes

Create a feature branch before changing code, commit focused changes with their
validation, push the branch and open a pull request. Keep the build scripts and
requirements updated along with application changes.

Read [architecture](docs/ARCHITECTURE.md), [roadmap](docs/ROADMAP.md), and
[project instructions](CLAUDE.md) before implementation. Agent definitions and
model verification limits are retained as historical project documentation.
