# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""App settings and directory paths.

This module is imported first and sets the Hugging Face environment variables
BEFORE anything imports `huggingface_hub`, so models land in the program
folder instead of the user profile and Hugging Face telemetry is off.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
# Before v0.2 the folder was called "modele".
LEGACY_MODELS_DIR = PROJECT_DIR / "modele"


def _models_dir() -> Path:
    """`models/`, renamed from the v0.1 `modele/` on first start.

    The rename is instant on the same volume and keeps the downloaded models
    (several GB). If it fails (e.g. another Ucho process still has a model
    open) we keep using the old folder rather than download everything again.
    """
    new = PROJECT_DIR / "models"
    if new.exists() or not LEGACY_MODELS_DIR.is_dir():
        return new
    try:
        LEGACY_MODELS_DIR.rename(new)
        return new
    except OSError:
        return LEGACY_MODELS_DIR


MODELS_DIR = _models_dir()
OV_CACHE_DIR = MODELS_DIR / "ovcache"
SETTINGS_FILE = PROJECT_DIR / "settings.json"
# v0.1 settings file name; migrated to settings.json on first start (and kept).
LEGACY_SETTINGS_NAME = "ustawienia.json"
LOG_FILE = PROJECT_DIR / "ucho.log"

# Recordings deliberately do NOT go to Documents: on many company computers
# Documents is redirected to OneDrive, so recordings and transcripts would go
# to the cloud and the "nothing leaves the computer" promise would be false.
# OneDrive does not sync the home folder by default.
RECORDINGS_DIR = Path.home() / "Ucho" / "recordings"

# Must run before any other module imports huggingface_hub.
os.environ.setdefault("HF_HOME", str(MODELS_DIR))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

APP_NAME = "Ucho"
VERSION = "0.1.0"

# AppUserModelID: Windows groups taskbar windows by it. Without our own id the
# window inherits the pythonw.exe icon instead of ours.
APP_ID = "Ucho.Transcription"

# Ready-made, pre-converted OpenVINO models: no PyTorch and no conversion.
# Turbo is a distilled model (4 decoder layers instead of 32): fast, but weak
# at detecting the language of short utterances. With "auto" on a PL/EN
# meeting it produced Icelandic, Russian or Arabic insertions, so the default
# is the full large-v3. Descriptions are translated in the UI.
MODELS = {
    "OpenVINO/whisper-large-v3-int8-ov": {
        "description": "Large-v3 INT8 — best language detection (recommended)",
        "size": "~1.6 GB",
    },
    "OpenVINO/whisper-large-v3-fp16-ov": {
        "description": "Large-v3 FP16 — full precision, about 25% slower (default in Dictate)",
        "size": "~3.1 GB",
    },
    "OpenVINO/whisper-large-v3-turbo-int8-ov": {
        "description": "Turbo INT8 — faster, worse with mixed languages",
        "size": "~790 MB",
    },
    "OpenVINO/whisper-large-v3-turbo-int4-ov": {
        "description": "Turbo INT4 — fastest, for long recordings",
        "size": "~500 MB",
    },
}

DEFAULT_MODEL = "OpenVINO/whisper-large-v3-int8-ov"

# Optional language models for the summary and folder naming. Same runtime as
# Whisper (OpenVINO GenAI), same models folder: no Ollama and no background
# server. The model loads only after transcription and is released right away.
# Summaries are off by default: transcription is the core of Ucho.
LLM_MODELS = {
    "OpenVINO/Qwen3-8B-int4-ov": {
        "description": "Qwen3 8B INT4 — best summaries, slower",
        "size": "~5.0 GB",
    },
    "OpenVINO/Qwen3-4B-int4-ov": {
        "description": "Qwen3 4B INT4 — fast, good Polish",
        "size": "~2.3 GB",
    },
    "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov": {
        "description": "Qwen2.5 1.5B INT4 — fastest, smallest",
        "size": "~0.95 GB",
    },
}

DEFAULT_LLM_MODEL = "OpenVINO/Qwen3-8B-int4-ov"

# Pinned model revisions (Hugging Face commit, checked 14.11.2026). We download
# exactly these, so a model swapped in the source repository does not reach
# users without a change to this dict.
REVISIONS = {
    "OpenVINO/whisper-large-v3-int8-ov": "5603442ef51120fc1d5bb6c8749d01592b14acb5",
    "OpenVINO/whisper-large-v3-fp16-ov": "220761e60602a5ca694c409d5f424563b75d6820",
    "OpenVINO/whisper-large-v3-turbo-int8-ov": "b568445dd5dc8c695bde596f8acbb4694fd6ba64",
    "OpenVINO/whisper-large-v3-turbo-int4-ov": "ae50b4d9a9dbaf16f2df59c23f3984e42f864dfc",
    "OpenVINO/Qwen3-8B-int4-ov": "5c47abf4b8e12ebe8e99745bb0c1ec17e0c0abcc",
    "OpenVINO/Qwen3-4B-int4-ov": "b467368d16b75df14055562fe927ae8e1f15f7ef",
    "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov": "4d14c299e35d8b74e3471f9e92bd1377fae50736",
}

# Whisper expects the language token in this form. None = auto-detection.
# Auto-detection picks only among the other entries of this dict
# (see transcribe.py, narrowed lang_to_id map).
LANGUAGES = {
    "auto": None,
    "pl": "<|pl|>",
    "en": "<|en|>",
}

# Language names are shown in their own language, so they are not translated.
LANGUAGE_LABELS = {
    "auto": "Auto (PL/EN)",
    "pl": "Polski",
    "en": "English",
}

TRACK_LABELS = {
    "both": "Microphone + system audio",
    "microphone": "Microphone only",
    "system": "System audio only",
}

UI_LANGUAGE_LABELS = {
    "auto": "Auto (same as system)",
    "pl": "Polski",
    "en": "English",
}

DEFAULTS = {
    "recordings_dir": str(RECORDINGS_DIR),
    "model": DEFAULT_MODEL,
    "device": "GPU",
    "language": "auto",
    "ui_language": "auto",
    "tracks": "both",
    "microphone_index": None,
    "system_index": None,
    "hotkey": "<ctrl>+<alt>+r",
    "tray": True,
    "transcribe_after_stop": True,
    "summary_enabled": False,
    "summary_model": DEFAULT_LLM_MODEL,
    "rename_folder": False,
    # Written by `python -m ucho.hardware --save` (the installer runs it). The
    # dictation window does not read it yet: it still uses dictate.json until
    # both windows are merged (plan v0.2, W3).
    "dictation_model": "OpenVINO/whisper-large-v3-fp16-ov",
    # Short hardware summary with the last automatic recommendation, so that
    # re-running the installer can tell an automatic choice from a manual one.
    "hardware_profile": None,
}

# v0.1 key -> current key. Keys that did not change (model, device, hotkey,
# tray, system_index, dictation_model, hardware_profile) are copied as they are.
LEGACY_KEYS = {
    "katalog_nagran": "recordings_dir",
    "jezyk": "language",
    "jezyk_interfejsu": "ui_language",
    "sciezki": "tracks",
    "mikrofon_index": "microphone_index",
    "transkrybuj_po_stopie": "transcribe_after_stop",
    "podsumowanie": "summary_enabled",
    "model_llm": "summary_model",
    "zmiana_nazwy": "rename_folder",
}
LEGACY_VALUES = {
    "tracks": {"obie": "both", "mikrofon": "microphone", "system": "system"},
}


def migrate_legacy_settings(old: dict) -> dict:
    """v0.1 settings -> current keys and values. Unknown keys are dropped.

    A stored recordings path is kept as it is: existing archives live in
    folders such as `...\\Ucho\\nagrania` and must not move.
    """
    new: dict = {}
    for key, value in old.items():
        key = LEGACY_KEYS.get(key, key)
        if key not in DEFAULTS:
            continue
        values = LEGACY_VALUES.get(key, {})
        new[key] = values.get(value, value) if isinstance(value, str) else value
    return new


def migrate_file(new_file: Path, legacy_file: Path, migrate) -> bool:
    """Writes `new_file` from `legacy_file` when only the old one exists.

    The old file is kept, so going back to v0.1 still works. Returns True when
    a migration happened.
    """
    if new_file.exists() or not legacy_file.exists():
        return False
    try:
        old = json.loads(legacy_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        log.warning("Could not read %s for migration", legacy_file)
        return False
    if not isinstance(old, dict):
        return False
    try:
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_text(json.dumps(migrate(old), indent=2, ensure_ascii=False),
                            encoding="utf-8")
    except OSError:
        log.warning("Could not write %s", new_file)
        return False
    log.info("Migrated %s -> %s", legacy_file.name, new_file.name)
    return True


class Settings:
    """Thin layer over the JSON file. Missing keys are filled with defaults.

    On first use `settings.json` is created from a v0.1 `ustawienia.json`
    lying next to it, if there is one.
    """

    def __init__(self, file: Path = SETTINGS_FILE):
        self._file = file
        self._data = dict(DEFAULTS)
        migrate_file(file, file.with_name(LEGACY_SETTINGS_NAME), migrate_legacy_settings)
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self._file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS:
                    self._data[key] = value

    def save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def __getitem__(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self._data[key] = value

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    @property
    def recordings_dir(self) -> Path:
        return Path(self._data["recordings_dir"])


def prepare_dirs(settings: "Settings | None" = None) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    OV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    folder = settings.recordings_dir if settings else RECORDINGS_DIR
    folder.mkdir(parents=True, exist_ok=True)
