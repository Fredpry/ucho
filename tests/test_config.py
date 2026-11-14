# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Migrating v0.1 settings (Polish keys) to settings.json, on synthetic files."""
import json
from pathlib import Path

from ucho import config

LEGACY = {
    "katalog_nagran": "C:\\Users\\Test\\Nagrywacz\\nagrania",
    "model": "OpenVINO/whisper-large-v3-turbo-int8-ov",
    "device": "CPU",
    "jezyk": "pl",
    "jezyk_interfejsu": "en",
    "sciezki": "mikrofon",
    "mikrofon_index": 3,
    "system_index": None,
    "hotkey": "<ctrl>+<alt>+t",
    "tray": False,
    "transkrybuj_po_stopie": False,
    "podsumowanie": True,
    "model_llm": "OpenVINO/Qwen3-4B-int4-ov",
    "zmiana_nazwy": True,
    "nieznany_klucz": 1,
}


def test_migration_maps_keys_and_values():
    new = config.migrate_legacy_settings(LEGACY)
    assert new == {
        "recordings_dir": "C:\\Users\\Test\\Nagrywacz\\nagrania",
        "model": "OpenVINO/whisper-large-v3-turbo-int8-ov",
        "device": "CPU",
        "language": "pl",
        "ui_language": "en",
        "tracks": "microphone",
        "microphone_index": 3,
        "system_index": None,
        "hotkey": "<ctrl>+<alt>+t",
        "tray": False,
        "transcribe_after_stop": False,
        "summary_enabled": True,
        "summary_model": "OpenVINO/Qwen3-4B-int4-ov",
        "rename_folder": True,
    }
    assert set(new) <= set(config.DEFAULTS)


def test_all_track_values_are_mapped():
    for old, new in (("obie", "both"), ("mikrofon", "microphone"), ("system", "system")):
        assert config.migrate_legacy_settings({"sciezki": old}) == {"tracks": new}


def test_settings_migrates_file_on_first_start_and_keeps_old_one(tmp_path):
    legacy = tmp_path / "ustawienia.json"
    # The v0.1 installer wrote the file with a BOM (Out-File -Encoding utf8).
    legacy.write_text(json.dumps(LEGACY), encoding="utf-8-sig")
    settings = config.Settings(tmp_path / "settings.json")
    assert settings.recordings_dir == Path("C:\\Users\\Test\\Nagrywacz\\nagrania")
    assert settings["tracks"] == "microphone" and settings["summary_enabled"] is True
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["recordings_dir"] == LEGACY["katalog_nagran"]
    assert legacy.exists()


def test_existing_settings_are_not_overwritten(tmp_path):
    (tmp_path / "ustawienia.json").write_text(json.dumps(LEGACY), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({"device": "GPU"}), encoding="utf-8")
    settings = config.Settings(tmp_path / "settings.json")
    assert settings["device"] == "GPU" and settings["tracks"] == "both"


def test_no_files_gives_defaults_and_writes_nothing(tmp_path):
    settings = config.Settings(tmp_path / "settings.json")
    assert settings["recordings_dir"] == config.DEFAULTS["recordings_dir"]
    assert not (tmp_path / "settings.json").exists()


def test_broken_legacy_file_is_ignored(tmp_path):
    (tmp_path / "ustawienia.json").write_text("{broken", encoding="utf-8")
    assert config.Settings(tmp_path / "settings.json")["tracks"] == "both"
    assert not (tmp_path / "settings.json").exists()


def test_default_paths_are_english():
    assert config.RECORDINGS_DIR.parts[-2:] == ("Ucho", "recordings")
    assert config.SETTINGS_FILE.name == "settings.json"
    assert config.MODELS_DIR.name in ("models", "modele")
