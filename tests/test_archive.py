# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Shared archive search used by the app's recordings list and the MCP server."""
import json
import sqlite3
from pathlib import Path

import pytest

from ucho.archive import Archive, ArchiveError, fold, match_spans


def _session(folder: Path, name: str, meta: dict) -> None:
    session = folder / name
    session.mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def archive(tmp_path: Path) -> Archive:
    _session(tmp_path, "2026-11-10_10-00_Budzet", {
        "nazwa": "Budżet", "segmenty": [
            {"start": 5, "koniec": 9, "tekst": "Zaczynamy od budżetu.", "mowca": "Ja"}]})
    _session(tmp_path, "2026-11-12_14-30_Rekrutacja", {
        "name": "Rekrutacja", "topic": "Rekrutacja programistów",
        "tldr": "Trzech kandydatów, rozmowy w środę.",
        "segments": [
            {"start": 10, "end": 15, "text": "Mamy trzech kandydatów.", "speaker": "meeting"},
            {"start": 200, "end": 205, "text": "Budżet na rekrutację jest zatwierdzony.",
             "speaker": "me"}]})
    _session(tmp_path, "2026-11-13_09-00_Standup", {"name": "Standup", "segments": []})
    return Archive(tmp_path)


def test_fold_removes_diacritics():
    assert fold("Zażółć GĘŚLĄ jaźń") == "zazolc gesla jazn"


def test_empty_query_lists_everything_newest_first(archive):
    names = [m.session.name for m in archive.find("")]
    assert names == ["2026-11-13_09-00_Standup", "2026-11-12_14-30_Rekrutacja",
                     "2026-11-10_10-00_Budzet"]


def test_find_by_transcript_without_diacritics(archive):
    matches = archive.find("budzet")
    assert [m.session.name for m in matches] == ["2026-11-12_14-30_Rekrutacja",
                                                 "2026-11-10_10-00_Budzet"]
    assert matches[0].hits[0].start == 200
    assert matches[1].title_match  # the old-format name "Budżet"


def test_find_by_title_summary_and_folder_name(archive):
    assert [m.session.name for m in archive.find("programist")] == ["2026-11-12_14-30_Rekrutacja"]
    assert [m.session.name for m in archive.find("srodę")] == ["2026-11-12_14-30_Rekrutacja"]
    match = archive.find("standup")[0]
    assert match.title_match and not match.hits


def test_index_follows_changes(archive):
    assert archive.find("faktur") == []
    _session(archive.folder, "2026-11-14_08-00_Finanse", {
        "segments": [{"start": 1, "end": 2, "text": "Wysłałem fakturę.", "speaker": "me"}]})
    assert [m.session.name for m in archive.find("faktur")] == ["2026-11-14_08-00_Finanse"]


def test_search_rejects_empty_query(archive):
    with pytest.raises(ArchiveError):
        archive.search("  ...  ", 10)


@pytest.mark.parametrize("bad_id", ["..", "../x", "a/b", "C:\\x", "missing"])
def test_session_id_stays_inside(archive, bad_id):
    with pytest.raises(ArchiveError):
        archive.session(bad_id)


@pytest.fixture
def polish(tmp_path: Path) -> Archive:
    _session(tmp_path, "2026-11-15_10-00_Delegacja", {
        "name": "Delegacja", "segments": [
            {"start": 1, "end": 4, "text": "Hotel kosztował dwieście złotych.", "speaker": "me"},
            {"start": 5, "end": 9, "text": "Anna wyłożyła pieniądze, potem wyłożył Jan.",
             "speaker": "meeting"},
            {"start": 10, "end": 12, "text": "Spotkanie jest w Łodzi, nie w Łódź Kaliskiej.",
             "speaker": "Anna"}]})
    return Archive(tmp_path)


@pytest.mark.parametrize("query, expected", [
    ("zlotych", "złotych"),
    ("wylozyl", "wyłożył"),
    ("lodz", "Łódź"),
    ("LODZI", "Łodzi"),
    ("złotych", "złotych"),
    ("Łódź", "Łódź"),
    ("wyłoż", "wyłożyła"),
])
def test_search_without_polish_letters(polish, query, expected):
    hits = polish.search(query, 10)
    assert hits, query
    assert any(expected in hit.text for hit in hits)


def test_results_keep_the_original_text(polish):
    hit = polish.search("zlotych", 10)[0]
    assert hit.text == "Hotel kosztował dwieście złotych."
    assert polish.find("lodz kaliskiej")[0].hits[0].text.startswith("Spotkanie jest w Łodzi")


def test_prefix_rule_is_per_word(polish):
    assert polish.search("wylozyl jan", 10)[0].start == 5
    assert polish.search("otych", 10) == []  # inside a word: no match, as in FTS5


def test_match_spans_point_into_the_original_text():
    text = "Spotkanie w Łodzi: 200 złotych, wyłożył Jan."
    spans = match_spans(text, ["lodz", "ZLOT", "wylozyl"])
    assert [text[s:e] for s, e in spans] == ["Łodz", "złot", "wyłożył"]
    assert match_spans("Zażółć", ["zazolc"]) == [(0, 6)]
    assert match_spans("ﬁnanse", ["fin"]) == [(0, 2)]  # a ligature folds to two letters


def test_search_without_fts5_matches_the_same(polish):
    fts = sorted((h.id, h.start) for h in polish.search("lodz", 10))
    plain = Archive(polish.folder)
    real_connect = sqlite3.connect

    class NoFts:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if "fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self.db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.db, name)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sqlite3, "connect", lambda *a, **k: NoFts(real_connect(*a, **k)))
        assert sorted((h.id, h.start) for h in plain.search("lodz", 10)) == fts
        assert [h.start for h in plain.search("wylozyl", 10)] == [5]
        assert plain.search("otych", 10) == []
        assert plain.search("100%_", 10) == []
        assert not plain.fts
