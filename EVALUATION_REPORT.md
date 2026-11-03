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
- DRM was not the cause for ordinary tracks: their transcodings are plain `hls` AAC
  (`aac_160k`, `aac_96k`), not SoundCloud's encrypted `ctr-`/`cbc-` protocols. (An earlier
  version of this report said the user's Firefox had no Widevine; that was wrong — the
  active profile has Widevine 4.10.3112.0 enabled. It matters for Go+, see below.)

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

- **Local audio after the PipeWire fix:** confirmed by the user ("playback of the local
  library is now successful").
- **Signed-in Listen playback:** confirmed by the user, signed in, with screenshots
  (likes page playing).
- **Go+ tracks:** the user reports they do not play in the Listen page. Explained, not
  fixable in-app (see "Go+ investigation (0.3.1)").
- **Double-click from the Desktop or menu:** not yet confirmed by the user. The programmatic
  launch of the same file passed.

## 0.3.1: Listen view height and Go+ investigation

**Listen view height (user report: "the scaling for the listen tab is poorly implemented").**
The web view filled only the top ~300 px of the page. Cause: the page's content layout
held the browser widget (stretch 1) next to a leftover spacer (stretch 1) for the
placeholder text, so the browser always got about half the height. GTK was ruled out: an
override-redirect GTK window followed every external resize in a probe. Fix: the
placeholder takes the stretch itself and is hidden while the browser is shown.

- New test `test_browser_view_fills_the_page_height`: fails on the old layout (317 px left
  empty), passes with the fix.
- Real-window probe with the real WebKit view: 1190×700 page → embedded GTK window
  1150×611; after resizing to 1000×560 → 960×471. A screenshot shows soundcloud.com filling
  the page.

**Embedding race (found by the 0.3.1 AppImage smoke test).** In 1 of 5 smoke runs
(source and AppImage alike), the helper window appeared as a separate, decorated window
instead of inside the Listen page. The same thing had shown up once in an earlier sizing
probe.

- Cause: after `QWindow.fromWinId`, Qt rewrote the window's X attributes without
  override-redirect. `xwininfo` showed GTK's `yes` turned into `no`. A window manager then
  adopts the window if it is mapped as a top-level before Qt's reparent.
- Fix: `Qt.BypassWindowManagerHint` on the foreign window, so override-redirect stays `yes`.
- Keyboard focus was re-checked with XTest: typing reaches SoundCloud's search box, and
  clicking the address bar returns focus to Qt.
- The smoke test now asserts that the helper window's X ancestors include the main window,
  so a window-manager frame fails it immediately.

**Go+ investigation.** The Go+ tracks are DRM-protected and cannot be played in the
embedded engine without circumventing DRM. The supported workaround is Firefox. Evidence
(none of it uses the user's sign-in):

- **Anonymous public API**, "Cop a Chicken" (shown with a GO+ badge in the user's library):
  - `monetization_model=SUB_HIGH_TIER`, `policy=SNIP`
  - only 30-second MP3 previews are offered (`snipped`)
  - an ordinary track by comparison: full `hls` AAC transcodings
- **Subscriber formats:** yt-dlp's SoundCloud extractor treats transcodings whose protocol
  starts with `ctr-`/`cbc-` (CENC encryption) as DRM and skips them.
  - The actual subscriber transcoding list was not fetched, since that would require the
    user's session. The DRM classification of Go+ full streams is therefore inferred from
    this evidence, not observed directly.
- **WebKitGTK 2.52.6** (distribution build): `navigator.requestMediaKeySystemAccess` is
  undefined, so it has no EME and no key system at all (Widevine, FairPlay, PlayReady,
  ClearKey).
- **Installed browsers:** Firefox 159 is the only one and is the default browser. Its active
  profile has Widevine 4.10.3112.0 with `media.eme.enabled=true`.

Options considered:

- **Rejected:**
  - Adding a CDM to WebKitGTK: no licensed CDM exists for it.
  - QtWebEngine + Widevine: the wheel also lacks AAC, so it is not viable.
  - Downloading or decrypting the stream: this would bypass DRM and is forbidden.
- **Kept:** the existing handoff (**Open in browser**, and the error banner's
  **Open this track in my browser**).
- **Changed:** the UI and README now say plainly that Go+ tracks need Firefox, and no
  longer suggest that Go+ works in the Listen page.

**0.3.1 verification (real runs):**

- `pytest -q`: 629 passed, 4 skipped.
- 0.3.1 AppImage `--self-test`: PASSED.
- `--playback-smoke --network`: **6/6 PASS**, including the new X-ancestry embedding
  check.
- Desktop AppImage replaced: SHA-256
  `0f554984a9386616d96904a6a157e6ec7a31e1ad6bb544e4db4c0c9315b41286`. Backup:
  `dist/desktop-backups/SoundCloud_Library_Manager-0.3.0-*.AppImage`.

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
