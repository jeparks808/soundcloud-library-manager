# Development Status

_Last updated: 2026-11-02_

## Environment (verified)

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Zena), kernel 6.8, x86_64, 4 threads, 7.7 GiB RAM |
| Python | 3.12.3 (system), project venv at `.venv` |
| PySide6 | 6.12.0 (venv) |
| yt-dlp | 2026.10.26 (venv). The system package 2024.04.09 is too old for SoundCloud and isn't used |
| Mutagen | 1.48.1 (venv) |
| Beets | 2.14.1 (venv, optional at runtime) |
| FFmpeg | **No system ffmpeg.** Using the imageio-ffmpeg bundled binary 7.0.2 as a fallback. Recommend `sudo apt install ffmpeg` |
| Display | X11 (`DISPLAY=:0`) |
| Coding agent | Claude Code 2.1.316 (the active platform). Codex CLI 0.166.0 is installed but not running |

## Model / effort orchestration

### Claude Code (active platform)

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

### Codex (not active)

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
