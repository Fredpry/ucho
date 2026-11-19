# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Migrating v0.1 settings (Polish keys) to settings.json, on synthetic files."""
import json
import sys
from pathlib import Path

import pytest

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


# --- v0.2: dictation settings in settings.json (W3) ----------------------------------


def test_dictate_json_is_imported_once(tmp_path):
    (tmp_path / "dictate.json").write_text(json.dumps({
        "language": "pl", "enter": True, "mode": "type", "sounds": False,
        "max_recording_s": 120, "dictionary": {"Ucha": "Ucho"},
        "model": "OpenVINO/whisper-large-v3-fp16-ov",
    }), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({
        "dictation_model": "OpenVINO/whisper-large-v3-int8-ov"}), encoding="utf-8")
    settings = config.Settings(tmp_path / "settings.json")
    assert settings["dictation_language"] == "pl"
    assert settings["dictation_enter"] is True and settings["dictation_mode"] == "type"
    assert settings["dictation_sounds"] is False and settings["dictation_max_s"] == 120
    assert settings["dictation_dictionary"] == {"Ucha": "Ucho"}
    # The old default FP16 in dictate.json does not override the hardware recommendation.
    assert settings["dictation_model"] == "OpenVINO/whisper-large-v3-int8-ov"
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["dictation_language"] == "pl"
    assert (tmp_path / "dictate.json").exists()

    # A later change in settings.json is not overwritten by the old file again.
    settings["dictation_language"] = "en"
    settings.save()
    assert config.Settings(tmp_path / "settings.json")["dictation_language"] == "en"


def test_legacy_dyktuj_json_is_imported(tmp_path):
    (tmp_path / "dyktuj.json").write_text(json.dumps({
        "jezyk": "en", "tryb": "pisz", "model": "OpenVINO/whisper-large-v3-turbo-int8-ov"}),
        encoding="utf-8")
    settings = config.Settings(tmp_path / "settings.json")
    assert settings["dictation_language"] == "en" and settings["dictation_mode"] == "type"
    # A model chosen by hand in the old file is kept.
    assert settings["dictation_model"] == "OpenVINO/whisper-large-v3-turbo-int8-ov"


def test_no_dictate_file_writes_nothing(tmp_path):
    config.Settings(tmp_path / "settings.json")
    assert not (tmp_path / "settings.json").exists()


def test_shared_dictation_model_resolves_to_the_transcription_model(tmp_path):
    settings = config.Settings(tmp_path / "settings.json")
    settings["model"] = "OpenVINO/whisper-large-v3-turbo-int8-ov"
    settings["dictation_model"] = config.DICTATION_SHARED
    assert settings.dictation_model() == "OpenVINO/whisper-large-v3-turbo-int8-ov"
    assert settings.dictation_shared()
    settings["dictation_model"] = "OpenVINO/whisper-large-v3-fp16-ov"
    assert settings.dictation_model() == "OpenVINO/whisper-large-v3-fp16-ov"
    assert not settings.dictation_shared()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows only: OneDrive variables and drive-letter paths")
def test_onedrive_folder(monkeypatch):
    monkeypatch.setenv("OneDrive", r"C:\Users\Test\OneDrive")
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    assert config.onedrive_folder(Path(r"C:\Users\Test\OneDrive\Ucho\recordings"))
    assert config.onedrive_folder(Path(r"D:\OneDrive - Firma\x"))
    assert not config.onedrive_folder(Path(r"C:\Users\Test\Ucho\recordings"))


def test_installer_writes_only_known_settings_keys():
    script = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")
    block = script[script.index("$configuration = [ordered]@{"):]
    block = block[:block.index("}")]
    keys = {line.split("=")[0].strip() for line in block.splitlines()[1:] if "=" in line}
    assert keys and keys <= set(config.DEFAULTS), keys - set(config.DEFAULTS)


def test_write_json_failure_keeps_the_old_file(tmp_path, monkeypatch):
    file = tmp_path / "sub" / "data.json"
    config.write_json(file, {"a": 1, "b": "zażółć"})
    assert json.loads(file.read_text(encoding="utf-8")) == {"a": 1, "b": "zażółć"}

    def broken_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", broken_replace)
    with pytest.raises(OSError):
        config.write_json(file, {"a": 2})
    assert json.loads(file.read_text(encoding="utf-8")) == {"a": 1, "b": "zażółć"}
    assert not (file.parent / "data.json.tmp").exists()


def test_settings_save_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    settings = config.Settings(tmp_path / "settings.json")
    monkeypatch.setattr(config.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("x")))
    with caplog.at_level("WARNING"):
        settings.save()
    assert "Could not save settings: OSError" in caplog.text
