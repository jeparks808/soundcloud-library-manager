# Roadmap

Every stage ends with `.venv/bin/pytest -q` green (offline) and a report from the stage agent
listing files changed, test counts and known limitations. The lead engineer commits after
each checkpoint.

| # | Stage | Agent | Can run in parallel with |
|---|---|---|---|
| 1 | Architecture and core | architect | (none) |
| 2 | GUI | gui | 3, 4 |
| 3 | Downloader | downloader | 2, 4 |
| 4 | Metadata and organization | metadata | 2, 3 |
| 5 | Integration and debugging | debugger | (none) |
| 6 | Polish, performance and packaging | improver | (none) |
| 7 | Review and release | reviewer | (none) |

## 1. Architecture and core (done when)
- `docs/ARCHITECTURE.md` defines layout, data flow, threading, errors, schema, settings and
  exact public signatures.
- `core/` is fully implemented and tested: models, errors, settings, paths, logging, ffmpeg
  resolver, storage.
- Stubs exist for every cross-package interface. `pip install -e .` works.
  `python -m sclm --version` works.

## 2. GUI: `src/sclm/gui/` only
- `MainWindow(ctx)` is built with a dark theme and orange accents, and is usable at
  1440×900 and 1280×800.
- The preview panel, queue view (cancel, retry, keep both, clear), history, library (search,
  scan), metadata editor dialog and settings dialog (validation, ffmpeg info, filename
  preview) all work.
- `run_in_background` is implemented. No blocking call runs on the GUI thread.
- pytest-qt tests run against a fake manager. The GUI tolerates `NotImplementedError` from
  stage 3/4 functions until they land.

## 3. Downloader: `src/sclm/download/` only
- `fetch_preview` works for track, set, album, user tracks and likes URLs, and rejects
  non-SoundCloud URLs.
- `DownloadManager` implements the full state machine, bounded concurrency, cooperative
  cancel, network retry with backoff, enqueue dedupe and shutdown.
- yt-dlp errors are classified per ARCHITECTURE.md §7. Restricted content is never retried or worked around.
- Tests use a fake YoutubeDL and fake finalize/find_duplicate, plus one offline `file://`
  conversion through real ffmpeg.

## 4. Metadata: `src/sclm/metadata/` only
- `finalize_track` tags (ID3v2.3, APIC, TXXX id), checks the content hash and organizes per
  mode, with no overwrites ever.
- `find_duplicate`, `audio_sha256`, `render_filename`, `sanitize_component`,
  `normalize_genre`, `scan_library` and `editor.read_tags`/`save_tags` are implemented.
- Beets is an optional best-effort step. Tests cover no-overwrite, EXDEV and the conflict
  policies.

## 5. Integration and debugging
- `python -m sclm` runs end to end. A real public playlist downloads, gets tagged, is
  organized and recorded. Re-fetching it shows zero new tracks.
- The offline integration test passes (fake yt-dlp → real finalize → DB). Cross-stage
  contract mismatches are fixed and recorded in ARCHITECTURE.md.

## 6. Polish, performance and packaging
- Idle CPU stays near 0%. Memory stays under about 200 MB with 4 parallel downloads on the
  target laptop. Startup takes under 3 s.
- `packaging/install.sh` and `packaging/sclm.desktop.in` (already drafted) are verified on
  Linux Mint 22.
- UX papercuts are fixed, error messages are reviewed, and a README covers install and use.

## 7. Review and release
- A full code review against CLAUDE.md rules is complete: no DRM bypass, no overwrite or
  delete paths, no GUI-thread I/O.
- The full test suite passes, the network suite passes once, and the version is tagged
  0.1.0.
