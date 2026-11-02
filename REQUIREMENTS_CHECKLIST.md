# Requirements Checklist

States: **done+tested** (automated test run and passed) · **done (untested)** · **manual** (verified by
hand in the GUI only) · **partial** · **todo**. Updated by the lead engineer after each stage.

| # | Requirement | Stage | State | Evidence |
|---|---|---|---|---|
| A1 | Modular components: GUI, playlist retrieval, queue/workers, conversion, metadata, organization, storage, settings/logging | 1 | partial | core/ implemented + tested (112 tests); download/, metadata/, gui/ are stubs pending Stages 2–4 |
| A2 | Interfaces, error handling, data models, test strategy, dependency mgmt documented | 1 | done | docs/ARCHITECTURE.md §1–16, pyproject.toml, requirements*.txt |
| G1 | Dark theme with orange accents | 2 | todo | |
| G2 | SoundCloud URL input | 2 | todo | |
| G3 | Playlist preview | 2 | todo | |
| G4 | Track selection | 2 | todo | |
| G5 | Download settings (bitrate, destination, new-only) | 2 | todo | |
| G6 | Live progress indicators | 2 | todo | |
| G7 | Queue management (cancel/retry/clear) | 2 | todo | |
| G8 | Library browser | 2 | todo | |
| G9 | Metadata editor | 2 | todo | |
| G10 | Application settings dialog | 2 | todo | |
| G11 | Usable at 1440×900 | 2 | todo | |
| D1 | yt-dlp Python API integration | 3 | todo | |
| D2 | Individual tracks | 3 | todo | |
| D3 | Entire playlists | 3 | todo | |
| D4 | Per-track progress | 3 | todo | |
| D5 | MP3 conversion | 3 | todo | |
| D6 | Configurable bitrate | 3 | todo | |
| D7 | Artwork + metadata embedding | 3/4 | todo | |
| D8 | Cancellation | 3 | todo | |
| D9 | Retry handling | 3 | todo | |
| D10 | Persistent download history | 3 | partial | storage layer done+tested (tests/core/test_storage.py); manager wiring pending |
| D11 | Download only newly added tracks | 3 | todo | |
| D12 | Downloads don't block GUI event loop | 2/3 | todo | |
| M1 | ID3 metadata editing | 4 | todo | |
| M2 | Artist/title/album extraction | 4 | todo | |
| M3 | Embedded cover artwork | 4 | todo | |
| M4 | Consistent filenames | 4 | todo | |
| M5 | Duplicate detection | 4 | todo | |
| M6 | Organize by playlist/artist/album/genre | 4 | todo | |
| M7 | Optional Beets integration | 4 | todo | |
| M8 | Safe handling of missing/unreliable genre | 4 | todo | |
| M9 | Never overwrite user files without permission | 4 | todo | |
| R1 | Qt threading/signal handling | 5 | todo | |
| R2 | Interrupted downloads | 5 | todo | |
| R3 | FFmpeg errors | 5 | todo | |
| R4 | Malformed filenames | 5 | todo | |
| R5 | Unavailable tracks | 5 | todo | |
| R6 | Missing metadata | 5 | todo | |
| R7 | Duplicate files | 5 | todo | |
| R8 | Playlist changes | 5 | todo | |
| R9 | Network interruptions | 5 | todo | |
| R10 | Database consistency | 5 | partial | WAL, RLock, schema versioning, tested in tests/core/test_storage.py |
| I1 | UI performance / resource use | 6 | todo | |
| I2 | Accessibility / usability | 6 | todo | |
| Q1 | Linux desktop launcher | 7 | done (untested) | `packaging/install.sh`, `packaging/sclm.desktop.in` |
| Q2 | No DRM/authorization bypass | all | todo | |
