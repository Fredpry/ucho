# AGENTS.md: Ucho

Instructions for AI coding agents (Claude Code, Codex, Cursor and similar) working on this repository. Human-oriented docs: `README.md`, `docs/architecture.md`.

## Three rules

1. **The app stays offline.** Audio, transcripts, settings and logs never leave the computer. The only network use is the explicit model download from Hugging Face. Do not add network calls, telemetry, crash reporting, external AI APIs, Ollama or background servers. Do not add PyTorch.
2. **No real user data.** Never put real recordings, transcripts or names from meetings into code, tests, commits or issues. Tests use synthetic data.
3. **Every user-facing string goes through `t("...")`** with English source text, and gets a Polish entry in `ucho/locale/pl.json`. `tests/test_i18n.py` enforces this.

## Map

| Path | Purpose |
|---|---|
| `ucho.pyw`, `dictate.pyw` | Start scripts used by shortcuts; work without a working directory |
| `mcp_server.py` | Start script for the MCP server (same trick) |
| `install.ps1` | Installer: Python, `.venv`, pinned deps, telemetry opt-out, icon, settings (migrates a v0.1 `ustawienia.json`), hardware check and recommended model (`ucho.hardware --save`), shortcuts. ASCII only (PowerShell 5.1 reads it as ANSI). Keys written to `settings.json` must match `config.DEFAULTS` |
| `ucho/hardware.py` | Hardware profile (RAM, CPU, OpenVINO devices), rule table for model and device, benchmark on a synthetic signal, `python -m ucho.hardware [--save] [--benchmark]`. See `docs/hardware.md` |
| `ucho/config.py` | Paths, model lists, pinned model revisions (`REVISIONS`), defaults (`DEFAULTS`), `Settings`, migration of v0.1 settings keys |
| `ucho/i18n.py`, `ucho/locale/pl.json` | UI translations (English source text as key, Polish as value) |
| `ucho/app.py` | Main window (customtkinter): record, transcription view, recordings list, settings |
| `ucho/audio.py` | Two-track recording (PyAudioWPatch WASAPI loopback), resampling to 16 kHz, incremental FLAC |
| `ucho/transcribe.py` | Whisper via OpenVINO GenAI: silence cutting, per-chunk language detection restricted to `config.LANGUAGES`, GPU to CPU fallback |
| `ucho/merge.py` | `Segment`, speaker codes and labels, merging tracks, hallucination filter, `transcript.md/.srt/.txt`, `meta.json` |
| `ucho/session.py` | The one reader of session folders, for the app and the MCP server; maps v0.1 Polish file names and `meta.json` keys to the current ones |
| `ucho/summary.py` | Optional LLM summary: JSON schema output, map-reduce for long meetings, name and date filters, folder naming |
| `ucho/dictate.py` | Dictation window: global hotkey via `RegisterHotKey`, in-memory recording, paste into the active window |
| `ucho/tray.py`, `ucho/winshell.py`, `ucho/icon.py` | Tray icon and Ctrl+Alt+R, taskbar AppUserModelID via COM, icon drawing |
| `ucho_mcp/server.py` | Read-only stdio MCP server, standard library only |
| `tests/` | pytest, no models or audio needed |

## Change freely

- Model lists in `config.py` (OpenVINO IR repos only). Add the pinned revision to `REVISIONS` and update `THIRD_PARTY.md`.
- Output formats in `merge.py`, hallucination patterns `_HALLUCINATIONS`.
- Summary prompts and limits in `summary.py` (if you change `SUMMARY_SCHEMA`, update `_normalize` and `as_markdown`).
- GUI layout and colors in `app.py`.

## Change carefully (read the comments first)

- `transcribe.py` silence thresholds and language detection. Restricting `lang_to_id` fixed 8 of 8 bad chunks; `suppress_tokens` fixed 1 of 8. Change only after testing on a real recording.
- `summary.py` filters `_names`, `_name_in_text`, `_without_invented_dates`. They exist because prompts alone did not work. Whole-word comparison is deliberate.
- Model memory order: Whisper is released before the LLM loads; both must never sit on the iGPU at once.
- `audio.py` threads and drift tolerance; `winshell.py` COM vtable indices.
- Import order in `config.py`: `HF_HOME` and telemetry variables are set before anything imports `huggingface_hub`. Import modules through the package (`from ucho import ...`).
- Backward compatibility: v0.1 archives (`mikrofon.flac`, `transkrypt.md`, `podsumowanie.md`, Polish `meta.json` keys) must keep working. Read sessions only through `ucho/session.py`; new mappings go into its `LEGACY_*` tables, settings keys into `config.LEGACY_KEYS`.

## Commands

```powershell
.\.venv-dev\Scripts\python.exe -m pytest            # tests
.\.venv-dev\Scripts\python.exe -m ruff check .      # lint
.\.venv\Scripts\python.exe -m ucho                  # app with a console (after install.ps1)
Get-Content .\ucho.log -Tail 100                     # log (pythonw has no console)
```

## Style

All code is in English: file and script names, identifiers, comments, docstrings, log messages, CLI flags, settings keys and installer switches. Polish appears only in Polish-facing content (`ucho/locale/pl.json`, `README.pl.md`, `docs/usability/`, the Polish halves of the installer messages) and in Polish speech data (hallucination patterns, month names, honorifics and the Polish summary prompts). Comments explain *why* and record measurements. `from __future__ import annotations`, type hints, `logging`, no `print` in app code. New source files start with the SPDX header (`tools/set_copyright.py` adds it).
