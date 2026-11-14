# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Optional Parakeet backend: result shape, sentence split, long-audio split, routing.

A fake recognizer stands in for sherpa-onnx, so neither the package nor the
model is needed.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from ucho import parakeet, transcribe
from ucho.merge import Segment, merge_tracks

RATE = 16_000


class FakeStream:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.audio = None
        self.result = None

    def accept_waveform(self, rate, audio):
        assert rate == RATE
        self.audio = np.asarray(audio)


class FakeRecognizer:
    """Returns one sentence per piece, with token times like sherpa-onnx."""

    def __init__(self, tokens=None, times=None):
        self.pieces: list[np.ndarray] = []
        self.tokens = tokens
        self.times = times

    def create_stream(self):
        return FakeStream(self)

    def decode_stream(self, stream):
        self.pieces.append(stream.audio)
        n = len(self.pieces)
        tokens = self.tokens or [" Zdanie", " numer", f" {n}", "."]
        times = self.times or [0.1 * i for i in range(len(tokens))]
        stream.result = SimpleNamespace(tokens=tokens, timestamps=times, text="".join(tokens))


def make_engine(recognizer: FakeRecognizer, monkeypatch) -> parakeet.Engine:
    monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: "unused")
    return parakeet.Engine(device="GPU", recognizer_factory=lambda folder, threads, files: recognizer)


def speech(seconds: float, pause_every: float = 6.0, level: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    audio = (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    period = int(pause_every * RATE)
    for start in range(period, audio.size, period):
        audio[start:start + int(0.6 * RATE)] = 0.0
    return audio


def test_sentences_split_on_punctuation_before_a_new_word():
    result = SimpleNamespace(
        tokens=[" Dzień", " dobry", ".", " Spotkanie", " o", " 10", ".", "00", "?"],
        timestamps=[0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.2, 2.3, 2.6],
    )
    chunks = parakeet.sentences(result, offset=10.0, length=3.0)
    assert [c.text for c in chunks] == ["Dzień dobry.", "Spotkanie o 10.00?"]
    assert chunks[0].start_ts == pytest.approx(10.0)
    assert chunks[0].end_ts == pytest.approx(11.2)  # where the next sentence starts
    assert chunks[1].end_ts == pytest.approx(13.0)  # the end of the piece


def test_sentences_without_tokens_fall_back_to_the_whole_text():
    result = SimpleNamespace(tokens=[], timestamps=[], text=" Tekst bez znaczników ")
    chunks = parakeet.sentences(result, offset=5.0, length=2.0)
    assert [(c.start_ts, c.end_ts, c.text) for c in chunks] == [(5.0, 7.0, "Tekst bez znaczników")]
    assert parakeet.sentences(SimpleNamespace(tokens=[], timestamps=[], text=""), 0, 1) == []


def test_engine_runs_on_cpu_whatever_was_requested(monkeypatch):
    engine = make_engine(FakeRecognizer(), monkeypatch)
    assert engine.requested_device == "CPU"
    assert engine.device == "CPU"
    assert engine.threads in (1, 2)


def test_generate_returns_a_whisper_shaped_result(monkeypatch):
    engine = make_engine(FakeRecognizer(), monkeypatch)
    result = engine.generate(speech(5), "<|pl|>")
    assert result.texts == ["Zdanie numer 1."]
    assert str(result) == "Zdanie numer 1."
    assert len(result.chunks) == 1
    assert result.chunks[0].start_ts == pytest.approx(0.0)


def test_long_audio_is_split_into_short_pieces_with_correct_offsets(monkeypatch):
    recognizer = FakeRecognizer()
    engine = make_engine(recognizer, monkeypatch)
    result = engine.generate(speech(65), None)
    assert len(recognizer.pieces) >= 4
    assert all(p.size <= parakeet.MAX_PIECE_S * RATE for p in recognizer.pieces)
    starts = [c.start_ts for c in result.chunks]
    assert starts == sorted(starts)
    assert starts[-1] > 40.0


def test_release_drops_the_recognizer_and_load_builds_it_once(monkeypatch):
    built = []

    def factory(folder, threads, files):
        built.append(threads)
        assert files == parakeet.FILES[parakeet.REPO_ID]
        return FakeRecognizer()

    monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: "unused")
    engine = parakeet.Engine(threads=2, recognizer_factory=factory)
    engine.load()
    engine.load()
    assert built == [2]
    engine.release()
    assert engine._pipe is None
    engine.generate(speech(2), None)
    assert built == [2, 2]


def test_transcribe_file_gives_segments_that_merge_like_whisper(monkeypatch, tmp_path):
    import soundfile as sf

    wav = tmp_path / "meeting.wav"
    sf.write(str(wav), speech(30), RATE)
    engine = make_engine(FakeRecognizer(), monkeypatch)
    segments = transcribe.transcribe_file(wav, engine, "meeting", language=None)
    assert segments and all(isinstance(s, Segment) for s in segments)
    assert all(0.0 <= s.start <= s.end <= 30.0 for s in segments)
    merged = merge_tracks(segments)
    assert merged and merged[0].speaker == "meeting"


def test_dictation_uses_the_joined_text(monkeypatch):
    engine = make_engine(FakeRecognizer(tokens=[" Wyślij", " raport", "."], times=[0.0, 0.3, 0.6]),
                         monkeypatch)
    assert transcribe.recognize_dictation(engine, speech(3), "pl") == "Wyślij raport."


def test_int8_export_uses_its_own_file_names(monkeypatch):
    seen = []
    monkeypatch.setattr(transcribe, "model_path", lambda repo, download=False: "unused")
    engine = parakeet.Engine(parakeet.REPO_ID_INT8,
                             recognizer_factory=lambda f, n, files: seen.append(files) or FakeRecognizer())
    engine.load()
    assert seen == [parakeet.FILES[parakeet.REPO_ID_INT8]]
    assert all(".int8." in name for name in seen[0][:3])


def test_missing_package_gives_a_clear_error(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def no_sherpa(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sherpa)
    with pytest.raises(RuntimeError, match="-WithParakeet"):
        parakeet._recognizer(tmp_path, 2, parakeet.FILES[parakeet.REPO_ID])


def test_create_engine_routes_by_model(monkeypatch):
    from ucho import config

    assert config.MODELS[parakeet.REPO_ID]["backend"] == parakeet.BACKEND
    assert isinstance(transcribe.create_engine(parakeet.REPO_ID, "GPU"), parakeet.Engine)
    assert isinstance(transcribe.create_engine(parakeet.REPO_ID_INT8, "CPU"), parakeet.Engine)
    assert isinstance(transcribe.create_engine(config.DEFAULT_MODEL, "GPU"), transcribe.Engine)


def test_parakeet_is_offered_only_when_installed(monkeypatch):
    from ucho import config

    monkeypatch.setattr(parakeet, "available", lambda: False)
    offered = transcribe.offered_models()
    assert parakeet.REPO_ID not in offered
    assert offered[0] == config.DEFAULT_MODEL
    monkeypatch.setattr(parakeet, "available", lambda: True)
    assert parakeet.REPO_ID in transcribe.offered_models()


def test_parakeet_is_pinned_and_never_the_default():
    from ucho import config, hardware

    for repo in (parakeet.REPO_ID, parakeet.REPO_ID_INT8):
        assert len(config.REVISIONS[repo]) == 40
        assert set(parakeet.FILES[repo]) <= set(config.DOWNLOAD_PATTERNS[repo])
    assert config.DEFAULT_MODEL != parakeet.REPO_ID
    assert config.DEFAULTS["dictation_model"] != parakeet.REPO_ID
    assert all(parakeet.REPO_ID not in (r.model, r.dictation_model) for r in hardware.RULES.values())


def test_token_durations_keep_pauses_so_paragraphs_split_like_whisper():
    # Two sentences 4 s apart, token times and durations as sherpa-onnx reports them.
    result = SimpleNamespace(
        tokens=[" Napisz", " test", ".", " Wyślij", " raport", "."],
        timestamps=[0.8, 1.2, 1.6, 6.0, 6.4, 6.9],
        durations=[0.3, 0.3, 0.2, 0.3, 0.4, 0.2],
    )
    chunks = parakeet.sentences(result, offset=0.0, length=9.0)
    assert [(c.start_ts, c.end_ts) for c in chunks] == [
        pytest.approx((0.8, 1.8)), pytest.approx((6.0, 7.1))]
    segments = [Segment(c.start_ts, c.end_ts, c.text, "meeting") for c in chunks]
    assert [s.text for s in merge_tracks(segments)] == ["Napisz test.", "Wyślij raport."]
