# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""One reader for session folders in the current and the pre-v0.2 (Polish) format."""
import json
from pathlib import Path

from ucho import session
from ucho.merge import Segment


def _old_session(root: Path) -> Path:
    folder = root / "2026-11-10_10-00_Budzet"
    folder.mkdir(parents=True)
    meta = {
        "nazwa": "Budżet", "data": "2026-11-10 10:00", "trwanie_s": 600, "jezyk": "pl",
        "urzadzenie_mikrofon": "Mikrofon (USB)", "temat": "Budżet na Q4",
        "uczestnicy": ["Anna"], "tldr": "Krótko.", "punkty": ["Cena stali"],
        "ustalenia": ["Środa 12.00"], "model_llm": "OpenVINO/Qwen3-8B-int4-ov",
        "segmenty": [
            {"start": 5, "koniec": 9, "tekst": "Zaczynamy od budżetu.", "mowca": "Ja"},
            {"start": 130, "koniec": 135, "tekst": "Cena wzrosła.", "mowca": "Spotkanie"},
            {"start": 140, "koniec": 141, "tekst": "", "mowca": "Spotkanie"},
        ],
    }
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for name in ("mikrofon.flac", "spotkanie.flac", "transkrypt.md", "podsumowanie.md"):
        (folder / name).write_bytes(b"x")
    return folder


def _new_session(root: Path) -> Path:
    folder = root / "2026-11-12_14-30_Hiring"
    folder.mkdir(parents=True)
    meta = {
        "name": "Hiring", "date": "2026-11-12 14:30", "duration_s": 300,
        "segments": [{"start": 10, "end": 15, "text": "Three candidates.", "speaker": "meeting"}],
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for name in ("microphone.flac", "transcript.md"):
        (folder / name).write_bytes(b"x")
    return folder


def test_old_meta_keys_are_mapped(tmp_path):
    meta = session.read_meta(_old_session(tmp_path))
    assert meta["name"] == "Budżet" and meta["date"] == "2026-11-10 10:00"
    assert meta["duration_s"] == 600 and meta["language"] == "pl"
    assert meta["topic"] == "Budżet na Q4" and meta["participants"] == ["Anna"]
    assert meta["key_points"] == ["Cena stali"] and meta["decisions"] == ["Środa 12.00"]
    assert meta["microphone_device"] == "Mikrofon (USB)"
    assert meta["summary_model"] == "OpenVINO/Qwen3-8B-int4-ov"
    assert not {"nazwa", "data", "segmenty", "temat"} & set(meta)


def test_old_segments_get_codes_and_english_keys(tmp_path):
    meta = session.read_meta(_old_session(tmp_path))
    assert meta["segments"][0] == {"start": 5.0, "end": 9.0, "text": "Zaczynamy od budżetu.",
                                   "speaker": "me"}
    assert session.segments(meta) == [
        Segment(5.0, 9.0, "Zaczynamy od budżetu.", "me"),
        Segment(130.0, 135.0, "Cena wzrosła.", "meeting"),
    ]


def test_new_format_is_read_as_is(tmp_path):
    meta = session.read_meta(_new_session(tmp_path))
    assert meta["name"] == "Hiring"
    assert session.segments(meta) == [Segment(10.0, 15.0, "Three candidates.", "meeting")]


def test_english_labels_from_v01_english_ui_map_to_codes():
    meta = session.normalize_meta({"segmenty": [
        {"start": 1, "koniec": 2, "tekst": "Hi", "mowca": "Me"},
        {"start": 3, "koniec": 4, "tekst": "Hello", "mowca": "Meeting"}]})
    assert [s["speaker"] for s in meta["segments"]] == ["me", "meeting"]


def test_current_key_wins_over_legacy_twin():
    assert session.normalize_meta({"nazwa": "old", "name": "new"})["name"] == "new"


def test_files_are_found_under_both_names(tmp_path):
    old, new = _old_session(tmp_path), _new_session(tmp_path)
    assert session.find_file(old, "transcript.md") == old / "transkrypt.md"
    assert session.find_file(old, session.SUMMARY_MD) == old / "podsumowanie.md"
    assert session.find_file(new, "transcript.md") == new / "transcript.md"
    assert session.find_file(new, session.SUMMARY_MD) is None
    assert session.audio_tracks(old) == {"microphone": old / "mikrofon.flac",
                                         "system": old / "spotkanie.flac"}
    assert session.audio_tracks(new) == {"microphone": new / "microphone.flac"}


def test_broken_or_missing_meta(tmp_path):
    assert session.read_meta(tmp_path) == {}
    (tmp_path / "meta.json").write_text("{broken", encoding="utf-8")
    assert session.read_meta(tmp_path) == {}
    assert session.normalize_meta({"segmenty": "nonsense"})["segments"] == []
    assert session.normalize_meta({"segmenty": [{"start": "x"}, 5]})["segments"] == []


def test_list_and_dates(tmp_path):
    _old_session(tmp_path)
    _new_session(tmp_path)
    (tmp_path / "note.txt").write_text("not a session", encoding="utf-8")
    assert [s.name for s in session.list_sessions(tmp_path)] == [
        "2026-11-12_14-30_Hiring", "2026-11-10_10-00_Budzet"]
    assert session.list_sessions(tmp_path / "missing") == []
    assert session.date_from_name("2026-11-12_14-30_Hiring") == "2026-11-12 14:30"
    assert session.date_from_name("whatever") == ""
