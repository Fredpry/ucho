# Contributing

Thanks for wanting to help. Ucho is maintained by one person, so small, focused pull requests have the best chance.

## Ground rules

1. **Ucho stays offline.** No network calls, telemetry, crash reporting, cloud APIs or background servers. The only network use is the explicit model download from Hugging Face.
2. **No PyTorch.** Models are ready-made OpenVINO IR files; nothing is converted on the user's machine.
3. **No real recordings or transcripts** in issues, pull requests, tests or commits. Use synthetic data (see `tests/test_mcp.py`).
4. **Measured decisions stay measured.** Many odd-looking choices in `transcribe.py` and `podsumowanie.py` come from measurements described in comments. Change them only with a new measurement.

## Code style

- Identifiers are Polish without diacritics (`katalog_nagran`). Comments and docstrings too, and they explain *why*.
- User-facing text is written in Polish inside `t("...")` (see `ucho/i18n.py`) and **must** get an English entry in `ucho/locale/en.json`. `tests/test_i18n.py` fails otherwise.
- `from __future__ import annotations`, type hints, `logging` instead of `print`.

## Before you open a pull request

```powershell
.\.venv-dev\Scripts\python.exe -m ruff check .
.\.venv-dev\Scripts\python.exe -m pytest
```

If you changed recording or transcription, also record ~20 seconds of Polish and English speech and check `transkrypt.md` and `ucho.log`. Say in the pull request what you ran and on which hardware; do not claim checks you did not run. AI-assisted code is fine as long as you understand and have verified it.

## Developer Certificate of Origin

Sign off every commit (`git commit -s`). By doing so you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change or have the right to submit it under the project's license (Apache-2.0).

## Translations

New interface languages are one file: copy `ucho/locale/en.json` to `<code>.json`, translate the values, keep the `{placeholders}`, and add the code to `ucho/i18n.py`.
