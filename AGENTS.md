# AGENTS.md: Ucho

Instructions for AI coding agents (Claude Code, Codex, Cursor and similar) working on this repository. Human-oriented docs: `README.md`, `docs/architecture.md`.

## Three rules

1. **The app stays offline.** Audio, transcripts, settings and logs never leave the computer. The only network use is the explicit model download from Hugging Face. Do not add network calls, telemetry, crash reporting, external AI APIs, Ollama or background servers. Do not add PyTorch.
2. **No real user data.** Never put real recordings, transcripts or names from meetings into code, tests, commits or issues. Tests use synthetic data.
3. **Every user-facing string goes through `t("...")`** with Polish source text, and gets an English entry in `ucho/locale/en.json`. `tests/test_i18n.py` enforces this.

## Map

| Path | Purpose |
|---|---|
| `uruchom.pyw`, `dyktuj.pyw` | Start scripts used by shortcuts; work without a working directory |
| `mcp_serwer.py` | Start script for the MCP server (same trick) |
| `instaluj.ps1` | Installer: Python, `.venv`, pinned deps, telemetry opt-out, icon, GPU check, model, settings, shortcuts. ASCII only (PowerShell 5.1 reads it as ANSI). Keys written to `ustawienia.json` must match `config.DOMYSLNE` |
| `ucho/config.py` | Paths, model lists, pinned model revisions (`REWIZJE`), defaults, `Ustawienia` |
| `ucho/i18n.py`, `ucho/locale/en.json` | UI translations (Polish source text as key) |
| `ucho/app.py` | Main window (customtkinter): record, transcription view, recordings list, settings |
| `ucho/audio.py` | Two-track recording (PyAudioWPatch WASAPI loopback), resampling to 16 kHz, incremental FLAC |
| `ucho/transcribe.py` | Whisper via OpenVINO GenAI: silence cutting, per-chunk language detection restricted to `config.JEZYKI`, GPU to CPU fallback |
| `ucho/merge.py` | `Segment`, merging tracks, hallucination filter, `transkrypt.md/.srt/.txt`, `meta.json` |
| `ucho/podsumowanie.py` | Optional LLM summary: JSON schema output, map-reduce for long meetings, name and date filters, folder naming |
| `ucho/dyktuj.py` | Dictation window: global hotkey via `RegisterHotKey`, in-memory recording, paste into the active window |
| `ucho/tray.py`, `ucho/winshell.py`, `ucho/ikona.py` | Tray icon and Ctrl+Alt+R, taskbar AppUserModelID via COM, icon drawing |
| `ucho_mcp/serwer.py` | Read-only stdio MCP server, standard library only |
| `tests/` | pytest, no models or audio needed |

## Change freely

- Model lists in `config.py` (OpenVINO IR repos only). Add the pinned revision to `REWIZJE` and update `THIRD_PARTY.md`.
- Output formats in `merge.py`, hallucination patterns `_HALUCYNACJE`.
- Summary prompts and limits in `podsumowanie.py` (if you change `SCHEMAT_PODSUMOWANIA`, update `_normalizuj` and `jako_markdown`).
- GUI layout and colors in `app.py`.

## Change carefully (read the comments first)

- `transcribe.py` silence thresholds and language detection. Restricting `lang_to_id` fixed 8 of 8 bad chunks; `suppress_tokens` fixed 1 of 8. Change only after testing on a real recording.
- `podsumowanie.py` filters `_imiona`, `_pada_w_tekscie`, `_bez_zmyslonych_dat`. They exist because prompts alone did not work. Whole-word comparison is deliberate.
- Model memory order: Whisper is released before the LLM loads; both must never sit on the iGPU at once.
- `audio.py` threads and drift tolerance; `winshell.py` COM vtable indices.
- Import order in `config.py`: `HF_HOME` and telemetry variables are set before anything imports `huggingface_hub`. Import modules through the package (`from ucho import ...`).

## Commands

```powershell
.\.venv-dev\Scripts\python.exe -m pytest            # tests
.\.venv-dev\Scripts\python.exe -m ruff check .      # lint
.\.venv\Scripts\python.exe -m ucho                  # app with a console (after instaluj.ps1)
Get-Content .\ucho.log -Tail 100                     # log (pythonw has no console)
```

## Style

Identifiers, comments and docstrings in Polish without diacritics; comments explain *why* and record measurements. `from __future__ import annotations`, type hints, `logging`, no `print` in app code. New source files start with the SPDX header (`tools/set_copyright.py` adds it).
