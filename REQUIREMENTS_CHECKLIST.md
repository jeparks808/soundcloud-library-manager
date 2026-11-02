# Requirements Checklist

States: **done+tested** (automated test run and passed) · **done (untested)** · **manual** (verified by
hand in the GUI only) · **partial** · **todo**. Updated by the lead engineer after each stage.

"Offscreen" GUI tests are automated pytest-qt tests on `QT_QPA_PLATFORM=offscreen`. They are **not** manual
verification. Manual GUI verification so far (user, 2026-11-02): startup on the real display, playlist fetch/preview, downloads — "its working great!".

Last test run (lead, 2026-11-02, after Stage 5): `pytest -q` → **574 passed, 4 skipped**; download+gui repeated 30× → 0 failures; ordering tests 100× → 0 failures; `SCLM_NETWORK_TESTS=1 pytest -m network` → **4 passed**;
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
| Q1 | Linux desktop launcher | 7 | partial | packaging/install.sh works (user-run). AppImage built + self-tested offscreen (packaging/build_appimage.sh); desktop placement pending |
| Q3 | Clear startup failure when a Qt system lib is missing | 6 | done+tested | tests/core/test_platform_check.py (8); lead ran it against the real missing lib → message + exit 1 |
| Q2 | No DRM/authorization bypass | all | done+tested | Go+ preview-only and DRM formats → AuthRequiredError (network test); cookies only pass the user's own login |
