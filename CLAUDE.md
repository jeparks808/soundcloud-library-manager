# SoundCloud Library Manager — project instructions

Linux desktop app (PySide6) that downloads **authorized** SoundCloud tracks/playlists with the
yt-dlp Python API, converts them to MP3 with FFmpeg, tags them with Mutagen (optional Beets),
detects duplicates, and organizes the library. History and duplicate tracking live in SQLite.

Target: Linux Mint 22.x, MacBook Air 2015 (i5-5350U, 8 GB RAM, 1440×900). Keep it lightweight.

## Layout
- `src/sclm/core/`      data models, settings, logging, SQLite storage, errors, service interfaces
- `src/sclm/download/`  playlist retrieval, queue/workers, yt-dlp integration, FFmpeg location/conversion
- `src/sclm/metadata/`  ID3 tags, artwork, filenames, organization, duplicates, Beets bridge
- `src/sclm/gui/`       PySide6 presentation layer only (no business logic)
- `tests/`              pytest (+ pytest-qt); no network required for the default suite
- `packaging/`          .desktop launcher and install script
- `docs/ARCHITECTURE.md` is the source of truth for interfaces. Read it before changing modules.

## Commands
- Run app:   `.venv/bin/python -m sclm`
- Tests:     `.venv/bin/pytest -q` (Qt tests use `QT_QPA_PLATFORM=offscreen`, set in conftest)
- Network tests (opt-in): `SCLM_NETWORK_TESTS=1 .venv/bin/pytest -q -m network`

## Rules
- Never bypass DRM, SoundCloud authorization, or access restrictions. Unavailable or
  restricted tracks are reported as errors, never worked around.
- Never overwrite, move, or delete user files without an explicit user-confirmed path.
- Downloads and file I/O never run on the Qt GUI thread.
- FFmpeg lookup order: settings path → `PATH` → `imageio-ffmpeg` bundled binary.
- Don't add speculative features that complicate the core flow.

## Agent workflow (Claude Code)
Stage agents live in `.claude/agents/` with explicit `model`/`effort`:
architect (opus/high), gui (sonnet/high), downloader (opus/high), metadata (opus/high),
debugger (opus/xhigh), improver (sonnet/medium), reviewer (opus/high).
The lead engineer (main session) owns `DEVELOPMENT_STATUS.md` and all git commits.
Subagents must not edit either. Run stages in order; run them concurrently only when
their files can't conflict.

## Independent evaluation rules
This project is developed independently for comparison with a separate implementation.
- Work only inside this project directory. Never inspect, reference, or copy any other implementation.
- `REQUIREMENTS_CHECKLIST.md` tracks each requirement's state: implemented / tested / partial /
  incomplete. Mark something "tested" only when a test that exercises it was actually run and passed.
- Record real test output. Never claim verification that didn't happen. Keep automated test results
  separate from manual GUI checks.
- At project end, write `EVALUATION_REPORT.md`: implemented and missing features, tests executed, known
  bugs, performance measurements, and model/effort settings actually verified.
