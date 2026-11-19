# AGENTS.md: Ucho

Instructions for AI coding agents (Claude Code, Codex, Cursor and similar) working on this repository. Human-oriented docs: `README.md`, `docs/architecture.md`.

## Three rules

1. **The app stays offline.** Audio, transcripts, settings and logs never leave the computer. The only network use is the explicit model download from Hugging Face. Do not add network calls, telemetry, crash reporting, external AI APIs, Ollama or background servers. Do not add PyTorch.
2. **No real user data.** Never put real recordings, transcripts or names from meetings into code, tests, commits or issues. Tests use synthetic data.
3. **Every user-facing string goes through `t("...")`** with English source text, and gets a Polish entry in `ucho/locale/pl.json`. `tests/test_i18n.py` enforces this.

## Commits

- Commit as the maintainer, with a DCO sign-off (`git commit -s`). Do not add `Co-Authored-By` trailers or other tool attribution.
- Dependabot pull requests are proposals only. Never merge them with the GitHub button; test the update, apply it in a maintainer commit (several updates may share one) and close the PR with a link to that commit.

## Map

| Path | Purpose |
|---|---|
| `ucho.pyw`, `dictate.pyw` | Start scripts used by shortcuts; work without a working directory. `dictate.pyw` = `ucho.pyw --dictate` (kept for old shortcuts) |
| `mcp_server.py` | Start script for the MCP server (same trick) |
| `install.cmd`, `install.ps1` | Installer (`install.cmd` is the double-click wrapper for `install.ps1 -Pause`; Setup passes `-PauseOnError`, waits only after a failure; the run is logged to `install.log`; prints the MCP command at the end): Python, `.venv`, pinned deps, telemetry opt-out, icons, settings (migrates a v0.1 `ustawienia.json`), hardware check and recommended model (`ucho.hardware --save`), shortcuts "Ucho" and "Ucho Dictate" (both start `ucho.pyw`, one AppUserModelID). ASCII only (PowerShell 5.1 reads it as ANSI). Keys written to `settings.json` must be in `config.DEFAULTS` (tested) |
| `uninstall.cmd`, `uninstall.ps1` | Uninstaller: removes shortcuts and the autostart entry that point to this folder, then (asked) `.venv`, models, settings and logs; recordings only after a typed confirmation. ASCII only |
| `ucho/hardware.py` | Hardware profile (RAM, CPU, OpenVINO devices), rule table for model and device, benchmark on a synthetic signal, `python -m ucho.hardware [--save] [--benchmark]`. See `docs/hardware.md` |
| `ucho/config.py` | Paths, model lists, pinned model revisions (`REVISIONS`), defaults (`DEFAULTS`), `Settings`, migration of v0.1 settings keys and of `dictate.json`/`dyktuj.json` into `settings.json` |
| `ucho/i18n.py`, `ucho/locale/pl.json` | UI translations (English source text as key, Polish as value) |
| `ucho/app.py` | The one app process: window and the two layouts (`single`: one window with Meeting/Dictation, `tray`: tray hub + small dictation window), tray icon, hotkeys, single instance, Jump List, taskbar progress, `--dictate/--record/--minimized`, `--smoke-test` |
| `ucho/engine.py` | Shared Whisper engine: one worker thread, priority queue (dictation first), meeting transcription as step jobs between chunks, pipelines cached by (model, device), `exclusive()` for summary and benchmark |
| `ucho/meeting.py`, `ucho/dictation.py` | Controllers without widgets: meeting recording/transcription/summary; dictation (microphone, paste/type into the active window) |
| `ucho/downloads.py` | Model downloads with progress (a tqdm stand-in for `snapshot_download(tqdm_class=...)` feeding a thread-safe byte counter, `Downloads` with one download per model) and the speech model gate: `speech_readiness`/`block_reason` decide whether a meeting or dictation may start |
| `ucho/events.py` | `Emitter`: the listener list and UI-thread event delivery shared by both controllers |
| `ucho/live.py` | Live dictation: cuts the microphone stream at pauses (`PieceCutter`) and recognizes each piece on the engine while you speak (`LiveDictation`); language pinned after the first piece, the previous text as Whisper's prompt, filters only on the joined text |
| `ucho/live_meeting.py` | Live meeting transcription: cuts the growing tracks with the same functions as the transcription after the stop (`TrackChunker`), recognizes final chunks on the engine while recording (`LiveMeeting`, one chunk at a time, waits while a dictation is spoken, gives up when it falls behind), keeps them in a `ChunkCache` that the transcription after the stop uses only for chunks with the same range and samples |
| `ucho/screens.py`, `ucho/settings_screen.py`, `ucho/wizard.py` | Screens: Meeting, Dictation (+ small window), Recordings with search, Transcript; Settings with the Hardware panel; first-run wizard |
| `ucho/theme.py`, `ucho/ui.py` | Theme from `design/tokens.json` (light/dark pairs), fonts, WCAG checks; components (buttons, status pill, PIL-drawn record button, transcript text) and keyboard focus (`ui.focusable`) |
| `ucho/archive.py` | Archive listing and full-text search (SQLite FTS5), shared by the recordings list and the MCP server |
| `ucho/retention.py` | Deleting a session folder and the audio retention setting. Every path goes through `session_folder()` (a direct, non-link child of the recordings folder); logs carry counts, never names |
| `ucho/hotkeys.py`, `ucho/instance.py` | Global hotkeys via `RegisterHotKey` (busy hotkeys are reported, not fatal); single instance via a named mutex and events |
| `ucho/audio.py` | Two-track recording (PyAudioWPatch WASAPI loopback), resampling to 16 kHz, incremental FLAC |
| `ucho/transcribe.py` | Whisper via OpenVINO GenAI: silence cutting, per-chunk language detection restricted to `config.LANGUAGES`, GPU to CPU fallback; `create_engine` picks Whisper or Parakeet for a model id |
| `ucho/parakeet.py` | Optional Parakeet backend (sherpa-onnx, CPU only, `install.ps1 -WithParakeet`): same surface as `transcribe.Engine`, Whisper-shaped results, splits long chunks into 20 s pieces. Offered only when `sherpa_onnx` is installed |
| `ucho/diarize.py` | Optional speaker detection inside a track (`install.ps1 -WithSpeakers`): sherpa-onnx diarization in a short-lived worker process, largest-overlap assignment to Whisper segments, codes `meeting:N`/`me:N`; off by default |
| `ucho/merge.py` | `Segment`, speaker codes and labels, merging tracks, hallucination filter, `transcript.md/.srt/.txt`, `meta.json` |
| `ucho/session.py` | The one reader of session folders, for the app and the MCP server; maps v0.1 Polish file names and `meta.json` keys to the current ones |
| `ucho/llm.py` | Analysis language model choice (`analysis_model`, shared by summaries and automatic titles), derivation from v0.4 settings, validation of custom OpenVINO IR folders and `.gguf` files (header quantization check), privacy-safe `label()` for `meta.json` and logs |
| `ucho/summary.py` | Optional LLM summary: JSON schema output, map-reduce for long meetings, name and date filters, folder naming |
| `ucho/titles.py` | Automatic meeting titles: small LLM pass on an excerpt, filters, `wants_title`; run by meeting.py after the transcription queue is empty |
| `ucho/dictate.py` | Compatibility: `main()` opens dictation in the app, reading the old `dictate.json`, last dictated text from the log |
| `ucho/diagnostics.py`, `ucho/error_dialog.py` | Error recognition (`classify`, by type name/errno/winerror, no heavy imports), log redaction, the diagnostics ZIP, mailto draft and dialog throttle (no Tk); the error dialog, the package dialog and the global exception hooks (`install`, `show_error`). A new log line that writes user text needs a marker in `diagnostics._USER_TEXT` |
| `ucho/tray.py`, `ucho/winshell.py`, `ucho/icon.py` | Tray icon; taskbar AppUserModelID, Jump List, progress and autostart via COM/registry; the ear icon drawn from the design mark |
| `ucho_mcp/server.py` | Read-only stdio MCP server, standard library only |
| `tests/` | pytest, no models or audio needed |
| `tools/screenshots.py`, `tools/benchmark_engine.py`, `tools/benchmark_diarization.py` | Screenshots of the real windows on synthetic data (`docs/screens`); dictation latency during a transcription and the TTS set for WER, on synthetic speech; speaker-detection benchmark (DER and per-utterance speaker accuracy) on synthetic multi-voice TTS meetings |

## Change freely

- Model lists in `config.py` (OpenVINO IR repos, plus the one optional Parakeet entry with `"backend": "parakeet"`). Add the pinned revision to `REVISIONS` and update `THIRD_PARTY.md`. Never make Parakeet the default or a hardware recommendation without a WER check on real meetings (`docs/benchmarks.md`).
- Output formats in `merge.py`, hallucination patterns `_HALLUCINATIONS`.
- Summary prompts and limits in `summary.py` (if you change `SUMMARY_SCHEMA`, update `_normalize` and `as_markdown`).
- Screen layouts in `screens.py`, `settings_screen.py`, `wizard.py`. Colors come from `design/tokens.json` through `theme.py`; do not hard-code hex colors in screens. Check the result with `tools/screenshots.py` (also at `--size 1093x535`, a 1366x768 screen at 125 %).

## Change carefully (read the comments first)

- `transcribe.py` silence thresholds and language detection. Restricting `lang_to_id` fixed 8 of 8 bad chunks; `suppress_tokens` fixed 1 of 8. Change only after testing on a real recording. Live meeting transcription (`live_meeting.py`) cuts with the same functions (`split_into_chunks`, `ranges_at_boundaries`, `boundaries_from_energy`); keep its tests green so live results stay reusable.
- `summary.py` filters `_names`, `_name_in_text`, `_without_invented_dates` and `titles.py` filters (`clean_title`). They exist because prompts alone did not work. Whole-word comparison is deliberate.
- `diarize.py` and speaker names: diarization only relabels speaker codes (`meeting:N`, `me:N`) and never changes segment text or start/end times; names of numbered speakers are dropped on "Transcribe again" because a new run may number voices differently.
- Model memory order: Whisper is released before the LLM loads; both must never sit on the iGPU at once. `engine.exclusive()` enforces it; run anything that loads a model through the engine.
- `engine.py`: every OpenVINO call runs on the engine thread. Long work must be a step generator, or a dictation waits for all of it.
- PortAudio init and terminate go through `audio.new_pyaudio()` / `audio.close_pyaudio()` (one lock): two threads creating PyAudio at once crashed the process.
- `audio.py` threads and drift tolerance; `winshell.py` COM vtable indices.
- Import order in `config.py`: `HF_HOME` and telemetry variables are set before anything imports `huggingface_hub`. Import modules through the package (`from ucho import ...`).
- Backward compatibility: v0.1 archives (`mikrofon.flac`, `transkrypt.md`, `podsumowanie.md`, Polish `meta.json` keys) must keep working. Read sessions only through `ucho/session.py`; new mappings go into its `LEGACY_*` tables, settings keys into `config.LEGACY_KEYS`.

## Commands

```powershell
.\.venv-dev\Scripts\python.exe -m pytest            # tests
.\.venv-dev\Scripts\python.exe -m ruff check .      # lint
.\.venv\Scripts\python.exe -m ucho                  # app with a console (after install.ps1)
.\.venv\Scripts\python.exe -m ucho --smoke-test     # GUI start test: no audio, no model; exit 1 on any logged error
Get-Content .\ucho.log -Tail 100                     # log (pythonw has no console)
```

## Style

All code is in English: file and script names, identifiers, comments, docstrings, log messages, CLI flags, settings keys and installer switches. Polish appears only in Polish-facing content (`ucho/locale/pl.json`, `README.pl.md`, `docs/usability/`, the Polish halves of the installer messages) and in Polish speech data (hallucination patterns, month names, honorifics and the Polish summary prompts). Comments explain *why* and record measurements. `from __future__ import annotations`, type hints, `logging`, no `print` in app code. New source files start with the SPDX header (`tools/set_copyright.py` adds it).
