# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
import json

from ucho import i18n
from ucho.merge import (Segment, as_markdown, as_srt, as_text, clean, format_hms, merge_tracks,
                        save_results, slug)


def test_clean_drops_whisper_hallucinations():
    segments = [
        Segment(0.0, 2.0, "Napisy stworzone przez społeczność Amara.org", "meeting"),
        Segment(2.0, 4.0, "Dziękuję za uwagę.", "meeting"),
        Segment(4.0, 6.0, "Thank you.", "meeting"),
        Segment(6.0, 9.0, "Zaczynamy od budżetu na czwarty kwartał.", "me"),
    ]
    result = clean(segments)
    assert [s.text for s in result] == ["Zaczynamy od budżetu na czwarty kwartał."]


def test_clean_drops_too_short_and_repeated():
    segments = [
        Segment(0.0, 0.05, "Za krótko", "me"),
        Segment(1.0, 2.0, "To samo zdanie", "me"),
        Segment(2.0, 3.0, "To samo zdanie", "me"),
    ]
    result = clean(segments)
    assert len(result) == 1
    assert result[0].end == 3.0


def test_merge_orders_tracks_by_time_and_joins_close_utterances():
    me = [Segment(0.0, 2.0, "Cześć wszystkim.", "me"), Segment(2.5, 4.0, "Zaczynamy?", "me")]
    meeting = [Segment(5.0, 7.0, "Tak, zaczynajmy.", "meeting")]
    result = merge_tracks(me, meeting)
    assert [(s.speaker, s.text) for s in result] == [
        ("me", "Cześć wszystkim. Zaczynamy?"),
        ("meeting", "Tak, zaczynajmy."),
    ]


def test_slug_without_polish_letters_and_underscores():
    assert slug("Budżet Q4_zespół") == "Budzet-Q4-zespol"
    assert len(slug("a" * 100)) == 48


def test_format_hms():
    assert format_hms(3725.9) == "01:02:05"
    assert format_hms(-5) == "00:00:00"


def test_srt_has_numbering_and_minimum_duration():
    srt = as_srt([Segment(1.0, 1.1, "Krótko", "me")])
    assert srt.splitlines()[:3] == ["1", "00:00:01,000 --> 00:00:01,500", "Me: Krótko"]


def test_markdown_without_speech():
    text = as_markdown([], {"name": "Test", "duration_s": 61})
    assert "# Transcript: Test" in text
    assert "00:01:01" in text
    assert "No speech was recognized." in text


def test_speaker_labels_are_localized_on_output():
    segments = [Segment(0.0, 2.0, "Dzień dobry.", "me"), Segment(3.0, 5.0, "Witam.", "meeting")]
    assert as_text(segments) == "Me: Dzień dobry.\nMeeting: Witam.\n"
    i18n.set_language("pl")
    assert as_text(segments) == "Ja: Dzień dobry.\nSpotkanie: Witam.\n"
    assert "**[00:00:00] Ja:** Dzień dobry." in as_markdown(segments, {"name": "Test"})


def test_save_results_writes_new_names_and_english_keys(tmp_path):
    files = save_results(tmp_path, [Segment(0.0, 2.0, "Dzień dobry.", "me")],
                         {"name": "Test", "duration_s": 2})
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "meta.json", "transcript.md", "transcript.srt", "transcript.txt"]
    meta = json.loads(files["meta"].read_text(encoding="utf-8"))
    assert meta["segment_count"] == 1
    assert meta["segments"] == [{"start": 0.0, "end": 2.0, "text": "Dzień dobry.", "speaker": "me"}]
