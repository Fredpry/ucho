# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""App settings and directory paths.

This module is imported first and sets the Hugging Face environment variables
BEFORE anything imports `huggingface_hub`, so models land in the program
folder instead of the user profile and Hugging Face telemetry is off.
"""

from __future__ import annotations

import importlib.util
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
VERSION = "0.5.0"

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
    # Optional second backend (ucho/parakeet.py, sherpa-onnx, CPU only). Offered
    # in Settings only when `install.ps1 -WithParakeet` installed sherpa-onnx
    # (transcribe.offered_models). Never recommended automatically: on synthetic
    # Polish speech it matches Whisper closely and is 3-10x faster on the CPU,
    # but English phrases inside Polish sentences come out badly
    # (docs/benchmarks.md, "Parakeet").
    "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3": {
        "name": "parakeet-tdt-0.6b-v3",
        "description": "Parakeet TDT 0.6B v3 — fast on the CPU, weak with English phrases in Polish",
        "size": "~2.5 GB",
        "backend": "parakeet",
    },
}

DEFAULT_MODEL = "OpenVINO/whisper-large-v3-int8-ov"

# Optional built-in language models for meeting analysis (summary, folder
# naming and automatic meeting titles; ucho/llm.py). Same runtime as Whisper
# (OpenVINO GenAI), same models folder: no Ollama and no background server. The
# model loads only after transcription and is released right away. Users can
# also point `analysis_model` to their own OpenVINO IR folder or .gguf file.
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

# Fallback analysis model derived by `llm.chosen` for v0.4 users who had
# downloaded it for automatic meeting titles (ucho/titles.py). Measured on 10
# synthetic Polish and English meetings (docs/benchmarks.md, "Meeting titles"):
# Qwen3-1.7B and Qwen2.5-1.5B gave a wrong or ungrammatical Polish title in 3-4
# of 10, Qwen3-0.6B in most; Qwen3-4B in none. 4B takes about 1 s on the Arc
# iGPU and 7-27 s on the CPU per title.
TITLE_MODEL = "OpenVINO/Qwen3-4B-int4-ov"

# Speaker detection models (ucho/diarize.py, sherpa-onnx, CPU only).
SPEAKER_MODELS = {
    "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0": {
        "file": "model.onnx",
        "size": "~6 MB",
    },
    "csukuangfj/speaker-embedding-models": {
        "file": "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
        "size": "~27 MB",
    },
}

SPEAKER_MODELS_SIZE = "~33 MB"

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
    # Optional Parakeet backend (ucho/parakeet.py): ONNX exports of NVIDIA's
    # parakeet-tdt-0.6b-v3 (CC BY 4.0) by the sherpa-onnx project. SHA-256 of
    # the files at these revisions (equal to the Hugging Face LFS ids):
    # fp32: encoder.onnx    36b758364dfddce472b9a9f75bf6049bbd35c886d7927e3eda80d4ceb3348512
    #       encoder.weights 3af3f51af5f2d01dbbf5af47d42c7962a2c205f11004254bb4f2b979862f39a8
    #       decoder.onnx    caed31af1bb08edcc09a01a1c3cbb4d4eb5bfdb9c4e5f307012f84bbf5dbfdbc
    #       joiner.onnx     0f294e5ba9021a3d80e492de94abe4c116f0afce78452c7debb5dd07690d0340
    # int8: encoder.int8.onnx 1afbe69c1ffdabee94755f42e4daf1d93549131a3da11356bda84fcef0f2c479
    #       decoder.int8.onnx 0222a86147299d4dee90edf95b6d13827edb42ad8b7c9cf0b2582f030937c0b5
    #       joiner.int8.onnx  cbd02f3696e9944970111fb439d9c3fd365a80374565f13f2b9c7007d253fc98
    # both: tokens.txt        d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d
    "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3": "5fcc0122fe87e8365e339e467af90c67b746218f",
    "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": "ed5d745623472f97cfc2e6c2916ee248798fc5a8",
    # Speaker detection (ucho/diarize.py); pyannote segmentation-3.0 (MIT, CNRS)
    # exported to ONNX by the sherpa-onnx project; ERes2Net speaker embeddings
    # trained on VoxCeleb by the 3D-Speaker project (ModelScope
    # iic/speech_eres2net_sv_en_voxceleb_16k, Apache-2.0), ONNX by sherpa-onnx.
    # SHA-256: model.onnx 220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079,
    #   3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx
    #   c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5
    "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0": "9403a6902bb58e3d5ae8c7e77c3422de279db2e0",
    "csukuangfj/speaker-embedding-models": "0743f301363dec56491a490f6d6cbc9d67f9a3bf",
}

# Repos of which only some files are downloaded (the Parakeet repos also hold
# test recordings). The same patterns must be passed to the offline check, or
# huggingface_hub reports the snapshot as incomplete.
DOWNLOAD_PATTERNS = {
    "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3": [
        "encoder.onnx", "encoder.weights", "decoder.onnx", "joiner.onnx", "tokens.txt", "README.md",
    ],
    "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": [
        "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "README.md",
    ],
    "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0": [
        "model.onnx", "LICENSE", "README.md",
    ],
    # That repo also holds 20+ other models and test recordings; only this one file is downloaded.
    "csukuangfj/speaker-embedding-models": [
        "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
    ],
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

THEME_LABELS = {
    "system": "Same as Windows",
    "light": "Light",
    "dark": "Dark",
}

LAYOUT_LABELS = {
    "single": "One window: Meeting and Dictation",
    "tray": "Tray icon with separate windows",
}

DICTATION_MODE_LABELS = {
    "paste": "Paste (clipboard + Ctrl+V)",
    "type": "Type (simulated keyboard)",
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
    # Chosen devices by name; None = the Windows default. Earlier versions
    # saved a PortAudio index instead, which shifts when devices are plugged in
    # or out; audio.migrate_device_choices() turns it into a name.
    "microphone_name": None,
    "system_name": None,
    "microphone_index": None,
    "system_index": None,
    "hotkey": "<ctrl>+<alt>+r",
    "tray": True,
    "transcribe_after_stop": True,
    # Transcribe a meeting while it is recorded (ucho/live_meeting.py), so the
    # transcript is ready soon after the stop. None = automatic: on in Auto
    # language mode unless the transcription runs on the CPU (Settings.meeting_live).
    "meeting_live": None,
    # Detect speakers inside a track (ucho/diarize.py). Experimental, off by
    # default: needs the optional sherpa-onnx package and two small models.
    "speaker_detection": False,
    "summary_enabled": False,
    "summary_model": DEFAULT_LLM_MODEL,
    # The one local language model for summaries and automatic titles: a repo id
    # from LLM_MODELS, an absolute path to the user's own model (OpenVINO IR
    # folder or .gguf file), or "" for none. None = not chosen yet (v0.4
    # settings), derived by `llm.chosen` from `summary_model` and the downloaded
    # models.
    "analysis_model": None,
    "rename_folder": False,
    # Name meetings the user did not name with a short title from the analysis
    # model (ucho/titles.py); None = automatic: on when that model is available;
    # True/False = the user's choice.
    "auto_title": None,
    # Delete the audio of transcribed recordings after this many days (0 = never).
    # Transcripts, summaries and meta.json stay. See ucho/retention.py.
    "audio_retention_days": 0,
    # Written by `python -m ucho.hardware --save` (the installer runs it) and
    # by the first-run wizard. A repo id, or DICTATION_SHARED: dictation then
    # uses the transcription model (one model in memory instead of two).
    "dictation_model": "OpenVINO/whisper-large-v3-fp16-ov",
    # Short hardware summary with the last automatic recommendation, so that
    # re-running the installer can tell an automatic choice from a manual one.
    "hardware_profile": None,
    # Window and app behavior (v0.2).
    "theme": "system",            # system / light / dark
    "layout": "single",           # single (one window, two modes) / tray (tray hub, separate windows)
    "start_with_windows": False,
    "setup_complete": False,      # the first-run wizard was finished
    # Dictation (before v0.2 in dictate.json, imported on first start).
    "dictation_hotkey": "<ctrl>+<shift>+space",
    "dictation_language": "auto",
    "dictation_enter": False,     # Enter after pasting, i.e. "send" right away
    "dictation_mode": "paste",    # paste (clipboard + Ctrl+V) / type (simulated keyboard)
    "dictation_sounds": True,
    "dictation_max_s": 300,
    # Keep the dictation model loaded so a dictation starts at once. Off: it
    # loads on the first dictation (a few seconds) and frees memory otherwise.
    "dictation_preload": True,
    # Recognize a dictation in pieces while it is spoken (ucho/live.py), so the text is ready right after the stop.
    "dictation_live": True,
    # Names that Whisper keeps getting wrong. Key = what it recognized
    # (case-insensitive, whole words), value = the correct form.
    "dictation_dictionary": {
        "Cloud Code": "Claude Code",
        "Cloud Codem": "Claude Code'em",
    },
}

DICTATION_SHARED = "shared"
THEMES = ("system", "light", "dark")
# Whisper devices offered in Settings. GPU.0/GPU.1 from the hardware
# recommendation are kept as they are.
DEVICES = ("GPU", "CPU", "NPU", "AUTO")

# --- dictation settings, before v0.2 in a separate file -----------------------

# dictate.json (v0.2 development builds) and dyktuj.json (v0.1) next to the
# settings file. Their content is imported into settings.json on first start.
DICTATE_FILE_NAME = "dictate.json"
LEGACY_DICTATE_FILE_NAME = "dyktuj.json"
DICTATE_DEFAULTS = {
    "language": "auto",
    "enter": False,
    "mode": "paste",
    "sounds": True,
    "max_recording_s": 300,
    "model": "OpenVINO/whisper-large-v3-fp16-ov",
    "dictionary": dict(DEFAULTS["dictation_dictionary"]),
}
# dyktuj.json key -> dictate.json key ("enter" and "model" did not change).
DICTATE_LEGACY_KEYS = {
    "jezyk": "language",
    "tryb": "mode",
    "dzwieki": "sounds",
    "maks_nagranie_s": "max_recording_s",
    "slownik": "dictionary",
}
DICTATE_LEGACY_MODES = {"wklej": "paste", "pisz": "type"}
# dictate.json key -> settings.json key.
DICTATE_TO_SETTINGS = {
    "language": "dictation_language",
    "enter": "dictation_enter",
    "mode": "dictation_mode",
    "sounds": "dictation_sounds",
    "max_recording_s": "dictation_max_s",
    "dictionary": "dictation_dictionary",
}


def migrate_legacy_dictate_settings(old: dict) -> dict:
    """v0.1 dyktuj.json content -> dictate.json keys and values. Unknown keys are dropped."""
    new: dict = {}
    for key, value in old.items():
        key = DICTATE_LEGACY_KEYS.get(key, key)
        if key not in DICTATE_DEFAULTS:
            continue
        if key == "mode" and isinstance(value, str):
            value = DICTATE_LEGACY_MODES.get(value, value)
        new[key] = value
    return new


def read_dictate_file(folder: Path) -> dict | None:
    """dictate.json (or v0.1 dyktuj.json) in `folder` with current keys; None when absent."""
    for name, migrate in ((DICTATE_FILE_NAME, None),
                          (LEGACY_DICTATE_FILE_NAME, migrate_legacy_dictate_settings)):
        try:
            data = json.loads((folder / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return migrate(data) if migrate else data
    return None


def dictate_to_settings(dictate: dict) -> dict:
    """dictate.json content -> settings.json keys.

    The model is taken only when it differs from the old dictate.json default;
    otherwise the hardware recommendation already in settings.json wins.
    """
    new = {DICTATE_TO_SETTINGS[k]: v for k, v in dictate.items() if k in DICTATE_TO_SETTINGS}
    model = dictate.get("model")
    if isinstance(model, str) and model in MODELS and model != DICTATE_DEFAULTS["model"]:
        new["dictation_model"] = model
    return new


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


def sherpa_available() -> bool:
    """True when the optional sherpa-onnx package is installed (checked without importing it)."""
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def write_json(file: Path, data) -> None:
    """Writes `data` as JSON through a temp file in the same folder, so a failure keeps the old file."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(file.name + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, file)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


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
        write_json(new_file, migrate(old))
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
        self._file = Path(file)
        self._data = dict(DEFAULTS)
        self._stored: set[str] = set()
        migrate_file(self._file, self._file.with_name(LEGACY_SETTINGS_NAME),
                     migrate_legacy_settings)
        self.load()
        self._import_dictate_file()

    @property
    def file(self) -> Path:
        return self._file

    def load(self) -> None:
        try:
            stored = json.loads(self._file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(stored, dict):
            self._stored = set(stored)
            for key, value in stored.items():
                if key in DEFAULTS:
                    self._data[key] = value

    def _import_dictate_file(self) -> None:
        """Dictation settings from dictate.json / dyktuj.json, once (W3: one app).

        Runs only while settings.json has no dictation keys yet. The old file
        is kept, so going back to an older version still works.
        """
        if "dictation_language" in self._stored:
            return
        dictate = read_dictate_file(self._file.parent)
        if dictate is None:
            return
        self._data.update(dictate_to_settings(dictate))
        self.save()
        self._stored |= set(DICTATE_TO_SETTINGS.values())
        log.info("Imported dictation settings from %s", DICTATE_FILE_NAME)

    def dictation_model(self) -> str:
        """The model dictation actually uses (resolves DICTATION_SHARED)."""
        value = self["dictation_model"]
        if not value or value == DICTATION_SHARED:
            return self["model"]
        return value

    def meeting_live(self) -> bool:
        """Live meeting transcription: the user's choice, else automatic.

        Automatic is on in Auto language mode on a GPU or NPU. On the CPU the
        live work would compete with the call itself (video, screen sharing)
        for the same cores. With a forced language the chunks are up to 5
        minutes long and cannot be interrupted, so a dictation spoken while
        one is being recognized would wait for it (#12; docs/benchmarks.md).
        """
        value = self["meeting_live"]
        if isinstance(value, bool):
            return value
        return str(self["device"] or "").upper() != "CPU" and self["language"] == "auto"

    def dictation_shared(self) -> bool:
        return self.dictation_model() == self["model"]

    def save(self) -> None:
        try:
            write_json(self._file, self._data)
        except OSError as error:
            log.warning("Could not save settings: %s", type(error).__name__)

    def __getitem__(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self._data[key] = value

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    @property
    def recordings_dir(self) -> Path:
        return Path(self._data["recordings_dir"])


def prepare_dirs(settings: Settings | None = None) -> None:
    """Creates the recordings folder.

    The models folder is created by the download or the first compile, so a
    start without models (or the smoke test) leaves no empty folders behind.
    """
    folder = settings.recordings_dir if settings else RECORDINGS_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("Could not create the recordings folder %s", folder)


def onedrive_folder(path: Path) -> bool:
    """True when `path` lies in a OneDrive-synced folder (recordings would go to the cloud)."""
    text = str(path).lower()
    for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(variable)
        if root and text.startswith(root.lower().rstrip("\\/")):
            return True
    return "onedrive" in text
