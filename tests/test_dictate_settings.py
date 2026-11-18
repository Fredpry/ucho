# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictate settings migration (dyktuj.json -> dictate.json) and the last text from the log."""
import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictate uses WinAPI")

LEGACY = {
    "jezyk": "pl",
    "enter": True,
    "tryb": "pisz",
    "dzwieki": False,
    "maks_nagranie_s": 120,
    "model": "OpenVINO/whisper-large-v3-int8-ov",
    "slownik": {"Cloud Code": "Claude Code", "Ucha": "Ucho"},
    "nieznany_klucz": 1,
}


@pytest.fixture
def dictate():
    from ucho import dictate

    return dictate


def test_migration_maps_keys_and_values(dictate):
    new = dictate.migrate_legacy_dictate_settings(LEGACY)
    assert new == {
        "language": "pl",
        "enter": True,
        "mode": "type",
        "sounds": False,
        "max_recording_s": 120,
        "model": "OpenVINO/whisper-large-v3-int8-ov",
        "dictionary": {"Cloud Code": "Claude Code", "Ucha": "Ucho"},
    }
    assert dictate.migrate_legacy_dictate_settings({"tryb": "wklej"}) == {"mode": "paste"}


def test_load_migrates_legacy_file_and_keeps_it(dictate, tmp_path):
    legacy = tmp_path / "dyktuj.json"
    legacy.write_text(json.dumps(LEGACY), encoding="utf-8")
    new = tmp_path / "dictate.json"

    settings = dictate.load_settings(new)

    assert legacy.exists()
    assert new.exists()
    stored = json.loads(new.read_text(encoding="utf-8"))
    assert stored["mode"] == "type" and "tryb" not in stored
    assert settings["language"] == "pl"
    assert settings["mode"] == "type"
    assert settings["dictionary"]["Ucha"] == "Ucho"
    assert settings["max_recording_s"] == 120


def test_existing_new_file_is_not_overwritten(dictate, tmp_path):
    (tmp_path / "dyktuj.json").write_text(json.dumps(LEGACY), encoding="utf-8")
    new = tmp_path / "dictate.json"
    new.write_text(json.dumps({"language": "en", "mode": "paste"}), encoding="utf-8")

    settings = dictate.load_settings(new)

    assert json.loads(new.read_text(encoding="utf-8")) == {"language": "en", "mode": "paste"}
    assert settings["language"] == "en"
    assert settings["sounds"] is True  # default fills the gap


def test_save_and_load_round_trip(dictate, tmp_path):
    file = tmp_path / "dictate.json"
    data = dict(dictate.DEFAULTS, language="en", enter=True)
    dictate.save_settings(data, file)
    assert dictate.load_settings(file) == data


def test_last_text_from_new_log(dictate, tmp_path):
    log = tmp_path / "dictate.log"
    log.write_text(
        "2026-11-14 10:00:00 INFO ucho.dictate: Recognized 3.2 s of audio in 0.8 s: 'First text'\n"
        "2026-11-14 10:01:00 INFO ucho.dictate: Recognized 2.0 s of audio in 0.5 s: 'Zażółć gęślą'\n"
        "2026-11-14 10:02:00 INFO ucho.dictate: Recognized 0.4 s of audio in 0.1 s: ''\n",
        encoding="utf-8")
    assert dictate.last_text_from_log(log) == "Zażółć gęślą"


def test_last_text_falls_back_to_legacy_log(dictate, tmp_path, monkeypatch):
    legacy = tmp_path / "dyktuj.log"
    legacy.write_text(
        "2026-11-13 09:00:00 INFO ucho.dyktuj: Rozpoznano 4.1 s audio w 1.0 s: 'Stary tekst'\n",
        encoding="utf-8")
    monkeypatch.setattr(dictate, "LOG_FILE", tmp_path / "dictate.log")
    monkeypatch.setattr(dictate, "LEGACY_LOG_FILE", legacy)
    assert dictate.last_text_from_log() == "Stary tekst"


def test_last_text_without_any_log(dictate, tmp_path, monkeypatch):
    monkeypatch.setattr(dictate, "LOG_FILE", tmp_path / "dictate.log")
    monkeypatch.setattr(dictate, "LEGACY_LOG_FILE", tmp_path / "dyktuj.log")
    assert dictate.last_text_from_log() == ""


def test_last_text_looks_at_the_rotated_log(dictate, tmp_path, monkeypatch):
    log = tmp_path / "ucho.log"
    log.write_text("2026-11-15 10:00:00 INFO ucho: Start\n", encoding="utf-8")
    (tmp_path / "ucho.log.1").write_text(
        "2026-11-15 09:00:00 INFO ucho.dictation: Recognized 2.0 s of audio in 0.5 s: 'Starszy tekst'\n",
        encoding="utf-8")
    monkeypatch.setattr(dictate, "LOG_FILE", log)
    monkeypatch.setattr(dictate, "LEGACY_LOG_FILE", tmp_path / "dictate.log")
    assert dictate.last_text_from_log() == "Starszy tekst"
