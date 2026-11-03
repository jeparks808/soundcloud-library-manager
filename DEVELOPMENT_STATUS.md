# Development Status

_Last updated: 2026-11-02_

## Original build environment (historical; see the 0.2.0 update below)

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Zena), kernel 6.8, x86_64, 4 threads, 7.7 GiB RAM |
| Python | 3.12.3 (system), project venv at `.venv` |
| PySide6 | 6.12.0 (venv) |
| yt-dlp | 2026.10.26 (venv). The system package 2024.04.09 is too old for SoundCloud and isn't used |
| Mutagen | 1.48.1 (venv) |
| Beets | 2.14.1 (venv, optional at runtime) |
| FFmpeg | System `ffmpeg`/`ffprobe` 6.1.1 at `/usr/bin` (the resolver prefers PATH). The initial environment check reported "command not found"; that was wrong, corrected after the user's `apt` showed it installed. The imageio-ffmpeg 7.0.2 bundled binary is the fallback |
| Qt runtime libs | **`libxcb-cursor0` missing** on the user's machine, so Qt's xcb plugin aborts at startup. User action: `sudo apt install libxcb-cursor0` |
| Display | X11 (`DISPLAY=:0`) |
| Coding agent | Claude Code 2.1.316 (the active platform). Codex CLI 0.166.0 is installed but not running |

## Original build model / effort orchestration (historical)

### Claude Code (original build platform)

Project-local subagents are in `.claude/agents/*.md` with explicit `model:` and `effort:` frontmatter.
The Agent tool's model choices here are the aliases `opus`, `sonnet`, `haiku` and `fable`. In this
environment, `opus` = Opus 5.5 and `sonnet` = Sonnet 5.5, the current generation. Reasoning effort
levels are `low`, `medium`, `high`, `xhigh` and `max`.

| Stage | Requested | Agent definition | How it actually ran |
|---|---|---|---|
| Architecture | Opus 5.5 / high | `sclm-architect` (opus/high) | see stage log |
| GUI implementation | Sonnet 5.5 / high | `sclm-gui` (sonnet/high) | see stage log |
| Downloader integration | Opus 5.5 / high | `sclm-downloader` (opus/high) | see stage log |
| Metadata and categorization | Opus 5.5 / high | `sclm-metadata` (opus/high) | see stage log |
| Complex debugging | Opus 5.5 / xhigh | `sclm-debugger` (opus/xhigh) | not yet run |
| Minor improvements | Sonnet 5.5 / medium | `sclm-improver` (sonnet/medium) | not yet run |
| Final testing and review | Opus 5.5 / high | `sclm-reviewer` (opus/high) | not yet run |

**Limitation found:** Claude Code doesn't hot-load agent definitions created during a running
session (`Agent type 'sclm-architect' not found`). In the session that created them, each stage
ran as a `general-purpose` subagent with explicit `model` and `effort` overrides matching its
definition, and the definition file was given to the agent as its instructions. From the next
session on, the named agents (`sclm-architect`, etc.) can be used directly.

**What could be verified:** the model alias and effort passed for each spawn, which are recorded
below. Claude Code doesn't report a subagent's effective model or effort back to the parent
session, so per-run verification is limited to the configuration that was requested.

### Codex (portable definitions during the original build)

`.codex/agents/*.toml` defines the requested Codex configuration (gpt-6-astra / gpt-6.1-sol with
`model_reasoning_effort`). It was **not verified**: Codex isn't the running platform, and the
local Codex default model is `gpt-6.1-terra`. Whether those model names are available wasn't checked.

## Stage log

(see below)

### Stage 1: Architecture ✅ (2026-11-02)
- **Ran as:** `general-purpose` subagent, `model=opus`, `effort=high`, with `.claude/agents/sclm-architect.md` as its instructions.
- **Implemented:** `docs/ARCHITECTURE.md` (interfaces, threading, errors, schema, settings, test strategy), `docs/ROADMAP.md`,
  `pyproject.toml`, requirements files, the full `core/` package (models, errors, normalize, settings, paths, logging, ffmpeg
  resolver, event protocol, SQLite storage), `app.py`/`context.py`, `gui/bridge.DownloadBridge`, and stubs for download/, metadata/ and gui/.
- **Validation:** `.venv/bin/pytest -q` gave **112 passed** (re-run by the lead to confirm). `python -m sclm --version` works.
  `python -m sclm` exits with NotImplementedError until Stages 2–3 land (expected).
- **Lead decisions:** accepted ≤500-row paged SQLite reads on the GUI thread. Kept `cookies_file`, which carries only the user's own
  authorization and bypasses nothing. In v1 the "ask" conflict policy resolves to skip, with a "Keep both" action in the queue.
- **Packaging (lead):** `packaging/install.sh`, `sclm.desktop.in`, `sclm.svg`.

### Stages 2–4: GUI, Downloader, Metadata ✅ first working version (2026-11-02)
These ran concurrently because their file ownership doesn't overlap (gui/, download/, metadata/, plus each one's tests/ dir).
| Stage | Ran as | Agent tests reported |
|---|---|---|
| 2 GUI | `general-purpose`, `model=sonnet`, `effort=high`, instructions from `sclm-gui.md` | tests/gui 53 passed |
| 3 Downloader | `general-purpose`, `model=opus`, `effort=high`, instructions from `sclm-downloader.md` | tests/download 112 passed + 4 network passed |
| 4 Metadata | `general-purpose`, `model=opus`, `effort=high`, instructions from `sclm-metadata.md` | tests/metadata 243 passed |

**Lead verification:**
- `pytest -q` → **520 passed, 4 skipped** (15.9 s). `SCLM_NETWORK_TESTS=1 pytest -m network` → **4 passed** (9.9 s).
- Offscreen app launch: window shown in 0.41 s, closes and shuts down cleanly (exit 0).
- Real end-to-end (scratch script, not in repo): preview of a public SoundCloud test track (2.6 s) → DownloadManager →
  real `finalize_track` → `Library/Singles/…mp3` (ID3v2.3: TIT2, TPE1, TPE2, TCON, TDRC, COMM, WOAS, TXXX:SOUNDCLOUD_TRACK_ID,
  APIC). A second enqueue → `skipped_duplicate`. Both outcomes are in the history.
- Reviewed the offscreen screenshots at 1440×900 (Download, Queue, Settings).
- **Not done:** manual GUI use on a real display; a multi-track network download.

**Side effect reported by the metadata agent:** it briefly created an empty `~/.config/beets` and removed it with `rmdir`.
The lead confirmed it's gone.

### Outstanding issues
1. **Bitrate ignored for MP3 sources (D6).** yt-dlp's FFmpegExtractAudio copies a source that's already MP3, so a 192 kbps
   setting produced a 128 kbps file. Plan for Stage 5: record the actual bitrate, show it in the UI, and add an explicit
   "always re-encode to the chosen bitrate" option. The default to be decided; copying avoids a lossy re-encode.
2. `downloads.file_path` isn't cleared when a job is retried, because `Storage.update_download` can't set NULL. A stale value
   can remain on failed rows (not user-visible today).
3. Cancellation can't interrupt an in-flight network resolve (≤20 s socket timeout) or a running ffmpeg conversion (documented v1).
4. Template tidying can strip trailing punctuation such as "..." from titles.
5. On filesystems without hard links, a process crash during the exclusive-create copy can leave a partial file.
6. docs/ARCHITECTURE.md should be updated with the agents' documented deviations (test seams, the title→artist heuristic,
   namespaced user-list playlist ids, the cookies copy, the non-hardlink fallback).

### First user run + minor fixes (2026-11-02)
- **User report:** `python -m sclm` aborted with a core dump: Qt's xcb plugin needs `libxcb-cursor0`, which isn't installed.
  `packaging/install.sh` ran fine. The user also confirmed system ffmpeg 6.1.1 is installed.
- **User decision on issue #1:** keep original MP3s (no re-encode).
- **Ran as:** `general-purpose`, `model=sonnet`, `effort=medium`, instructions from `sclm-improver.md` (Stage 6 config, minor changes).
- **Changes:**
  - `core/platform_check.py`: a pre-flight check before QApplication prints the apt command, shows a best-effort zenity/notify-send
    dialog, and exits 1. `install.sh` warns too.
  - The actual MP3 bitrate is measured with mutagen and stored in history and the job. The queue shows "· 128 kbps (original MP3 kept)".
  - Settings shows a bitrate hint. ARCHITECTURE §13 is updated.
- **Validation (lead):** `pytest -q` → **530 passed, 4 skipped**. The pre-flight was run against the genuinely missing library on this
  machine (DISPLAY=:0, notifiers hidden from PATH): clear message, exit 1, no core dump. The zenity dialog itself was only tested with mocks.
- **Issue #1 resolved.** New minor issue #7: the "(original MP3 kept)" label compares against the *current* target bitrate, so it can
  also appear on a transcoded file if the target was raised afterwards.
- **Pending user action:** `sudo apt install libxcb-cursor0`, then the first manual GUI check.

### Bug fix: playlist preview showed "Track <id>" titles (2026-11-02)
- **User report (manual GUI test, first real-display run):** the GUI opens and fetch works, but every playlist track title is "Track XXXXXXXXX".
  **This is the first manual GUI verification:** startup on the real display, URL fetch and preview table.
- **Root cause (lead):** yt-dlp's `_extract_set` builds flat set entries with only id/url, with no title, uploader or duration.
- **Ran as:** `general-purpose`, `model=opus`, `effort=high`, instructions from `sclm-downloader.md`.
- **Fix:** `download/enrich.py` batch-resolves untitled entries via SoundCloud api-v2 `tracks?ids=` (50 per call) through yt-dlp's own
  SoundCloud client, which keeps client_id handling and cookies. It's best effort and never fails the preview. Our own mapping avoids
  yt-dlp's per-track artwork HEAD requests. Tests prove a placeholder can't reach tags or filenames.
- **Measured (agent):** 484-track set: 10 API calls, 7.8 s preview (3.7 s without the lookup). 6-track EP: 1 call.
- **Validation (lead):** full suite **551 passed, 4 skipped**; network **4 passed** (the EP test now asserts real titles).
- **New issue #8 (found by lead's repeated runs):** in about 25% of repeated `tests/download tests/gui` runs, one test fails
  (`test_clear_finished_and_jobs` or `test_e2e_offline` status-sequence). Cause: `DownloadManager._transition` updates the in-memory
  job under the lock, then persists to the DB and emits to the listener after releasing it. Observers see the new state before the
  DB or listener do. Concurrent transitions on one job could also persist or emit out of order. Assigned to Stage 5 (debugger, opus/xhigh).

### Stage 5: Debugging and reliability ✅ (2026-11-02)
- **Ran as:** `general-purpose` subagent, `model=opus`, `effort=xhigh`, instructions from `sclm-debugger.md`.
  **Limitation:** the agent hit the 5-hour Opus usage limit during final verification and was terminated. Its fixes and
  tests were complete and on disk; the **lead session (by then running on Fable 5) finished the verification** and wrote
  this entry. Per orchestration rule 8, this deviation is reported rather than silently absorbed.
- **Priority 1 (transition-ordering race):** fixed with a per-job `order` lock in `download/manager.py`: per job, in-memory
  state = DB writes = listener emissions in one sequence; terminal states become visible to `get_job`/`jobs`/`is_idle` only
  after their final persist + emit; stale writes can never overwrite a terminal state; listener still never called under the
  manager lock. ARCHITECTURE §6 updated. New tests: tests/download/test_manager_ordering.py (6).
- **Priority 2:** crash reconciliation at startup ("Interrupted" rows, retryable), ffmpeg failure handling with stderr tail
  (custom FFmpegExtractAudioPP subclass in ytdlp_opts.py), network-drop retry in fresh staging with no leaks, removed/restricted
  tracks fail cleanly, DB concurrency stress with integrity_check, bridge events after window destruction, shutdown order
  (manager before storage/widgets), SIGTERM/SIGINT graceful shutdown via socketpair + QSocketNotifier (found by the packaging
  agent). `Storage.update_download(file_path=CLEAR)` sentinel resolves issue #2.
- **Validation (lead, Fable 5):** full suite **574 passed, 4 skipped**; `tests/download tests/gui` 30× in a loop → **0 failures**
  (about 25% of runs failed before the fix); test_manager_ordering.py 100× → **0 failures**. Issues #2 and #8 closed; #3 (cancel
  lag) remains documented v1 behavior.

### Packaging: standalone AppImage ✅ (2026-11-02)
- **Ran as:** `general-purpose` subagent, `model=opus`, `effort=high` (packaging task, outside the seven stages).
- `packaging/build_appimage.sh` → `dist/SoundCloud_Library_Manager-0.1.0-x86_64.AppImage` (76 MB, ~2 min build, pinned
  appimagetool + runtime with SHA-256 checks, lean Qt, bundled libxcb-cursor + certifi + imageio-ffmpeg fallback; same XDG
  data dirs as the venv app). Includes a hidden `--self-test`.
- **Validation (lead):** `--version` OK; `--self-test --network` **11/11 PASS** (Qt, ffmpeg encode, HTTPS, real SoundCloud
  preview). Agent additionally verified it works with the system libxcb-cursor hidden. Not yet verified: real-display window,
  double-click launch, Wayland (plugin not bundled). glibc ≥ 2.38 required. Frozen yt-dlp goes stale → updater planned.
- **User decision:** AppImage goes on the Desktop, after all Stage 5 fixes are in.

### yt-dlp self-updater ✅ (2026-11-02)
- **Ran as:** implemented by the **lead session directly (Fable 5, the session model)**, not a stage subagent: Opus/Sonnet were
  rate-limited until 06:20 (orchestration rule 8: strongest suitable available model, reported here).
- Help → "Update yt-dlp…": checks PyPI, SHA-256-verifies the wheel, installs to `<data_dir>/yt-dlp-override/`; activated at next
  start only when newer than the bundled copy, probe-imported, falls back on any problem. ARCHITECTURE §17.
- **Validation:** full suite **592 passed, 4 skipped**; 16 updater tests + 2 GUI tests; real-PyPI run in a throwaway home →
  correct "already up to date" no-op (venv has the current latest). The real-install path can only be exercised once a newer
  release than the bundled one exists.

### Final AppImage + desktop placement (2026-11-02, lead on Fable 5)
- Rebuilt with all Stage 5 fixes + the yt-dlp updater: `dist/SoundCloud_Library_Manager-0.1.0-x86_64.AppImage` (76 MB, 52 s build).
- **Verified (lead):** `--version`; `--self-test --network` all PASS (incl. real SoundCloud preview); SIGTERM to the frozen app →
  "Received SIGTERM; quitting" / "Shutting down", exit 0.
- **Placed:** copied to `~/Desktop/SoundCloud_Library_Manager.AppImage` (chmod +x, gio trusted; nothing was overwritten — the name
  was free). Mint menu entry `~/.local/share/applications/sclm.desktop` now launches the AppImage (was: venv python).
- **Pending user verification:** double-click launch from the Desktop and the menu on the real display.


### 0.2.0 desktop listening integration (2026-11-02, Codex continuation)

- Scope explicitly corrected by the user: update `/home/john/soundcloud-library-manager`,
  the source verified against the Desktop AppImage and uploaded to
  `jeparks808/soundcloud-library-manager`. Preserve the original architecture/history.
- Implemented lazy **Listen** page, official website sign-in profile, Queue/History
  listen buttons, HTTPS navigation restrictions, visible identity-provider address,
  external fallback, stop/reopen and orderly browser/profile disposal. Download services
  and user music/history are unchanged. Version bumped to 0.2.0; full PySide6 dependency.
- Packaging now retains WebEngineProcess, resource packs, English locale, GLX/EGL plugins
  and dynamically loaded NSS modules. Added isolated `--browser-smoke [--network]`.
- Requested GUI agent: `gpt-6.1-sol/high`; it hit the usage limit. The root session finished
  implementation and disclosed the limitation. Requested independent review agent:
  `gpt-6-astra/high`, accepted by the collaboration tool and completed read-only review.
  Spawn parameters are observable; no independent backend model/effort attestation is
  exposed. No model switch is claimed for the root continuation.
- Review found missing dynamically loaded NSS modules and stale address display during
  main-frame provider login; both corrected. Reviewer ran 22 listening tests: passed.
- Actual validation so far: baseline 592 passed/4 skipped; new listening tests22 passed;
  integrated full suite614 passed/4 skipped (30.03 s); after identity-display fix23 focused
  tests passed. Four network pytest tests are opt-in and were skipped in these commands.
- Real-display source browser smoke passed: generated muted MP3 decoded/time advanced;
  official homepage rendered; popup/profile closed without warnings. Screenshot inspected
  at1360×850. First fixture attempt failed because its view was hidden; corrected the
  fixture to display its view before asserting playback.
- First AppImage `--self-test --network`: all10 checks passed (FFmpeg encode, HTTPS,
  live yt-dlp preview included). Browser smoke exposed missing XCB OpenGL plugins and a
  stale hidden-view fixture. First build completed in144 s (180 MiB); second intermediate
  build was intentionally stopped after the review identified NSS pruning.
- Final rebuild and desktop placement results will be recorded below. No GitHub push:
  user explicitly requires successful personal testing before publication.
- Limits: this wheel reports MP3 supported and AAC unsupported; no bundled Widevine.
  SoundCloud account login, Go+ tracks, audible playback and manual desktop launch
  remain unverified. The external-browser fallback is available. Do not present a
  rendered homepage or muted fixture as verification of protected streaming.

- Final source regression run after review fixes: `.venv/bin/pytest -q` → **615 passed,
  4 skipped in 32.74 s**. Review follow-up confirmed NSS and GLX/EGL files in AppDir.
- Subsequent frozen test decoded MP3 but paused the muted fixture when hidden. Source
  probe with normal playback at 1% volume passed foreground/background time advancement,
  homepage loading and teardown. Smoke fixture updated accordingly with finally cleanup;
  no production workaround or browser security flag was added. A final rebuild follows.

- Release validation completed: AppImage `--self-test --network` all 10 PASS; real-display
  `--browser-smoke --network` passed MP3 decoding, foreground/background time advancement,
  homepage render and popup/profile cleanup. Screenshot `/tmp/sclm-listen-appimage.png`
  inspected. This is automated real-display validation, not manual account/Go+ validation.
- Installed tested 0.2.0 to `/home/john/Desktop/SoundCloud_Library_Manager.AppImage` and launched it.
  SHA-256 `40bd2fdb6e057283831e0402083d3e0e9d85e6e959f2f9c6a4305756f2d39fba`. Original backup: `/home/john/soundcloud-library-manager/dist/desktop-backups/SoundCloud_Library_Manager-0.1.0-20261102-152248.AppImage`.
  Installed size 189,008,376 bytes. Music/settings/history preserved.
- User confirmation of actual account/failed-track playback remains pending. All changes
  stay local; no GitHub push has occurred. See EVALUATION_REPORT.md for full scope/limits.

- Installation build completed in133 s. Installed app logged Ready0.65 s after its Python
  entry point; process and real X11 window confirmed. A nonfatal Qt host-portal registration
  warning appeared during this programmatic launch; the window remained open. External
  browser handoff in the user's desktop session is still a manual check.
- Release smoke cleanup race resolved by deleting the test-owned QApplication before
  removing its temporary profile. Final packaged browser test exited0.

### 0.3.0 playback integration + Listen-page repair (2026-11-02, lead: Claude Opus 5.5)

- Project selection: the Desktop AppImage's SHA-256 equals this repo's
  `dist/…0.2.0…AppImage`; the menu entry launches it. `soundcloud-library-manager-codex`
  was identified and left unmodified.
- Root cause: SoundCloud only streams AAC/HLS; the PySide6 QtWebEngine has no AAC (and no
  Widevine). It was not DRM for ordinary tracks (their transcodings are plain `hls` AAC; the earlier
  note that Firefox has no CDM was wrong — its active profile has Widevine).
  Replaced by the host's WebKitGTK in a helper process (`gui/webkit_player.py`), embedded
  via X11 with frame sync disabled, and paused over MPRIS.
- New local player (`gui/player.py`, QtMultimedia), persistent player bar, Library
  Play/Add to queue/double-click, mutual pausing of local and web audio.
- AppImage without QtWebEngine (94 MB). `--browser-smoke` was replaced by `--playback-smoke`.
- Incident: test runs that muted WebKit at the GStreamer level made WirePlumber remember
  `media.role:Music:mute=true`, silencing the user's local playback. Repaired and the test
  mute removed; the smoke now uses a temporary null sink. Lesson: never mute at the
  sound-server level in tests.
- Verified (all real runs):
  - pytest 628 passed / 4 skipped
  - source and AppImage `--playback-smoke --network` PASS
  - AppImage `--self-test` PASS
  - audible per-stream capture: local −35 dBFS, web −22 dBFS, both unmuted on the real sink
- Desktop AppImage replaced after the tests passed; the 0.2.0 backup is in
  `dist/desktop-backups/`.
- Still pending (user): signed-in and Go+ playback, hearing local playback after the fix,
  double-click launch. See EVALUATION_REPORT.md.

### 0.3.1 Listen view height + Go+ investigation (2026-11-02, lead: Claude Opus 5.5)

- User confirmed (manual): Listen-tab playback signed in, and local library playback, both work.
- Embedding race fixed: Qt cleared override-redirect on the adopted GTK window, so the WM
  occasionally framed it as a separate window (1/5 smoke runs). Fix: BypassWindowManagerHint;
  smoke now checks the X ancestry.
- User report: the Listen web view used only the top ~300 px. Cause: a leftover layout
  spacer shared the stretch with the browser widget. Fixed and covered by a regression test
  that fails on the old layout; real-window probe confirms the view follows resizes.
- Go+: not playable in-app. Go+ (`SUB_HIGH_TIER`) full streams are DRM-encrypted for
  subscribers. Distribution WebKitGTK exposes no EME at all, and Firefox (the user's
  default browser, Widevine enabled) is the legitimate route. The UI and README now say
  this plainly. Corrected the earlier wrong note that the user's Firefox had no Widevine.
- The laptop froze once during this work, while the user's app, Firefox and a WebKit probe
  were running. The previous boot's journal shows no OOM kill or GPU hang. Probes now run
  under `systemd-run --scope -p MemoryMax` with timeouts.
