# Changelog

## Unreleased (v0.2)

### Added

- Hardware detection and automatic model choice (`ucho/hardware.py`, `docs/hardware.md`). `python -m ucho.hardware` prints the hardware profile and a recommended Whisper model, device and dictation model with the reasons; `--benchmark` measures the real-time factor on a synthetic signal and steps one row down below 1.5x; `--save` writes the result to the settings.
- New settings keys `dictation_model` and `hardware_profile`.
- `ucho/session.py`: one reader of recording folders for the app and the MCP server, for both the current and the v0.1 format.
- **New interface on the design system** (W2). Theme from `design/tokens.json`, light and dark, following the Windows app theme unless set in Settings. Sidebar with Meeting, Dictation, Recordings and Settings. Recording screen with a status pill (Ready / Recording / Transcribing), a round REC button in the action color that turns clay red with a slow pulse while recording, timer, both level meters, meeting name and the last three recordings. Recordings list with search in titles, summaries and all transcripts (diacritics-insensitive). Transcript view with monospaced timestamps, speaker colors, *Copy all* and *Open folder*. Screenshots in `docs/screens/`.
- **First-run wizard**: interface language, recordings folder (warns about OneDrive), hardware profile and recommendation with an optional speed measurement (`hardware.benchmark` / `after_benchmark`), note on consent and headphones.
- **Settings → Hardware**: profile, recommendation with reasons, *Measure again*, device GPU / CPU / NPU (experimental, asks first: the first compile takes about 3 minutes) / AUTO, transcription model with download.
- **Keyboard access**: every control is reachable with Tab, Enter/Space activate it, a focus ring in the focus color shows where you are. Layout fits a 1366x768 screen at 125 %.
- **One app for meetings and dictation** (W3): one process, one shared Whisper engine (`ucho/engine.py`, a worker thread with a priority queue; a meeting transcription yields after every chunk, so a 10 s dictation is answered in 1.2-1.7 s while a meeting transcribes, measured), one tray icon, both global hotkeys always active (Ctrl+Alt+R, Ctrl+Shift+Space, Esc while dictating), single instance (a second start shows the running window), one AppUserModelID, Jump List tasks *Record a meeting* and *Dictate*, taskbar progress, optional start with Windows.
- Window layout setting: one window with Meeting and Dictation modes (default) or the tray icon as the hub with a small separate dictation window.
- Dictation model setting: the transcription model (one model in memory) or a separate one; option to keep it loaded. Default from the hardware recommendation.
- `python -m ucho --smoke-test` (W4d): builds the whole UI without audio capture or a model, walks through every screen, both layouts and both themes, and exits with 1 when anything logged an error. CI runs it on windows-latest with `requirements.txt`.
- `python -m ucho --dictate | --record | --minimized`.
- `tools/screenshots.py` (window screenshots on synthetic data) and `tools/benchmark_engine.py` (dictation latency during a transcription and a 20-sentence TTS set for WER, synthetic speech from the offline Windows voice). Results in `docs/benchmarks.md`.
- `ucho/archive.py`: the archive search of the MCP server, now shared with the app.
- **Optional Parakeet backend** (W6): NVIDIA Parakeet TDT 0.6B v3 (fp32 ONNX, CC BY 4.0) through sherpa-onnx, CPU only. Not installed by default: `install.ps1 -WithParakeet` adds the pinned `sherpa-onnx` package (`requirements-parakeet.txt`); the model (~2.5 GB, pinned revision) is then offered in Settings → Hardware and downloaded on demand. Same transcript segments as Whisper, so the transcript files, search and MCP server do not change. Measured on synthetic Polish speech: within 1 pp WER of Whisper large-v3 and 3 to 10 times faster on the CPU, but clearly worse with English phrases in Polish sentences; never recommended automatically, Whisper stays the default. The int8 export was rejected (7.6 % vs 2.7 % WER). Details in `docs/benchmarks.md`.
- `tools/benchmark_wer.py --backend parakeet [--threads N]`; the JSON report also records the backend and the peak working set.

### Changed

- Models can be downloaded partially (`config.DOWNLOAD_PATTERNS`, used by `transcribe.model_path` both for the download and the offline check).

- `dictate.pyw` and the *Ucho Dictate* shortcut start Ucho with dictation open (or open it in the running Ucho). The installer creates *Ucho* and *Ucho Dictate* shortcuts on the Desktop and in the Start menu, both with the Ucho AppUserModelID.
- Dictation settings live in `settings.json` (`dictation_language`, `dictation_enter`, `dictation_mode`, `dictation_sounds`, `dictation_max_s`, `dictation_dictionary`, `dictation_preload`, `dictation_hotkey`); dictation logs to `ucho.log`. New keys `theme`, `layout`, `start_with_windows`, `setup_complete`.
- The record hotkey uses `RegisterHotKey` instead of a pynput keyboard listener: other programs no longer also receive Ctrl+Alt+R, and a combination taken by another program is shown in the window. `pynput` is no longer a dependency.
- New app icon drawn from the ear mark of the design system.
- PortAudio is initialized and closed under one lock (two threads doing it at once crashed the process in the merged app).

- The installer runs `python -m ucho.hardware --save` after installing the packages and downloads the recommended model instead of always large-v3 INT8. Re-running it keeps model and device choices made by hand (`--keep-choice`).
- **All code is in English** (workstream W0). English is the UI source language; the Polish translation moved to `ucho/locale/pl.json` (`en.json` is gone). Renamed:
  - scripts: `instaluj.ps1` → `install.ps1` (switches `-SkipModel`, `-WithSummary`, `-ReinstallPackages`), `uruchom.pyw` → `ucho.pyw`, `dyktuj.pyw` → `dictate.pyw`, `mcp_serwer.py` → `mcp_server.py`, `Ucho (diagnostyka).cmd` → `Ucho (diagnostics).cmd`;
  - modules: `ucho/dyktuj.py` → `dictate.py`, `ikona.py` → `icon.py`, `podsumowanie.py` → `summary.py`, `ucho_mcp/serwer.py` → `server.py`;
  - files: `ustawienia.json` → `settings.json`, `dyktuj.json` → `dictate.json`, `dyktuj.log` → `dictate.log`, `modele\` → `models\`, default recordings folder `~\Ucho\nagrania` → `~\Ucho\recordings`, Dictate shortcuts `Ucho Dyktuj.lnk` → `Ucho Dictate.lnk`;
  - settings keys: `katalog_nagran` → `recordings_dir`, `jezyk` → `language`, `jezyk_interfejsu` → `ui_language`, `sciezki` (`obie/mikrofon/system`) → `tracks` (`both/microphone/system`), `mikrofon_index` → `microphone_index`, `transkrybuj_po_stopie` → `transcribe_after_stop`, `podsumowanie` → `summary_enabled`, `model_llm` → `summary_model`, `zmiana_nazwy` → `rename_folder`; Dictate: `jezyk` → `language`, `tryb` (`wklej/pisz`) → `mode` (`paste/type`), `dzwieki` → `sounds`, `maks_nagranie_s` → `max_recording_s`, `slownik` → `dictionary`;
  - session files: `mikrofon.flac` → `microphone.flac`, `spotkanie.flac` → `meeting.flac`, `transkrypt.md/.srt/.txt` → `transcript.md/.srt/.txt`, `podsumowanie.md` → `summary.md`;
  - `meta.json` keys: `nazwa` → `name`, `data` → `date`, `trwanie_s` → `duration_s`, `segmenty[start, koniec, tekst, mowca]` → `segments[start, end, text, speaker]`, `uczestnicy` → `participants`, `temat` → `topic`, `punkty` → `key_points`, `ustalenia` → `decisions` (and the other technical keys);
  - segments store stable speaker codes `me` / `meeting`; the labels ("Me"/"Ja", "Meeting"/"Spotkanie") are rendered in the UI language;
  - MCP tool arguments: `od`, `do`, `fraza`, `od_min`, `do_min` → `from`, `to`, `query`, `from_min`, `to_min`; result keys are English; `--katalog` → `--recordings-dir`, `UCHO_NAGRANIA` → `UCHO_RECORDINGS_DIR`.

### Compatibility

- On first start `ustawienia.json` and `dyktuj.json` are migrated to `settings.json` and `dictate.json` (the old files are kept; a stored recordings folder is kept as it is), and `modele\` is renamed to `models\`.
- The app (recordings list, re-transcribe, re-summarize) and the MCP server read v0.1 and Nagrywacz archives with Polish file names and `meta.json` keys. A re-transcribed or re-summarized old session is saved in the new format.
- The MCP server still accepts the old Polish argument names, `--katalog` and `UCHO_NAGRANIA`.
- The installer removes the v0.1 `Ucho Dyktuj.lnk` shortcuts and `Ucho (diagnostyka).cmd`.
- `dictate.json` (or v0.1 `dyktuj.json`) is imported into `settings.json` once and kept; a dictation model set by hand there is kept, the old default is not (the hardware recommendation wins). `dictate.example.json` is gone. Quit an older *Ucho Dyktuj* before starting v0.2: it holds Ctrl+Shift+Space.
- The last dictated text is read from `ucho.log`, else from the old `dictate.log`.

## v0.1.0

- First public version: local meeting transcription (microphone + system audio), dictation window, optional local summaries, read-only MCP server.
