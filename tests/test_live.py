# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Live (piecewise) dictation tests: cutter, helpers, recognize_piece, LiveDictation, Microphone."""
from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from ucho import config, live, transcribe
from ucho import engine as engine_mod

RATE = live.SAMPLE_RATE


def tone(seconds: float, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(round(seconds * RATE))) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * RATE)), dtype=np.float32)


# --- PieceCutter -------------------------------------------------------------


def test_cutter_cuts_at_08s_pause_and_not_03s_pause():
    stream = np.concatenate([
        tone(2.5), silence(0.3), tone(1.5), silence(0.8), tone(1.0),
    ])
    cutter = live.PieceCutter()
    pieces = cutter.feed(stream)
    tail = cutter.tail()

    assert len(pieces) == 1
    assert pieces[0].dtype == np.float32
    assert pieces[0].flags["C_CONTIGUOUS"]
    # Cut falls around the middle of the 0.8 s pause (at 4.3 s + 0.3 s = 4.6 s).
    assert 4.4 * RATE <= pieces[0].size <= 4.9 * RATE
    assert np.array_equal(np.concatenate([*pieces, tail]), stream)
    assert cutter.pieces == 1
    assert cutter.pending_s == 0.0


def test_cutter_respects_min_piece_s():
    # Pause at 1.0 s is before MIN_PIECE_S (2.0 s) so it must NOT cut there.
    stream = np.concatenate([
        tone(1.0), silence(0.8), tone(1.5), silence(0.8), tone(0.5),
    ])
    cutter = live.PieceCutter()
    pieces = cutter.feed(stream)
    tail = cutter.tail()

    assert len(pieces) == 1
    assert pieces[0].size >= live.MIN_PIECE_S * RATE
    assert np.array_equal(np.concatenate([*pieces, tail]), stream)


def test_cutter_respects_max_piece_s_and_quietest_point():
    # 25 s without a pause, with a brief quieter dip at ~17.5 s inside [15, 20].
    stream = np.concatenate([
        tone(17.0, amp=0.3), tone(1.0, amp=0.01), tone(7.0, amp=0.3),
    ])
    cutter = live.PieceCutter()
    pieces = cutter.feed(stream)
    tail = cutter.tail()

    assert len(pieces) == 1
    assert pieces[0].size <= live.MAX_PIECE_S * RATE
    # Cut lands inside the quiet dip around 17-18 s.
    assert 16.5 * RATE <= pieces[0].size <= 18.5 * RATE
    assert np.array_equal(np.concatenate([*pieces, tail]), stream)


def test_cutter_block_size_independence_and_concat_invariant():
    stream = np.concatenate([
        tone(3.0), silence(0.8),
        tone(2.5), silence(0.3), tone(2.0), silence(0.8),
        tone(22.0), silence(0.8),
        tone(1.2),
    ])

    def run(block_s: float | None) -> tuple[list[np.ndarray], np.ndarray]:
        cutter = live.PieceCutter()
        pieces: list[np.ndarray] = []
        if block_s is None:
            pieces.extend(cutter.feed(stream))
        else:
            step = int(round(block_s * RATE))
            for i in range(0, stream.size, step):
                pieces.extend(cutter.feed(stream[i:i + step]))
        return pieces, cutter.tail()

    p_one, t_one = run(None)
    p_01, t_01 = run(0.1)
    p_025, t_025 = run(0.25)

    assert len(p_one) == len(p_01) == len(p_025) >= 3
    for a, b, c in zip(p_one, p_01, p_025, strict=True):
        assert np.array_equal(a, b)
        assert np.array_equal(a, c)
    assert np.array_equal(t_one, t_01)
    assert np.array_equal(t_one, t_025)
    assert np.array_equal(np.concatenate([*p_01, t_01]), stream)


def test_cutter_empty_feed_and_tail():
    cutter = live.PieceCutter()
    assert cutter.feed(np.zeros(0, dtype=np.float32)) == []
    assert cutter.pending_s == 0.0
    assert cutter.tail().size == 0


# --- join_texts, prompt_from, finish_text ------------------------------------


def test_join_texts_and_prompt_from():
    assert live.join_texts(["", "  hello\nworld  ", "   ", "again\t!"]) == "hello world again !"
    assert live.prompt_from(["", "   "]) is None
    assert live.prompt_from(["short text"], limit=20) == "short text"

    texts = ["First sentence here.", "Second sentence is longer."]
    p = live.prompt_from(texts, limit=20)
    assert p is not None and len(p) <= 20
    # Must start on a full word boundary (partial first word dropped).
    joined = live.join_texts(texts)
    assert joined.endswith(p)
    pos = len(joined) - len(p)
    assert pos == 0 or joined[pos - 1] == " "

    assert live.prompt_from(["supercalifragilistic"], limit=5) is None


def test_finish_text_filters_and_dictionary():
    assert transcribe.finish_text("Spotkanie   jutro.\nDziękuję za uwagę.") == "Spotkanie jutro."
    assert transcribe.finish_text("Tak.") == ""
    assert transcribe.finish_text("thank you.") == ""
    assert transcribe.finish_text("Dziękuję za uwagę.") == ""
    assert transcribe.finish_text("Używamy Cloud Code codziennie.",
                                  {"Cloud Code": "Claude Code"}) == "Używamy Claude Code codziennie."


# --- recognize_piece & Engine.generate ---------------------------------------


def test_recognize_piece_skips_silence_without_loading():
    class NoLoadEngine:
        def load(self):
            raise AssertionError("load should not be called on silence")

        def generate(self, audio, language):
            raise AssertionError("generate should not be called on silence")

    assert transcribe.recognize_piece(NoLoadEngine(), silence(1.5), "auto") == ("", None)
    assert transcribe.recognize_piece(NoLoadEngine(), tone(0.1), "pl") == ("", None)


def test_recognize_piece_language_prompt_and_no_filters():
    calls = []

    class TwoArgEngine:
        def load(self):
            calls.append("load")

        def generate(self, audio, language):
            calls.append(("gen2", language))
            return SimpleNamespace(texts=["  Tak.   Cloud Code.  Dziękuję za uwagę. "],
                                   languages=["<|pl|>"])

    # Without prompt, positional 2-arg call works and no filters/dictionary run.
    text, lang = transcribe.recognize_piece(TwoArgEngine(), tone(1.0), "auto")
    assert text == "Tak. Cloud Code. Dziękuję za uwagę."
    assert lang == "pl"
    assert calls == ["load", ("gen2", None)]

    class PromptEngine:
        def load(self):
            pass

        def generate(self, audio, language, prompt=None):
            calls.append(("gen3", language, prompt))
            return SimpleNamespace(texts=["Hello"], languages=["en"])

    calls.clear()
    assert transcribe.recognize_piece(PromptEngine(), tone(1.0), "en", prompt="prev") == ("Hello", "en")
    assert calls == [("gen3", "<|en|>", "prev")]

    # Forced language returns its code even if result has no languages attribute.
    class NoLangResultEngine:
        def load(self):
            pass

        def generate(self, audio, language):
            return SimpleNamespace(texts=["Cześć"], languages=["<|de|>"])

    assert transcribe.recognize_piece(NoLangResultEngine(), tone(1.0), "pl") == ("Cześć", "pl")
    assert transcribe.recognize_piece(NoLangResultEngine(), tone(1.0), "auto") == ("Cześć", None)


def test_engine_generate_initial_prompt_fallback(caplog):
    eng = transcribe.Engine("dummy", "CPU")
    seen = []

    class OldPipe:
        def generate(self, audio, **kwargs):
            if "initial_prompt" in kwargs:
                raise TypeError("unexpected keyword argument 'initial_prompt'")
            seen.append(dict(kwargs))
            return SimpleNamespace(texts=["ok"])

    eng._pipe = OldPipe()
    with caplog.at_level(logging.WARNING):
        eng.generate(tone(1.0), "<|pl|>", prompt="previous words")
        eng.generate(tone(1.0), "<|pl|>", prompt="more words")

    assert len(seen) == 2
    assert all("initial_prompt" not in kw for kw in seen)
    assert sum("initial_prompt" in r.message for r in caplog.records) == 1


# --- LiveDictation with real Engine ------------------------------------------


class LiveFakePipe:
    def __init__(self, handler, model: str = "M", device: str = "GPU"):
        self.repo_id = model
        self.requested_device = device
        self.device = device
        self._pipe = object()
        self.handler = handler
        self.calls: list[tuple[int, str | None, str | None]] = []

    def load(self):
        pass

    def release(self):
        self._pipe = None

    def generate(self, audio, language, prompt=None):
        self.calls.append((audio.size, language, prompt))
        return self.handler(len(self.calls) - 1, audio, language, prompt)


def _make_live_engine(handler):
    pipes: list[LiveFakePipe] = []

    def factory(model, device):
        pipe = LiveFakePipe(handler, model, device)
        pipes.append(pipe)
        return pipe

    eng = engine_mod.Engine(factory)
    eng.start()
    return eng, pipes


def test_live_dictation_end_to_end_pins_language_uses_prompt_and_filters_once():
    responses = [
        SimpleNamespace(texts=["Tak."], languages=["<|pl|>"]),
        SimpleNamespace(texts=["Spotkanie w Cloud Code."], languages=["<|en|>"]),
        SimpleNamespace(texts=["Dziękuję za uwagę."], languages=["<|pl|>"]),
    ]
    eng, pipes = _make_live_engine(lambda idx, _a, _l, _p: responses[idx])
    updates: list[str] = []
    session = live.LiveDictation(
        eng, "M", "GPU", "auto", {"Cloud Code": "Claude Code"}, on_text=updates.append,
    )

    stream = np.concatenate([
        tone(2.5), silence(0.8),
        tone(2.5), silence(0.8),
        tone(1.2),
    ])
    step = int(0.1 * RATE)
    submitted_during_feed = 0
    for i in range(0, stream.size, step):
        submitted_during_feed += session.feed(stream[i:i + step])

    assert submitted_during_feed == 2
    before_finish = session.submitted
    assert before_finish == 2

    final_job = session.finish(stream)
    assert session.submitted == before_finish + 1

    text, compute_s, device = final_job.wait(10)
    eng.stop()

    assert device == "GPU"
    assert compute_s >= 0.0
    # "Tak." was kept because it was joined with later text; outro at the end was stripped;
    # dictionary ran once on the joined text.
    assert text == "Tak. Spotkanie w Claude Code."
    assert updates == ["Tak.", "Tak. Spotkanie w Cloud Code."]

    # Language pinned to "pl" after the first piece; prompts carried earlier text.
    calls = pipes[0].calls
    assert len(calls) == 3
    assert calls[0][1:] == (None, None)
    assert calls[1][1:] == ("<|pl|>", "Tak.")
    assert calls[2][1:] == ("<|pl|>", "Tak. Spotkanie w Cloud Code.")
    assert session.language == "pl"


def test_live_dictation_silent_tail_makes_no_extra_generate_call():
    eng, pipes = _make_live_engine(
        lambda _i, _a, _l, _p: SimpleNamespace(texts=["Cześć."], languages=["<|pl|>"])
    )
    session = live.LiveDictation(eng, "M", "GPU", "pl")
    stream = np.concatenate([tone(2.5), silence(0.8)])
    assert session.feed(stream) == 1
    text, _sec, _dev = session.finish(stream).wait(10)
    eng.stop()
    assert text == "Cześć."
    assert len(pipes[0].calls) == 1


def test_live_dictation_cancel_and_post_finish_guards():
    first_started = threading.Event()
    release_first = threading.Event()

    def handler(idx, _a, _l, _p):
        if idx == 0:
            first_started.set()
            release_first.wait(5)
        return SimpleNamespace(texts=[f"piece {idx}"], languages=["<|pl|>"])

    eng, pipes = _make_live_engine(handler)
    session = live.LiveDictation(eng, "M", "GPU", "auto")

    # Feed two pieces: piece 0 runs and blocks, piece 1 stays queued.
    session.feed(np.concatenate([tone(2.5), silence(0.8), tone(2.5), silence(0.8)]))
    assert first_started.wait(5)
    session.cancel()
    release_first.set()

    # After cancel, feed() is a no-op and finish() raises RuntimeError.
    assert session.feed(np.concatenate([tone(2.5), silence(0.8)])) == 0
    with pytest.raises(RuntimeError):
        session.finish()

    eng.submit(lambda _j: None, engine_mod.BACKGROUND).wait(5)
    eng.stop()

    assert len(pipes[0].calls) == 1
    assert session.text == ""
    assert not session.failed

    # feed() after finish() raises RuntimeError.
    eng2, _ = _make_live_engine(lambda _i, _a, _l, _p: SimpleNamespace(texts=["x"]))
    s2 = live.LiveDictation(eng2, "M", "GPU", "pl")
    s2.finish(silence(0.5)).wait(5)
    with pytest.raises(RuntimeError):
        s2.feed(tone(0.5))
    with pytest.raises(RuntimeError):
        s2.finish()
    eng2.stop()


def test_live_dictation_fallback_on_failed_piece():
    def handler(idx, audio, _l, _p):
        if audio.size < 4 * RATE:
            raise RuntimeError("piece boom")
        return SimpleNamespace(texts=["Całe nagranie."], languages=["<|pl|>"])

    eng, pipes = _make_live_engine(handler)
    session = live.LiveDictation(eng, "M", "GPU", "auto")
    stream = np.concatenate([tone(2.5), silence(0.8), tone(2.0)])
    assert session.feed(stream) == 1

    text, _sec, _dev = session.finish(stream).wait(10)
    eng.stop()

    assert session.failed is True
    assert text == "Całe nagranie."
    assert len(pipes[0].calls) == 2
    assert pipes[0].calls[1][0] == stream.size


# --- Microphone.take / stop & config ----------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
def test_microphone_take_and_stop_match_prefix():
    from ucho import dictation

    mic = dictation.Microphone(None)
    mic._rate = 16_000
    mic._channels = 2
    mic._reset_take()

    assert mic.take().size == 0
    assert mic.taken_samples == 0

    frames = (np.linspace(-0.5, 0.5, 4800, dtype=np.float32).reshape(-1, 2) * 32768.0).astype(np.int16)
    b1 = frames[:800].tobytes()
    b2 = frames[800:1600].tobytes()
    b3 = frames[1600:].tobytes()

    mic._blocks.append(b1)
    p1 = mic.take()
    assert len(mic._blocks) == 1  # _blocks is never cleared in take()

    mic._blocks.append(b2)
    p2 = mic.take()
    assert mic.taken_samples == p1.size + p2.size

    mic._blocks.append(b3)
    taken_count = mic.taken_samples
    full = mic.stop()

    assert full[:taken_count].size == taken_count
    assert np.array_equal(full[:taken_count], np.concatenate([p1, p2]))
    assert full.size == frames.shape[0]
    assert mic.taken_samples == 0

    # Without take(), stop() still works via the original path.
    mic._blocks = [b1, b2, b3]
    full_direct = mic.stop()
    assert np.array_equal(full_direct, full)


def test_config_has_dictation_live_default():
    assert "dictation_live" in config.DEFAULTS
    assert isinstance(config.DEFAULTS["dictation_live"], bool)


# --- DictationController with live recognition -------------------------------------


class _FakeMicrophone:
    """Hands out a prepared stream in ticks, like Microphone.take()/stop()."""

    def __init__(self, stream: np.ndarray, step: int):
        self.stream, self.step = stream, step
        self.position = 0
        self.taken_samples = 0
        self.level = 0.0
        self.name = "fake"
        self.ready = threading.Event()

    def choose(self, *_args):
        pass

    def start(self):
        self.position = self.taken_samples = 0

    def take(self):
        part = self.stream[self.position:self.position + self.step]
        self.position += part.size
        self.taken_samples += part.size
        return part

    def stop(self):
        return self.stream.copy()

    def close(self):
        pass


class _FakeApp:
    def __init__(self, engine, live_on: bool):
        self.engine = engine
        self.settings = {"microphone_name": None, "microphone_index": None,
                         "dictation_live": live_on, "dictation_language": "auto",
                         "dictation_dictionary": {}, "dictation_sounds": False,
                         "dictation_mode": "paste", "dictation_enter": False}
        self.scheduled: list = []
        self.hotkeys = SimpleNamespace(escape=lambda _on: None)

    def post(self, fn):
        fn()

    def after(self, _ms, fn):
        self.scheduled.append(fn)

    def run_scheduled(self):
        while self.scheduled:
            self.scheduled.pop(0)()


@pytest.mark.skipif(sys.platform != "win32", reason="ucho.dictation uses WinAPI")
@pytest.mark.parametrize("live_on", [True, False])
def test_controller_stop_submits_only_the_tail(monkeypatch, live_on):
    from ucho import dictation

    inserted: list[str] = []
    monkeypatch.setattr(dictation, "dictation_target", lambda: None)
    monkeypatch.setattr(dictation, "insert_text",
                        lambda text, _mode, _enter, _target, report: (inserted.append(text),
                                                                       report(dictation.PASTE)))
    monkeypatch.setattr(dictation.DictationController, "model_key", lambda self: ("M", "GPU"))
    texts = iter(["Pierwsze zdanie.", "Drugie zdanie.", "Ostatnie."])
    eng, pipes = _make_live_engine(
        lambda _i, _a, _l, _p: SimpleNamespace(texts=[next(texts)], languages=["<|pl|>"]))
    app = _FakeApp(eng, live_on)
    controller = dictation.DictationController(app)
    stream = np.concatenate([tone(2.5), silence(0.8), tone(2.5), silence(0.8), tone(1.5)])
    controller.microphone = _FakeMicrophone(stream, int(0.25 * RATE))

    assert controller.start()
    # Ticks while speaking: everything but the last 1.6 s is taken.
    while controller.microphone.position < stream.size - int(1.6 * RATE):
        tick = app.scheduled.pop(0) if app.scheduled else None
        if tick is None:
            break
        tick()
    eng.submit(lambda _j: None, engine_mod.DICTATION).wait(10)  # pieces done
    calls_before_stop = len(pipes[0].calls) if pipes else 0
    assert calls_before_stop == (2 if live_on else 0)
    if live_on:
        assert controller.live_text == "Pierwsze zdanie. Drugie zdanie."

    controller.stop()
    app.scheduled = [controller._finish]
    app.run_scheduled()
    # The final job pastes from its own thread, started when the engine is done.
    for _ in range(200):
        thread = getattr(controller, "_insert_thread", None)
        if thread is not None:
            thread.join(10)
            break
        threading.Event().wait(0.05)
    eng.stop()

    if live_on:
        assert len(pipes[0].calls) == 3
        assert pipes[0].calls[2][0] < 2.0 * RATE  # only the tail went to the model
        assert inserted == ["Pierwsze zdanie. Drugie zdanie. Ostatnie. "]
    else:
        assert len(pipes[0].calls) == 1
        assert pipes[0].calls[0][0] > 7 * RATE
        assert inserted == ["Pierwsze zdanie. "]
    assert controller.live_text == ""
