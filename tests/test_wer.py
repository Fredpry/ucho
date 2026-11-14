# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Word error rate math and file pairing of tools/benchmark_wer.py (no audio, no models)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "benchmark_wer.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_wer", _PATH)
bw = importlib.util.module_from_spec(_SPEC)
sys.modules["benchmark_wer"] = bw  # dataclasses look the module up by name
_SPEC.loader.exec_module(bw)


# --- normalization ---------------------------------------------------------------


def test_normalize_lowercases_and_strips_punctuation():
    assert bw.normalize("Zaczynamy, od budżetu!  Na Q4?") == "zaczynamy od budżetu na q4"


def test_normalize_keeps_polish_letters_and_digits():
    assert bw.normalize("ŻÓŁĆ Gęślą jaźń 2026") == "żółć gęślą jaźń 2026"


def test_normalize_polish_quotes_dashes_and_ellipsis():
    assert bw.normalize("„Tak” – powiedział… (chyba)") == "tak powiedział chyba"


def test_normalize_hyphen_splits_apostrophe_joins():
    assert bw.normalize("time-line") == "time line"
    assert bw.normalize("Don't") == bw.normalize("don’t") == "dont"


def test_normalize_whitespace_and_unicode_forms():
    assert bw.normalize("  a\t\nb c  ") == "a b c"
    # Decomposed "ę" (e + combining ogonek) equals the precomposed one.
    assert bw.normalize("ręka") == bw.normalize("ręka")


def test_normalize_empty():
    assert bw.normalize(" ,.!? ") == ""
    assert bw.words("") == []


# --- alignment ------------------------------------------------------------------------


def test_identical_text_has_zero_errors():
    errors = bw.word_errors("Dzień dobry wszystkim", "dzień dobry, wszystkim.")
    assert errors.total == 0
    assert errors.hits == 3
    assert bw.wer("Dzień dobry wszystkim", "dzień dobry, wszystkim.") == 0.0


def test_substitution_deletion_insertion_are_counted_separately():
    assert bw.word_errors("a b c", "a x c") == bw.Errors(hits=2, substitutions=1)
    assert bw.word_errors("a b c", "a c") == bw.Errors(hits=2, deletions=1)
    assert bw.word_errors("a b c", "a b x c") == bw.Errors(hits=3, insertions=1)


def test_classic_example():
    # Reference 6 words; "the" deleted, "sat"->"sit" substituted, "down" inserted.
    errors = bw.word_errors("the cat sat on the mat", "cat sit on the mat down")
    assert (errors.substitutions, errors.deletions, errors.insertions) == (1, 1, 1)
    assert errors.reference_length == 6
    assert bw.wer("the cat sat on the mat", "cat sit on the mat down") == pytest.approx(3 / 6)


def test_wer_can_exceed_one():
    assert bw.wer("tak", "nie nie nie") == pytest.approx(3.0)


def test_empty_reference():
    assert bw.wer("", "") == 0.0
    assert bw.wer("", "coś") is None
    assert bw.wer("coś tam", "") == 1.0


def test_edit_distance_matches_brute_force_levenshtein():
    def levenshtein(a, b):
        table = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
        for i in range(1, len(a) + 1):
            for j in range(1, len(b) + 1):
                table[i][j] = min(table[i - 1][j] + 1, table[i][j - 1] + 1,
                                  table[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
        return table[-1][-1]

    pairs = [("kitten", "sitting"), ("", "abc"), ("abc", ""), ("flaw", "lawn"),
             ("spotkanie", "spotkania"), ("abcdef", "fedcba")]
    for a, b in pairs:
        errors = bw.align(list(a), list(b))
        assert errors.total == levenshtein(a, b), (a, b)
        assert errors.reference_length == len(a)


def test_cer_counts_characters_of_normalized_text():
    # "kot" -> "kod": one substitution in 3 characters.
    assert bw.cer("Kot.", "kod") == pytest.approx(1 / 3)
    # Space counts as a character: "ab" vs "a b" is one insertion over 2.
    assert bw.cer("ab", "a b") == pytest.approx(1 / 2)


def test_corpus_total_is_micro_average():
    short = bw.score("short", "a b", "a x")           # 1 error / 2 words
    long = bw.score("long", "a b c d e f g h", "a b c d e f g h")  # 0 / 8
    summary = bw.total([short, long])
    assert summary["wer"] == pytest.approx(1 / 10)
    assert summary["reference_words"] == 10
    assert summary["files"] == 2


def test_table_and_compare_render():
    results = [bw.score("clip_1", "jeden dwa", "jeden trzy")]
    summary = bw.total(results)
    table = bw.format_table(results, summary)
    assert "clip_1" in table and "TOTAL" in table and "50.0%" in table
    line = bw.compare(summary, {"total": {"wer": 0.25, "cer": None}})
    assert "+25.0 pp" in line


# --- files ---------------------------------------------------------------------------------


def test_find_items_wer_only_pairs_reference_and_hypothesis(tmp_path):
    (tmp_path / "a.txt").write_text("ref", encoding="utf-8")
    (tmp_path / "a.hyp.txt").write_text("hyp", encoding="utf-8")
    (tmp_path / "b.hyp.txt").write_text("no reference", encoding="utf-8")
    items = bw.find_items(tmp_path, wer_only=True)
    assert [(i.name, i.reference.name, i.hypothesis.name) for i in items] == [("a", "a.txt", "a.hyp.txt")]


def test_find_items_with_audio_needs_reference(tmp_path):
    for name in ("x.flac", "x.txt", "y.wav", "z.mp3", "z.txt", "notes.md"):
        (tmp_path / name).write_bytes(b"")
    items = bw.find_items(tmp_path, wer_only=False)
    assert [(i.name, i.audio.name, i.hypothesis.name) for i in items] == [
        ("x", "x.flac", "x.hyp.txt"), ("z", "z.mp3", "z.hyp.txt")]


def test_references_from_common_voice_tsv(tmp_path):
    (tmp_path / "common_voice_pl_1.mp3").write_bytes(b"")
    (tmp_path / "common_voice_pl_2.flac").write_bytes(b"")
    tsv = tmp_path / "test.tsv"
    tsv.write_text(
        "client_id\tpath\tsentence\tup_votes\n"
        'abc\tcommon_voice_pl_1.mp3\t"Cytat" na początek.\t2\n'
        "abc\tcommon_voice_pl_2.mp3\tDrugie zdanie.\t2\n"
        "abc\tcommon_voice_pl_3.mp3\tBrak pliku.\t2\n",
        encoding="utf-8",
    )
    assert bw.references_from_tsv(tsv, tmp_path) == 2
    assert (tmp_path / "common_voice_pl_1.txt").read_text(encoding="utf-8") == '"Cytat" na początek.\n'
    assert not (tmp_path / "common_voice_pl_3.txt").exists()


def test_main_wer_only_writes_json(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("Ala ma kota.", encoding="utf-8")
    (tmp_path / "a.hyp.txt").write_text("ala ma psa", encoding="utf-8")
    out = tmp_path / "out.json"
    assert bw.main([str(tmp_path), "--wer-only", "--json", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["total"]["wer"] == pytest.approx(1 / 3)
    assert report["files"][0]["word"]["substitutions"] == 1
    assert "TOTAL" in capsys.readouterr().out
    # Scoring must not have loaded the transcription stack.
    assert "openvino_genai" not in sys.modules


def test_main_reports_missing_pairs(tmp_path):
    assert bw.main([str(tmp_path), "--wer-only"]) == 2
