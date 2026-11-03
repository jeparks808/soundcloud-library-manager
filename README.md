# SoundCloud Library Manager

Linux desktop application for authorized SoundCloud downloads, MP3 conversion,
metadata/artwork, playlists, duplicate detection and library organization — plus a
built-in player for your downloaded music and the SoundCloud website for listening.
Built with Python 3.12+, PySide6 (Qt 6.12), yt-dlp, FFmpeg, Mutagen and SQLite; Beets is
optional.

Download only audio you are authorized to download. The app does not bypass DRM or
SoundCloud access restrictions; unavailable or restricted tracks are reported as errors.

## Features (0.3.x)

| Area | Status |
|---|---|
| Download tracks/playlists (yt-dlp), MP3 conversion (FFmpeg), ID3 tags + artwork (Mutagen), organization, duplicates, history | Working (unchanged from 0.1/0.2) |
| **Local player**: play/pause/stop, previous/next, seek bar, elapsed/total time, volume + mute, title/artist, cover art, play queue, double-click to play, auto-advance, missing/unsupported files reported and skipped, keeps playing on every page | **New, tested** |
| **Listen page**: the official SoundCloud website (sign in, browse, play) embedded in the app, playing while you use other pages | **Rebuilt, tested with public tracks** |
| One audio source at a time: starting local playback pauses the website and vice versa | **New, tested** |
| SoundCloud Go+ tracks | Not playable in the app (DRM); open in Firefox — see below |

### Local player

Library → double-click a track (or select it and press **Play**): the visible library page
becomes the play queue. **Add to queue** appends the selected track; **Queue (n)** in the
player bar shows the queue (double-click to jump, Remove, Clear). The player bar stays at
the bottom of every page. Keyboard: Ctrl+Space play/pause; media keys work while the window
has focus. **Edit tags…** (Ctrl+E) is still available; double-click now plays instead of
opening the tag editor.

Engine: QtMultimedia's FFmpeg backend, shipped inside PySide6 — no extra system package.
Decoding runs in Qt's media threads, never on the GUI thread. Volume and mute are
remembered.

### Listen page (SoundCloud website)

Open **Listen** (Ctrl+5), sign in on the website, and use SoundCloud's own player. From
the Queue or History page, **Listen on SoundCloud** opens the selected track there.
**Stop listening** closes the player and frees its memory; your sign-in is kept.
**Open in browser** opens the current page in your default browser.

Why the engine changed in 0.3.0: SoundCloud now streams only **AAC over HLS**
(`mp4a.40.2`). The QtWebEngine inside the PySide6 wheels is built without proprietary
codecs, so in 0.2.0 *every* track failed with "There was a problem playing this track"
(confirmed: `MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2"')` → `false`).
0.3.0 embeds the distribution's **WebKitGTK** instead, which decodes AAC through
GStreamer. It runs as a separate helper process (`src/sclm/gui/webkit_player.py`, started
with the system `python3`) whose window is embedded into the Listen page on X11.

Requirements on the host (preinstalled on Linux Mint 22 Cinnamon):

```sh
sudo apt install python3-gi gir1.2-webkit2-4.1 gstreamer1.0-libav gstreamer1.0-plugins-good
```

Without them the Listen page explains what is missing and offers **Open in browser**; the
rest of the app is unaffected.

**SoundCloud Go+ tracks do not play in the Listen page — use Open in browser.** Go+
tracks (marked GO+) are only offered to subscribers as DRM-encrypted streams (CENC,
SoundCloud's `ctr-`/`cbc-` transcodings); without a subscription SoundCloud serves a
30-second preview. Playing them requires a licensed DRM module (Widevine), which only
browsers such as Firefox and Chrome ship. The distribution's WebKitGTK has no DRM support
at all (`navigator.requestMediaKeySystemAccess` is undefined), and Widevine cannot be
added to it legitimately. There is no in-app workaround that would not mean circumventing
DRM, so the app hands these tracks to your browser: **Open in browser** (or the banner's
**Open this track in my browser** when SoundCloud reports a playback error) opens the
current page there, where your subscription plays it. In Firefox, DRM playback must be
enabled (Settings → General → "Play DRM-controlled content"). A subscription does not
make protected tracks downloadable through this app.

**DRM is not only Go+.** SoundCloud also encrypts many *free*, ad-supported tracks from
label-distributed artists, for every listener (the track's streams are all
`cbc-`/`ctr-encrypted-hls`). Downloads of such tracks fail with "SoundCloud only streams
this track DRM-encrypted…" (category `drm_protected`, shown in History). Signing in or
subscribing does not change this, and these tracks also need **Open in browser** to play.

**Sign-in.** The website sign-in is stored in the app's own profile
(`~/.local/share/soundcloud-library-manager/listening-webkit/`), separate from the
downloader, which never receives it. After upgrading from 0.2.0 you need to sign in once
more, because the engine changed; the old 0.2.0 profile (`listening-browser/`) is left
untouched.

On Wayland sessions the helper cannot be embedded; it opens as its own window instead.
The desktop's sound applet and media keys also see the web player (MPRIS) as
"SoundCloud (Library Manager)".

## Install the AppImage

Download `SoundCloud_Library_Manager-<version>-x86_64.AppImage` from the GitHub release,
check it against the published SHA-256, make it executable and double-click it:

```sh
sha256sum SoundCloud_Library_Manager-0.3.2-x86_64.AppImage
chmod +x SoundCloud_Library_Manager-0.3.2-x86_64.AppImage
```

The AppImage needs FUSE (`fusermount`/`fusermount3`); without it, run it with
`--appimage-extract-and-run`. It is built on Ubuntu 24.04 / Mint 22 (glibc 2.39) and does
not start on older distributions. It bundles Qt, QtMultimedia, yt-dlp, Mutagen and an
FFmpeg binary; WebKitGTK for the Listen page comes from the host (see above). Settings,
the library database and logs live in `~/.config`, `~/.local/share` and `~/.local/state`
under `soundcloud-library-manager/`, shared with a source install.

## Build from source

```sh
git clone https://github.com/jeparks808/soundcloud-library-manager.git
cd soundcloud-library-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e .
.venv/bin/python -m sclm
```

System requirements: x86_64 Linux desktop (developed on Linux Mint 22.x / Ubuntu 24.04,
X11), Python 3.12+, the Listen-page packages above, and the Qt/X11 libraries listed in
[packaging/README.md](packaging/README.md). FFmpeg is looked up in Settings → `PATH` →
the bundled `imageio-ffmpeg` binary.

## Test and package

```sh
.venv/bin/pytest -q                                   # offline suite (Qt offscreen)
SCLM_NETWORK_TESTS=1 .venv/bin/pytest -q -m network   # opt-in network tests
.venv/bin/python packaging/sclm_playback_smoke.py --network   # real window, X11 desktop
packaging/build_appimage.sh                           # -> dist/…AppImage + frozen self-test
```

The playback smoke test drives the real main window (local playback, embedded SoundCloud
player, cross-pausing) in temporary data and routes its audio to a temporary null sink.
See [packaging/README.md](packaging/README.md) for the AppImage build and its self-tests.

## Known issues and troubleshooting

- **No sound from the local player while the GUI shows it playing:** check the stream in
  the sound settings / `pactl list sink-inputs`. PipeWire remembers mute per media role
  (`~/.local/state/wireplumber/restore-stream`), so a muted "Music" stream from another
  program can mute this one too.
- **Web player CPU use:** measured on a MacBook Air (i5-5350U): ~60 % of one core while a
  SoundCloud stream plays with the Listen page visible, ~30 % on other pages, ~700 MiB
  RAM for the web engine. **Stop listening** releases it. `SCLM_WEBKIT_NO_GPU=1` disables
  GPU compositing (measured slower on that machine).
- **Google sign-in** may refuse embedded browsers; use e-mail sign-in or Open in browser.
- yt-dlp breaks when SoundCloud changes: Help → Update yt-dlp.

Test results and limitations: [EVALUATION_REPORT.md](EVALUATION_REPORT.md),
[DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md), [REQUIREMENTS_CHECKLIST.md](REQUIREMENTS_CHECKLIST.md).
Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
