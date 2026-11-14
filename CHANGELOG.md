# Changelog

## Unreleased (v0.2)

### Added

- Hardware detection and automatic model choice (`ucho/hardware.py`, `docs/hardware.md`). `python -m ucho.hardware` prints the hardware profile and a recommended Whisper model, device and dictation model with the reasons; `--benchmark` measures the real-time factor on a synthetic signal and steps one row down below 1.5x; `--save` writes the result to the settings.
- New settings keys `dictation_model` and `hardware_profile`.

### Changed

- The installer runs `python -m ucho.hardware --save` after installing the packages and downloads the recommended model instead of always large-v3 INT8. Re-running it keeps model and device choices made by hand (`--keep-choice`).

## v0.1.0

- First public version: local meeting transcription (microphone + system audio), dictation window, optional local summaries, read-only MCP server.
