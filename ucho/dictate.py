# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Compatibility entry point for dictation ("Ucho Dictate").

Since v0.2 dictation is part of the one Ucho app (ucho/dictation.py, shared
engine in ucho/engine.py). `dictate.pyw` and old "Ucho Dictate" shortcuts
still land here: `main()` starts Ucho in dictation mode, or, when Ucho
already runs, asks it to open dictation.

What is left here besides `main()`: reading the pre-v0.2 dictation settings
file (dictate.json / dyktuj.json, imported into settings.json by
config.Settings) and the last dictated text from the log.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from . import config, transcribe

APP_ID = config.APP_ID
SETTINGS_FILE = config.PROJECT_DIR / config.DICTATE_FILE_NAME
# v0.1 file name; read when dictate.json does not exist.
LEGACY_SETTINGS_FILE = config.PROJECT_DIR / config.LEGACY_DICTATE_FILE_NAME
# Since v0.2 dictation logs to ucho.log; earlier versions had their own log.
LOG_FILE = config.LOG_FILE
LEGACY_LOG_FILE = config.PROJECT_DIR / "dictate.log"

DEFAULTS = config.DICTATE_DEFAULTS
LEGACY_KEYS = config.DICTATE_LEGACY_KEYS
LEGACY_MODES = config.DICTATE_LEGACY_MODES
migrate_legacy_dictate_settings = config.migrate_legacy_dictate_settings

# Moved to transcribe.py (the shared engine calls them); kept for old imports.
recognize = transcribe.recognize_dictation
apply_dictionary = transcribe.apply_dictionary


def load_settings(file: Path | None = None, legacy_file: Path | None = None) -> dict:
    """dictate.json content with defaults (migrating dyktuj.json once)."""
    file = file or SETTINGS_FILE
    legacy_file = legacy_file or file.with_name(config.LEGACY_DICTATE_FILE_NAME)
    config.migrate_file(file, legacy_file, migrate_legacy_dictate_settings)
    data = dict(DEFAULTS)
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data: dict, file: Path | None = None) -> None:
    file = file or SETTINGS_FILE
    try:
        file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# Log line of a finished dictation, in the current and the v0.1 wording.
_RECOGNIZED_LINE = re.compile(
    r"(?:Recognized [\d.]+ s of audio in [\d.]+ s|Rozpoznano [\d.]+ s audio w [\d.]+ s): (.+)$"
)


def _last_text_in(file: Path) -> str:
    try:
        with open(file, "rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 256_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        match = _RECOGNIZED_LINE.search(line)
        if not match:
            continue
        try:
            text = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(text, str) and text.strip():
            return text
    return ""


def last_text_from_log(file: Path | None = None) -> str:
    """The last non-empty dictation from the log, so it survives an app restart."""
    if file is not None:
        return _last_text_in(file)
    for candidate in (LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1"), LEGACY_LOG_FILE):
        text = _last_text_in(candidate)
        if text:
            return text
    return ""


def main() -> int:
    """`dictate.pyw`: Ucho in dictation mode."""
    from .__main__ import main as ucho_main

    return ucho_main(["--dictate", *[a for a in sys.argv[1:] if a != "--dictate"]])


if __name__ == "__main__":
    sys.exit(main())
