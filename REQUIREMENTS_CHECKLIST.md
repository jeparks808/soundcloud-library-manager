# Requirements Checklist

States: **done+tested** (automated test run and passed) · **done (untested)** · **manual** (verified by
hand in the GUI only) · **partial** · **todo**. Updated by the lead engineer after each stage.

"Offscreen" GUI tests are automated pytest-qt tests on `QT_QPA_PLATFORM=offscreen`. They are **not** manual
verification. Manual GUI verification so far (user, 2026-11-02): startup on the real display, playlist fetch/preview, downloads — "its working great!".

Last test run (lead, 2026-11-02, 0.3.0): `pytest -q` → **628 passed, 4 skipped**; AppImage self-test + `--playback-smoke --network` PASS.
Earlier run (lead, 2026-11-02, after Stage 5): `pytest -q` → **574 passed, 4 skipped**; download+gui repeated 30× → 0 failures; ordering tests 100× → 0 failures; `SCLM_NETWORK_TESTS=1 pytest -m network` → **4 passed**;
lead end-to-end script against real SoundCloud → pass (details in DEVELOPMENT_STATUS.md, Stage 2–4).

| # | Requirement | Stage | State | Evidence |
|---|---|---|---|---|
| A1 | Modular components: GUI, playlist retrieval, queue/workers, conversion, metadata, organization, storage, settings/logging | 1 | done+tested | `src/sclm/{core,download,metadata,gui}`; per-package test suites |
| A2 | Interfaces, error handling, data models, test strategy, dependency mgmt documented | 1 | done | docs/ARCHITECTURE.md, pyproject.toml, requirements*.txt |
| G1 | Dark theme with orange accents | 2 | done+tested | tests/gui/test_theme.py; offscreen screenshots reviewed by lead |
| G2 | SoundCloud URL input | 2 | done+tested | test_preview_panel (empty URL, Enter/fetch, error banner) |
| G3 | Playlist preview | 2/3 | done+tested | Titles/artists/durations for sets come from a batched api-v2 lookup (download/enrich.py, tests/download/test_enrich.py, 19 tests). Network EP test asserts real titles. Bug "Track <id>" titles reported by the user and fixed 2026-11-02 |
| G4 | Track selection | 2 | done+tested | test_selection_produces_download_requests |
| G5 | Download settings (bitrate, destination, new-only) | 2 | done+tested | test_settings_dialog.py; "Only new tracks" toggle in the preview panel |
| G6 | Live progress indicators | 2 | done+tested | test_queue_view (worker-thread signals, coalescing at 10 Hz) |
| G7 | Queue management (cancel/retry/keep both/cancel all/clear) | 2 | done+tested | test_actions_call_manager (fake manager) |
| G8 | Library browser | 2 | done+tested | test_history_library.py (list, search, background scan, edit) |
| G9 | Metadata editor | 2/4 | done+tested | test_metadata_editor.py, tests/metadata/test_scanner_editor.py |
| G10 | Application settings dialog | 2 | done+tested | test_settings_dialog.py |
| G11 | Usable at 1440×900 | 2 | partial | test_smoke_construct_at_target_sizes + offscreen 1440×900 screenshots; **not checked on the real display** |
| D1 | yt-dlp Python API integration | 3 | done+tested | download/manager.py uses YoutubeDL; network tests |
| D2 | Individual tracks | 3 | done+tested | network download test; lead e2e script |
| D3 | Entire playlists | 3 | done+tested | test_preview (offline fixtures); network EP preview/sync test. A multi-track network *download* hasn't been run |
| D4 | Per-track progress | 3 | done+tested | manager progress hook → ProgressEvent; lead e2e received progress events |
| D5 | MP3 conversion | 3 | done+tested | test_e2e_offline (WAV→MP3 through real yt-dlp + ffmpeg) |
| D6 | Configurable bitrate | 3 | done+tested | Applied when the source is transcoded (test_e2e_offline 128/192 kbps). **User decision 2026-11-02:** MP3 sources are kept as-is (no lossy re-encode). The actual bitrate is measured and stored/shown (test_e2e_offline MP3 case, test_queue_view) |
| D7 | Artwork + metadata embedding | 3/4 | done+tested | tests/metadata/test_tagging_duplicates.py; lead e2e: APIC + ID3v2.3 frames present |
| D8 | Cancellation | 3 | done+tested | test_cancel_while_{queued,downloading,resolve,during_backoff}; real yt-dlp cancel in test_e2e_offline |
| D9 | Retry handling | 3 | done+tested | test_network_retry_then_success, retries_exhausted, manual_retry_same_row |
| D10 | Persistent download history | 3 | done+tested | test_history_rows_for_mixed_outcomes; tests/core/test_storage.py; lead e2e |
| D11 | Download only newly added tracks | 3 | done+tested | test_only_new_after_file_deleted, preview sync tests; network EP re-sync |
| D12 | Downloads don't block GUI event loop | 2/3 | done+tested | worker pool + bridge; test_model_updates_from_bridge_signals_emitted_on_worker_thread, test_runs_off_gui_thread |
| M1 | ID3 metadata editing | 4 | done+tested | metadata/editor.py; test_scanner_editor (atomic save) |
| M2 | Artist/title/album extraction | 4 | done+tested | metadata/titles.py, download/mapping.py; test_genre_titles, test_mapping |
| M3 | Embedded cover artwork | 4 | done+tested | test_tagging_duplicates (APIC); lead e2e |
| M4 | Consistent filenames | 4 | done+tested | test_naming.py (hostile names, byte truncation, reserved names) |
| M5 | Duplicate detection | 4 | done+tested | id / artist+title ±2 s / audio hash tests; lead e2e second run → skipped_duplicate |
| M6 | Organize by playlist/artist/album/genre | 4 | done+tested | test_organizer.py |
| M7 | Optional Beets integration | 4 | done+tested | test_beets.py (real beets 2.14.1, tmp config; files untouched) |
| M8 | Safe handling of missing/unreliable genre | 4 | done+tested | test_genre_titles.py |
| M9 | Never overwrite user files without permission | 4 | done+tested | test_organizer (existing dest, EEXIST race, EXDEV, read-only); save_tags only on explicit Save |
| R1 | Qt threading/signal handling | 5 | done+tested | Per-job order lock in manager (tests/download/test_manager_ordering.py, 6 tests); bridge cross-thread tests; events after window destroyed (tests/gui/test_shutdown.py). 30×/100× loops clean |
| R2 | Interrupted downloads | 5 | done+tested | Non-terminal rows reconciled as "Interrupted" at startup (test_crash_mid_download_is_reconciled_on_next_start); stale staging cleanup; SIGTERM/SIGINT run the normal shutdown (test_signal_runs_the_normal_shutdown) |
| R3 | FFmpeg errors | 5 | done+tested | tests/download/test_ffmpeg_failures.py (broken binary, missing path, none anywhere, stderr tail kept; no retry, no partial files) |
| R4 | Malformed filenames | 5 | done+tested | test_naming.py |
| R5 | Unavailable tracks | 5 | done+tested | test_errors (unavailable/private/geo); network Go+ track → AUTH_REQUIRED; removed-after-preview → test_tracks_removed_or_restricted_after_preview_fail_cleanly |
| R6 | Missing metadata | 5 | done+tested | test_mapping missing fields; scanner filename fallback |
| R7 | Duplicate files | 5 | done+tested | see M5/M9 |
| R8 | Playlist changes | 5 | done+tested | preview sync added/removed tests |
| R9 | Network interruptions | 5 | done+tested | Connection drop mid-download retries in a fresh staging dir; repeated drops fail without leaks (tests/download/test_reliability.py). Real network-drop not simulated at OS level |
| R10 | Database consistency | 5 | done+tested | WAL, RLock, migrations; `file_path=CLEAR` sentinel fixes stale paths on retry; concurrency stress test with integrity_check (test_db_stays_consistent_with_workers_gui_reads_previews_and_scans_at_once) |
| I1 | UI performance / resource use | 6 | partial | progress coalescing, model/view, paged queries; no measurements yet |
| I2 | Accessibility / usability | 6 | partial | accessible names + shortcuts tested (test_main_controls_have_accessible_names) |
| Q1 | Linux desktop launcher | 7 | done+tested | AppImage on ~/Desktop (executable, trusted); Mint menu entry repointed to it (desktop-file-validate OK). Frozen app: self-test 11/11 incl. real preview; SIGTERM → clean shutdown, exit 0 (lead-run). Double-click itself still needs the user |
| Q3 | Clear startup failure when a Qt system lib is missing | 6 | done+tested | tests/core/test_platform_check.py (8); lead ran it against the real missing lib → message + exit 1 |
| Q3b | yt-dlp self-update (AppImage stays usable when SoundCloud changes) | 6 | done+tested | download/updater.py; tests/download/test_updater.py (16), 2 GUI tests; real-PyPI no-op check by lead. Install path of a truly newer release untestable until one exists |
| Y1 | Alternatives page under Listen: failed downloads (DRM/Go+/unavailable/region) listed | 0.4.0 | done+tested | test_failed_tracks_lists_only_unresolved_alternative_failures; test_page_sits_under_listen_in_the_window; live: 130 failed tracks from a copy of the user's DB |
| Y2 | YouTube candidates with length check (Δ, match level), verified channel, views | 0.4.0 | done+tested | test_match_levels_and_candidate_mapping, test_search_ranks_by_length_then_verified, GUI label test; live "9k Freestyle": official verified video first, Δ +2.3 s |
| Y3 | DRM-only candidates labelled and never downloaded | 0.4.0 | done+tested | test_drm_only_formats_are_labelled_and_not_downloadable, test_check_source_never_allows_drm_and_labels_errors, test_drm_only_and_unavailable_candidates_cannot_be_downloaded, test_alternative_source_with_only_drm_formats_is_never_downloaded (manager refuses too). No real DRM-only YouTube video was available to test live |
| Y4 | User-confirmed lossy download keeps SoundCloud tags/artwork | 0.4.0 | done+tested | test_confirmed_download_enqueues_the_youtube_source, test_alternative_source_download_keeps_soundcloud_metadata; live download into an isolated SCLM_HOME: completed in 23.5 s, TIT2/TPE1/APIC/WOAS/TXXX from SoundCloud, COMM names the YouTube URL; AppImage smoke: frozen search + checks PASS |
| Y5 | Searches/checks off the GUI thread | 0.4.0 | done+tested | test_failed_track_is_searched_checked_and_labelled asserts no work on MainThread |
| R11 | DRM failures reported as DRM (not sign-in/Go+) | 0.3.2 | done+tested | DrmProtectedError / `drm_protected`; test_errors drm case, test_manager no-retry, test_models message; live check on Chris Travis "9k Freestyle" |
| Q2 | No DRM/authorization bypass | all | done+tested | Go+ preview-only → AuthRequiredError (network test); DRM → DrmProtectedError (0.3.2); `allow_unplayable_formats` forced False for every download (0.4.0, test_download_opts_never_allow_drm_formats); cookies only pass the user's own login |

| L1 | Official SoundCloud Listen page + Queue/History links | 0.2→0.3 | done+tested | engine replaced by WebKitGTK helper (0.3.0); tests/gui/test_listening.py (protocol with fake helper, unavailable engine, signals); AppImage playback smoke: embedded page loads |
| L2 | Independent persistent website login/profile | 0.3.0 | done+tested, manual | profile `listening-webkit/` (0700, persistent cookies) created and reused by the helper; user signed in and played their likes (screenshots) |
| L3 | Real SoundCloud playback in the app, continuing on other pages | 0.3.0 | done+tested | AppImage `--playback-smoke --network`: public AAC track plays (is-playing-audio), same process after page switch; audible capture −22 dBFS on the real sink |
| L4 | Go+ / protected streaming inside the app | 0.3.1 | unsupported (DRM) — handed to Firefox | user confirmed Go+ fails in-app; WebKitGTK has no EME (probe); Go+ = SUB_HIGH_TIER, encrypted for subscribers; see EVALUATION_REPORT 0.3.1 | no Widevine in WebKitGTK; error banner + "Open this track in my browser" (banner logic tested offline); real Go+ track needs the user's account |
| L5 | Desktop AppImage update / GitHub publication | 0.3.0 | desktop done+tested; publication see EVALUATION_REPORT.md | backup verified; self-test + playback smoke PASS before replacement |
| P1 | Local player: play/pause/stop, next/previous, seek, elapsed/total, volume/mute | 0.3.0 | done+tested | tests/gui/test_player.py (fake + real QtMultimedia); AppImage playback smoke |
| P2 | Title/artist/artwork display | 0.3.0 | done+tested | test_real_mp3_seek_and_artwork; smoke checks cover art |
| P3 | Queue, double-click to play, auto-advance | 0.3.0 | done+tested | test_library_double_click_plays_page_as_queue, end-of-media tests; smoke real double-click |
| P4 | Missing/unplayable files handled gracefully | 0.3.0 | done+tested | missing reported once and skipped; all-failed stops (unit + real + smoke) |
| L6 | Listen view fills the page and follows window resizes | 0.3.1 | done+tested | test_browser_view_fills_the_page_height (fails on old layout); real-window probe 1150×611 → 960×471 |
| P5 | Non-blocking GUI, playback persists across tabs, one audio source at a time | 0.3.0 | done+tested | QtMultimedia threads; smoke page switches; mutual pause local↔web (MPRIS) in smoke and test_web_and_local_playback_pause_each_other |
| P6 | Local playback audible on the user's desktop | 0.3.0 | done+tested, manual (user confirmed) | per-stream capture −35 dBFS on the real sink after the PipeWire role-mute repair; user hearing check pending |
