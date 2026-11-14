# Changelog

## Unreleased (v0.2)

### Added

- Hardware detection and automatic model choice (`ucho/hardware.py`, `docs/hardware.md`). `python -m ucho.hardware` prints the hardware profile and a recommended Whisper model, device and dictation model with the reasons; `--benchmark` measures the real-time factor on a synthetic signal and steps one row down below 1.5x; `--save` writes the result to the settings.
- New settings keys `dictation_model` and `hardware_profile`.
- `ucho/session.py`: one reader of recording folders for the app and the MCP server, for both the current and the v0.1 format.

### Changed

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

## v0.1.0

- First public version: local meeting transcription (microphone + system audio), dictation window, optional local summaries, read-only MCP server.
