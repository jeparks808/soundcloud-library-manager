# Evaluation report — 0.3.0 playback integration and Listen-page repair

Date: 2026-11-02. Scope: the original `sclm` application in
`/home/john/soundcloud-library-manager` (the source of the Desktop AppImage and of
`jeparks808/soundcloud-library-manager`). The separate `soundcloud-library-manager-codex`
folder was identified and left unmodified. Earlier stage results are kept in
DEVELOPMENT_STATUS.md. Automated checks and manual checks are listed separately.

## Root cause of "There was a problem playing this track" (0.2.0)

Every SoundCloud track failed in the 0.2.0 Listen page and worked in Firefox because:

- SoundCloud now serves public tracks only as AAC over HLS (`yt-dlp -F`: `hls_aac_96k`,
  `hls_aac_160k`) and plays them with Media Source Extensions.
- The QtWebEngine in the PySide6 6.12.0 wheels (Chromium 140) is built without
  proprietary codecs. In that engine, `MediaSource.isTypeSupported('audio/mp4;
  codecs="mp4a.40.2"')` and `<audio>.canPlayType` both reject AAC. MP3 and Opus are
  accepted, which is why the 0.2.0 MP3 fixture passed.
- DRM was not the cause for ordinary tracks. The user's Firefox profiles have no Widevine
  plugin installed, yet those tracks play there.

The fix replaces the engine. The distribution's WebKitGTK 2.52.6 (GStreamer `avdec_aac`)
accepts the AAC type and plays the same tracks.

## Implemented

- **Local player** (`gui/player.py`, QtMultimedia FFmpeg backend bundled with PySide6):
  - play/pause/stop, previous/next, click-to-seek bar, elapsed/total time
  - volume and mute, both persisted
  - title, artist and embedded cover art
  - play queue with popup (jump/remove/clear), Add to queue, double-click or Play
  - auto-advance
  - missing or unplayable files reported once and skipped, with a stop when all of them fail
  - persistent player bar on every page; Ctrl+Space and media keys
- **Listen page** (`gui/webkit_player.py` helper + `gui/listening_browser.py`):
  - The official website runs in WebKitGTK in a separate process under the system
    python3, embedded into the page on X11, or as its own window elsewhere.
  - Persistent sign-in profile (`listening-webkit/`, 0700).
  - The page is not reloaded on tab switches, and playback continues on other pages.
  - SoundCloud's error text is detected and shown as a banner with **Open this track in
    my browser**; an external-browser fallback is also available.
  - Clear message when WebKitGTK is missing; downloads are blocked and permissions denied.
  - Sign-in popups are allowed; other sites open externally.
  - Stop listening frees the process.
- **One audio source at a time:** local playback pauses the website through MPRIS, and
  web playback pauses local playback.
- **Packaging:**
  - QtWebEngine removed from the AppImage, QtMultimedia added (94 MB instead of 189 MB).
  - Self-test adds an MP3 decode check and a host WebKitGTK probe.
  - New `--playback-smoke [--network]` routes audio to a temporary null sink.
- **Unchanged:** downloader, conversion, tagging, organization, duplicates and history.
  Library double-click now plays; the tag editor stays on Edit tags… / Ctrl+E.

## Automated tests actually executed

| Command / check | Result |
|---|---|
| `.venv/bin/pytest -q` (final source, after all edits) | **628 passed, 4 skipped** (network opt-in) in 37.07 s |
| `pytest tests/gui/test_player.py tests/gui/test_listening.py` | 36 passed in 3.94 s, including two real-QtMultimedia MP3 tests: playback, auto-advance, missing-file skip, seek, cover art |
| Source `python packaging/sclm_playback_smoke.py --network` (X11) | RESULT: PASS |
| AppImage build (`packaging/build_appimage.sh`) | exit 0 in 97 s; in-build frozen self-test PASSED |
| `SoundCloud_Library_Manager-0.3.0-x86_64.AppImage --self-test` | All checks PASS: xdg-paths, imports, tls-ca, child-env, xcb-cursor, qt, ffmpeg (MP3 encode), multimedia (QAudioDecoder 44100 frames), webkit-player (host WebKitGTK 2.52.6), beets |
| AppImage `--playback-smoke --network` (X11, real window) | RESULT: PASS in 17 s, all steps listed below |
| Desktop path launch (isolated `SCLM_HOME`, auto-quit 9 s) | `Ready (0.62 s after start)`, clean shutdown, exit 0 |
| `desktop-file-validate ~/.local/share/applications/sclm.desktop` | valid |

The AppImage playback smoke test passed every step:

- **Local:**
  - A real double-click plays a generated MP3; its artwork is shown.
  - Playback continues on another page (+600 ms checked).
  - Seek to 5 s, pause/resume, volume and the mute button all work.
  - Next on a missing file reports "Can't play missing.mp3: file missing or unreadable." and skips it.
  - Playback stops at the end of the queue.
- **Web:**
  - The helper process starts embedded.
  - In SoundCloud's official embed player, an XTest click on Play gives `is-playing-audio` = true for a public track ("Ghetto Life").
  - Website playback paused local playback.
  - The page keeps playing on another page, in the same process, with no reload.
  - Resuming local playback paused the website through MPRIS.
  - Stop listening ends the process, with no helper left behind.

**Audible-output check** (one-off probe, real speakers, per-stream capture with `parec --monitor-stream`):

| Stream | Level | State |
|---|---|---|
| Local tone, player volume 30% | −35 dBFS | unmuted, analog sink, 100% stream volume |
| "Ghetto Life" in the embedded WebKit player | −22 dBFS | unmuted, same sink |

SoundCloud's two extra WebAudio streams were silent, as expected.

Failures found and fixed during testing:

- **Black embedded content:** GTK frame sync stalled. Fixed by pre-mapping and disabling frame sync.
- **Pause via a DOM click was unreliable** in the widget. Replaced by MPRIS Pause.
- **Second clicks in the anonymous widget hit an "explore" overlay.** Fixed the smoke ordering; SoundCloud links now load in the player instead of popups.
- **Assorted test-harness bugs** (described in DEVELOPMENT_STATUS.md).
- **Silent local playback reported by the user:**
  - Cause: an earlier test-only GStreamer mute was remembered by WirePlumber for the "Music" role (`restore-stream`).
  - Repaired the user's stream and role entry.
  - Removed the test mute; tests now use a temporary null sink.
  - Afterwards: no `mute=true` entries and no stream pinned to the test sink.

## Manual checks (user)

- **Local audio after the PipeWire fix:** awaiting the user's confirmation. The audible
  probe above is automated evidence, not a hearing test.
- **Signed-in playback and Go+ tracks:** **untested.** This needs the user's account, and
  no credentials were requested.
  - Expected: tracks SoundCloud serves as AAC play.
  - Expected: Widevine-only streams cannot play, because WebKitGTK here has no CDM. They
    show SoundCloud's error, which triggers the "Open this track in my browser" banner.
- **Double-click from the Desktop or menu:** not yet confirmed by the user. The programmatic
  launch of the same file passed.

## Limitations

- **Host packages needed for the Listen page:** `python3-gi gir1.2-webkit2-4.1 gstreamer1.0-libav`.
  They are present on Mint 22 and not bundled. If they are missing, the page explains this
  and offers Open in browser.
- **Wayland:** the web view runs as a separate window and cannot be embedded (not tested).
- **The 0.2.0 sign-in is not migrated:** it was a Chromium profile. The old profile is left
  untouched, and the user signs in once more.
- **Web player cost on the i5-5350U** (see measurements): high CPU and memory compared with
  local playback. Stop listening releases it.
- **Google sign-in** may refuse embedded browsers.

## Measurements (i5-5350U, 8 GB, source build unless noted)

| Measurement | Result |
|---|---|
| Startup to window | 1.05 s (AppImage: `Ready` at 0.62 s after Python entry) |
| Idle | 1.2 % CPU, 91.5 MiB PSS |
| Local MP3 playback | 5.9 % CPU, 118 MiB; first audio 165 ms after play |
| Web player embedded | 0.55 s; soundcloud.com home loaded 2.30 s (cold profile) |
| Web AAC playing, Listen visible | ~72 % of one core total (web process 60 %), ~725 MiB |
| Web AAC playing, other page shown | ~32 % (web process 30 %) |
| Home page, just after load | ~145 % briefly, ~730 MiB |
| `SCLM_WEBKIT_NO_GPU=1` | worse (78 % / 64 %, home load 17.3 s), so GPU compositing stays the default |
| AppImage | 97,929,720 bytes (0.2.0: 189,008,376); build 97 s |

## Deployment

- Desktop: `/home/john/Desktop/SoundCloud_Library_Manager.AppImage`, replaced only after the
  AppImage self-test and playback smoke test passed.
  - SHA-256 `dec33041d6c64c81d1b408c7255e6b01253604b2764c175546deef5ca46dd3b2`
- Backup of the replaced 0.2.0 file (SHA-256 verified identical):
  `dist/desktop-backups/SoundCloud_Library_Manager-0.2.0-20261102-172351.AppImage`.
- Menu entry `sclm.desktop` is unchanged (same Exec path).
- User data was backed up before testing to
  `~/.local/share/soundcloud-library-manager-backups/20261102-163050/`.
  - The library DB (161 tracks), music, tags and artwork were not modified.
  - Tests ran in isolated `SCLM_HOME` / `XDG_CONFIG_HOME`.

## Model / effort evidence

- This update was done entirely in the main Claude Code session, model `claude-opus-5-5`
  (Opus 5.5). The desktop app shows effort "High".
- No stage subagents were spawned, so the `.claude/agents/*` model/effort settings were not
  exercised in this update and are not claimed as verified.

## Earlier update (0.2.0, summary)

0.2.0 added the Listen page on QtWebEngine with an isolated profile, Queue/History links
and an external fallback. Its tests passed (615 passed, 4 skipped; MP3 browser smoke), but
real SoundCloud tracks never played, for the AAC reason above. 0.3.0 replaces that engine.
The 0.2.0 records remain in DEVELOPMENT_STATUS.md.
