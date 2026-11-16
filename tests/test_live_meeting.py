# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ucho.live_meeting: quantization, chunking, caching, LiveMeeting."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from ucho import engine, live_meeting, merge, transcribe

RATE = live_meeting.SAMPLE_RATE


def make_speech(
    seconds: float,
    seed: int = 42,
    *,
    click_first: bool = True,
) -> np.ndarray:
    """Deterministic synthetic speech: bursts of noise separated by pauses."""
    rng = np.random.default_rng(seed)
    target = int(round(seconds * RATE))
    parts: list[np.ndarray] = []
    total = 0

    if click_first and target > 0:
        # Loud impulse in the first second so auto-mode threshold stays stable.
        lead = min(int(0.5 * RATE), target)
        intro = rng.normal(0.0, 0.001, lead).astype(np.float32)
        if lead > 1600:
            intro[1600:1620] = 0.9
        parts.append(intro)
        total += lead

    speaking = True
    while total < target:
        if speaking:
            dur = float(rng.uniform(0.5, 6.0))
            amp = float(rng.uniform(0.1, 0.3))
            n = min(int(dur * RATE), target - total)
            parts.append((amp * rng.normal(0.0, 1.0, n)).astype(np.float32))
        else:
            dur = float(rng.uniform(0.1, 2.0))
            n = min(int(dur * RATE), target - total)
            parts.append((0.001 * rng.normal(0.0, 1.0, n)).astype(np.float32))
        total += n
        speaking = not speaking

    return np.ascontiguousarray(np.concatenate(parts)[:target], dtype=np.float32)


def feed_random_blocks(
    chunker: live_meeting.TrackChunker,
    track: np.ndarray,
    seed: int = 123,
    min_block: int = 1,
    max_block: int = 9000,
) -> list[tuple[int, int, np.ndarray]]:
    rng = np.random.default_rng(seed)
    out: list[tuple[int, int, np.ndarray]] = []
    pos = 0
    while pos < track.size:
        step = int(rng.integers(min_block, max_block + 1))
        out.extend(chunker.feed(track[pos:pos + step]))
        pos += step
    return out


class SpyEngine(engine.Engine):
    def __init__(self, pipeline_factory):
        super().__init__(pipeline_factory)
        self.hold_calls: list[tuple[str, str]] = []
        self.unhold_calls: list[tuple[str, str]] = []

    def hold(self, key):
        self.hold_calls.append(key)
        super().hold(key)

    def unhold(self, key):
        self.unhold_calls.append(key)
        super().unhold(key)


# --- quantize_pcm16 -----------------------------------------------------------


def test_quantize_pcm16_matches_flac_round_trip(tmp_path: Path):
    rng = np.random.default_rng(7)
    random_samples = rng.uniform(-1.2, 1.2, size=50_000).astype(np.float32)
    special = np.array(
        [
            -1.2,
            -1.0,
            -32767.5 / 32768.0,
            -0.5 / 32768.0,
            0.0,
            0.5 / 32768.0,
            1.5 / 32768.0,
            32767.0 / 32768.0,
            1.0,
            1.2,
        ],
        dtype=np.float32,
    )
    samples = np.concatenate([special, random_samples])

    flac_path = tmp_path / "roundtrip.flac"
    sf.write(str(flac_path), samples, RATE, format="FLAC", subtype="PCM_16")
    from_flac, _ = sf.read(str(flac_path), dtype="float32", always_2d=False)

    quantized = live_meeting.quantize_pcm16(samples)
    assert quantized.dtype == np.float32
    assert quantized.ndim == 1
    assert np.array_equal(quantized, from_flac)
    assert quantized.tobytes() == from_flac.tobytes()
    assert live_meeting.quantize_pcm16(np.empty(0, dtype=np.float32)).size == 0


# --- quiet_run_boundaries & trailing_quiet_start ------------------------------


def test_quiet_run_boundaries_matches_transcribe_on_random_and_edge_cases():
    min_frames = max(1, int(transcribe.MIN_SILENCE_S / transcribe.VAD_FRAME_S))
    frame = int(transcribe.VAD_FRAME_S * RATE)

    edge_cases = [
        (np.array([], dtype=np.float32), 0.1),
        (np.zeros(1, dtype=np.float32), 0.1),
        (np.zeros(min_frames - 1, dtype=np.float32), 0.1),
        (np.zeros(min_frames, dtype=np.float32), 0.1),
        (np.zeros(50, dtype=np.float32), 0.1),
        (np.ones(50, dtype=np.float32), 0.1),
        # Touching start and end, exact min_frames vs min_frames - 1.
        (np.array([0.0] * min_frames + [1.0] * 3 + [0.0] * (min_frames - 1), dtype=np.float32), 0.5),
        (np.array([0.0] * (min_frames - 1) + [1.0] * 3 + [0.0] * min_frames, dtype=np.float32), 0.5),
        # Threshold equality is NOT quiet (energy < threshold).
        (np.full(min_frames + 2, 0.5, dtype=np.float32), 0.5),
    ]
    for energy, threshold in edge_cases:
        expected = transcribe.boundaries_from_energy(energy, threshold)
        actual = live_meeting.quiet_run_boundaries(energy, threshold)
        assert actual == expected
        assert all(type(x) is int for x in actual)

    rng = np.random.default_rng(99)
    for _ in range(250):
        n = int(rng.integers(0, 401))
        threshold = float(rng.uniform(0.01, 0.9))
        # Mix quiet and loud runs to exercise every boundary pattern.
        energy = rng.uniform(0.0, 1.0, size=n).astype(np.float32)
        if n > 0 and rng.random() < 0.5:
            start = int(rng.integers(0, n))
            length = int(rng.integers(1, min(20, n - start) + 1))
            energy[start:start + length] = threshold * 0.5
        expected = transcribe.boundaries_from_energy(energy, threshold)
        actual = live_meeting.quiet_run_boundaries(energy, threshold)
        assert actual == expected
        assert all(type(x) is int for x in actual)
        for b in actual:
            assert b % frame == 0


def test_trailing_quiet_start_edge_cases():
    tqs = live_meeting.trailing_quiet_start
    assert tqs(np.array([], dtype=np.float32), 0.1) == 0
    assert tqs(np.zeros(10, dtype=np.float32), 0.1) == 0
    assert tqs(np.ones(10, dtype=np.float32), 0.1) == 10
    assert tqs(np.array([0.0, 0.0, 0.5, 0.0, 0.0], dtype=np.float32), 0.1) == 3
    assert tqs(np.array([0.0, 0.5, 0.1], dtype=np.float32), 0.1) == 3
    assert tqs(np.array([0.5, 0.0], dtype=np.float32), 0.1) == 1


# --- TrackChunker -------------------------------------------------------------


def test_track_chunker_forced_matches_offline():
    full = live_meeting.quantize_pcm16(make_speech(16 * 60.0, seed=101))
    chunker = live_meeting.TrackChunker(auto_detect=False)
    settled = feed_random_blocks(chunker, full, seed=202, min_block=1, max_block=9000)

    assert chunker.length == full.size
    expected_ranges = transcribe.split_into_chunks(full, auto_detect=False)[:-1]
    assert len(expected_ranges) >= 2
    assert [(b, e) for b, e, _ in settled] == expected_ranges

    prev_end = 0
    for b, e, audio in settled:
        assert b >= prev_end
        assert audio.dtype == np.float32
        assert np.array_equal(audio, full[b:e])
        prev_end = e


def test_track_chunker_auto_matches_offline_and_energy_buffer():
    full = live_meeting.quantize_pcm16(make_speech(3 * 60.0, seed=303, click_first=True))
    chunker = live_meeting.TrackChunker(auto_detect=True)
    settled = feed_random_blocks(chunker, full, seed=404, min_block=1, max_block=9000)

    assert chunker.length == full.size
    expected_energy = transcribe.frame_energy(full)
    assert chunker._energy_buf.dtype == np.float32
    assert np.array_equal(chunker._energy_buf[:chunker._energy_len], expected_energy)

    offline = transcribe.split_into_chunks(full, auto_detect=True)
    settled_ranges = [(b, e) for b, e, _ in settled]
    assert settled_ranges
    for r in settled_ranges:
        assert r in offline
    # Covers all offline ranges except at most the last 2.
    assert set(offline[:-2]).issubset(set(settled_ranges))

    prev_end = 0
    for b, e, audio in settled:
        assert b >= prev_end
        assert audio.dtype == np.float32
        assert np.array_equal(audio, full[b:e])
        prev_end = e


def test_track_chunker_auto_handles_late_loud_burst():
    quiet_part = make_speech(90.0, seed=505, click_first=False) * 0.2
    loud_part = make_speech(90.0, seed=606, click_first=True)
    full = live_meeting.quantize_pcm16(np.concatenate([quiet_part, loud_part]))

    chunker = live_meeting.TrackChunker(auto_detect=True)
    settled = feed_random_blocks(chunker, full, seed=707, min_block=1, max_block=9000)

    assert settled
    prev_end = 0
    for b, e, audio in settled:
        assert b >= prev_end
        assert audio.dtype == np.float32
        assert np.array_equal(audio, full[b:e])
        prev_end = e


# --- ChunkCache ---------------------------------------------------------------


def test_chunk_cache_operations_and_isolation():
    cache = live_meeting.ChunkCache("M-int8", "GPU", "auto")
    assert cache.matches("M-int8", "GPU", "auto")
    assert not cache.matches("M-fp16", "GPU", "auto")
    assert not cache.matches("M-int8", "CPU", "auto")
    assert not cache.matches("M-int8", "GPU", "pl")
    assert len(cache) == 0

    chunk_a = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    chunk_b = np.array([0.1, 0.2, 0.4], dtype=np.float32)
    seg_in = [merge.Segment(0.0, 1.0, "hello", merge.SPEAKER_ME)]
    cache.put("microphone", 100, 200, chunk_a, seg_in)
    seg_in[0].text = "mutated-input"
    assert len(cache) == 1

    # Hit returns independent copies.
    got = cache.get("microphone", 100, 200, chunk_a.copy())
    assert got == [merge.Segment(0.0, 1.0, "hello", merge.SPEAKER_ME)]
    got[0].text = "mutated-output"
    assert cache.get("microphone", 100, 200, chunk_a) == [
        merge.Segment(0.0, 1.0, "hello", merge.SPEAKER_ME)
    ]
    assert cache.hits == 2

    # Misses: different samples at same key, and unknown key.
    assert cache.get("microphone", 100, 200, chunk_b) is None
    assert cache.get("microphone", 200, 300, chunk_a) is None
    assert cache.misses == 2

    # Replacing existing key and ordering in segments().
    cache.put("microphone", 0, 100, chunk_a, [merge.Segment(0.0, 0.5, "first", merge.SPEAKER_ME)])
    cache.put(
        "microphone",
        100,
        200,
        chunk_b,
        [
            merge.Segment(1.0, 1.5, "second-a", merge.SPEAKER_ME),
            merge.Segment(1.6, 2.0, "second-b", merge.SPEAKER_ME),
        ],
    )
    assert len(cache) == 2
    by_track = cache.segments()
    assert [s.text for s in by_track["microphone"]] == ["first", "second-a", "second-b"]


# --- LiveMeeting --------------------------------------------------------------


def test_live_meeting_recognizes_speech_skips_silence_and_balances_hold():
    class DummyPipe:
        def load(self):
            pass

        def release(self):
            pass

    eng = SpyEngine(lambda m, d: DummyPipe())
    eng.start()
    try:
        # Prepare two tracks in auto mode:
        # - system: 60s of speech (click first)
        # - microphone: 25s silence, 10s speech, 25s silence -> has both silent & speech chunks.
        rng = np.random.default_rng(11)
        mic_silent_a = (0.0004 * rng.normal(0.0, 1.0, 25 * RATE)).astype(np.float32)
        mic_speech = make_speech(10.0, seed=12, click_first=True)
        mic_silent_b = (0.0004 * rng.normal(0.0, 1.0, 25 * RATE)).astype(np.float32)
        mic = np.concatenate([mic_silent_a, mic_speech, mic_silent_b])
        sys_audio = make_speech(60.0, seed=13, click_first=True)

        # Compute expected non-silent settled chunks deterministically.
        expected_keys: set[tuple[str, int, int]] = set()
        for name, raw in (("microphone", mic), ("system", sys_audio)):
            c = live_meeting.TrackChunker(auto_detect=True)
            for b, e, a in c.feed(live_meeting.quantize_pcm16(raw)):
                if transcribe.peak_rms(a) >= transcribe.SPEECH_RMS_THRESHOLD:
                    expected_keys.add((name, b, e))
        assert len(expected_keys) >= 4
        # Confirm microphone actually had silent settled chunks that were skipped.
        mic_all = live_meeting.TrackChunker(auto_detect=True).feed(live_meeting.quantize_pcm16(mic))
        assert any(transcribe.peak_rms(a) < transcribe.SPEECH_RMS_THRESHOLD for _, _, a in mic_all)

        recognized_calls: list[tuple[str, int, int, bool]] = []
        update_threads: list[str] = []
        after_stop_calls: list[str] = []
        stopped_event = threading.Event()
        all_done = threading.Event()

        def fake_recognize(pipe, audio, begin, end, language, speaker):
            if stopped_event.is_set():
                after_stop_calls.append("recognize")
            recognized_calls.append((speaker, begin, end, transcribe.peak_rms(audio) >= transcribe.SPEECH_RMS_THRESHOLD))
            return [merge.Segment(begin / RATE, end / RATE, f"{speaker}:{begin}-{end}", speaker)]

        def on_update(segs):
            if stopped_event.is_set():
                after_stop_calls.append("on_update")
            update_threads.append(threading.current_thread().name)
            if lm.done >= len(expected_keys):
                all_done.set()

        lm = live_meeting.LiveMeeting(
            eng,
            "M",
            "GPU",
            "auto",
            on_update=on_update,
            poll_s=0.01,
            recognize=fake_recognize,
        )
        lm.start()
        assert eng.hold_calls == [("M", "GPU")]
        assert eng.unhold_calls == []

        step = int(0.5 * RATE)
        for pos in range(0, sys_audio.size, step):
            lm.feed("microphone", mic[pos:pos + step])
            lm.feed("system", sys_audio[pos:pos + step])

        assert all_done.wait(10.0), f"done={lm.done}, expected={len(expected_keys)}"
        cache = lm.stop(timeout=5.0)
        stopped_event.set()

        assert eng.unhold_calls == [("M", "GPU")]
        assert lm.stopped
        assert lm.done == len(expected_keys)
        assert lm.submitted == len(expected_keys)
        assert lm.errors == 0
        assert not after_stop_calls
        assert update_threads and all(name == "live-meeting" for name in update_threads)

        # No chunk recognized twice; no silent chunks recognized.
        assert all(is_speech for _, _, _, is_speech in recognized_calls)
        assert len(recognized_calls) == len(set((s, b, e) for s, b, e, _ in recognized_calls))
        assert len(cache) == len(expected_keys)
        for track, b, e in expected_keys:
            raw = mic if track == "microphone" else sys_audio
            q = live_meeting.quantize_pcm16(raw[b:e])
            assert cache.get(track, b, e, q) is not None
    finally:
        eng.stop()


def test_live_meeting_end_to_end_matches_offline_forced(tmp_path: Path):
    class HashFakePipe:
        def __init__(self):
            self.generate_calls = 0

        def load(self):
            pass

        def release(self):
            pass

        def generate(self, audio, language):
            self.generate_calls += 1
            digest = hashlib.blake2b(
                np.ascontiguousarray(audio[:800], dtype=np.float32).tobytes(),
                digest_size=4,
            ).hexdigest()
            chunk = SimpleNamespace(
                start_ts=0.0,
                end_ts=len(audio) / RATE,
                text=f"t{digest}",
            )
            return SimpleNamespace(chunks=[chunk])

    # ~11 minutes so forced mode yields 3 chunks per track (2 settled + 1 tail).
    mic_raw = make_speech(11 * 60.0, seed=21, click_first=False)
    sys_raw = make_speech(11 * 60.0, seed=22, click_first=False)

    mic_file = tmp_path / "microphone.flac"
    sys_file = tmp_path / "system.flac"
    sf.write(str(mic_file), mic_raw, RATE, format="FLAC", subtype="PCM_16")
    sf.write(str(sys_file), sys_raw, RATE, format="FLAC", subtype="PCM_16")
    files = {"microphone": mic_file, "system": sys_file}

    eng = engine.Engine(lambda m, d: HashFakePipe())
    eng.start()
    try:
        done_event = threading.Event()
        lm = live_meeting.LiveMeeting(
            eng,
            "M",
            "CPU",
            "pl",
            on_update=lambda _segs: done_event.set() if lm.done >= 4 else None,
            poll_s=0.01,
            max_behind_s=2000.0,
            recognize=transcribe.transcribe_chunk,
        )
        lm.start()

        # Feed unquantized float32 blocks, just as the audio recorder does.
        block = int(2.0 * RATE)
        for pos in range(0, mic_raw.size, block):
            lm.feed("microphone", mic_raw[pos:pos + block])
            lm.feed("system", sys_raw[pos:pos + block])

        assert done_event.wait(10.0), f"done={lm.done}"
        cache = lm.stop(timeout=5.0)
        assert len(cache) == 4
    finally:
        eng.stop()

    pipe_cached = HashFakePipe()
    segs_cached = transcribe.drain(
        transcribe.iter_transcribe_session(files, pipe_cached, "pl", cache=cache)
    )
    pipe_uncached = HashFakePipe()
    segs_uncached = transcribe.drain(
        transcribe.iter_transcribe_session(files, pipe_uncached, "pl")
    )

    assert segs_cached == segs_uncached
    assert pipe_uncached.generate_calls == 6
    # Only the last chunk of each track (2 total) was missing from cache.
    assert pipe_cached.generate_calls == 2
    assert cache.hits == 4


def test_live_meeting_fallback_on_behind_limit():
    class DummyPipe:
        def load(self):
            pass

        def release(self):
            pass

    eng = engine.Engine(lambda m, d: DummyPipe())
    eng.start()
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[int, int]] = []

    def blocking_recognize(pipe, audio, begin, end, language, speaker):
        calls.append((begin, end))
        entered.set()
        release.wait(5.0)
        return [merge.Segment(begin / RATE, end / RATE, "in-flight", speaker)]

    try:
        # In auto mode, limit = max_behind_s (5.0) + CHUNK_AUTO_S (12.0) = 17.0 s.
        # Feed 15s first so 1 chunk (< 17s) starts and blocks in flight,
        # then feed 40s more so the backlog exceeds 17.0 s.
        lm = live_meeting.LiveMeeting(
            eng,
            "M",
            "CPU",
            "auto",
            poll_s=0.01,
            max_behind_s=5.0,
            recognize=blocking_recognize,
        )
        lm.start()
        first = make_speech(15.0, seed=31, click_first=True)
        lm.feed("system", first)
        assert entered.wait(5.0)

        more = make_speech(40.0, seed=32, click_first=True)
        lm.feed("system", more)
        deadline = time.monotonic() + 5.0
        while not lm.gave_up and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lm.gave_up

        # Subsequent feeds must be ignored.
        submitted_before = lm.submitted
        lm.feed("system", make_speech(20.0, seed=33, click_first=True))
        time.sleep(0.05)
        assert lm.submitted == submitted_before

        # Release the in-flight job so it completes and populates the cache.
        release.set()
        deadline = time.monotonic() + 5.0
        while lm.done < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lm.done == 1

        cache = lm.stop(timeout=5.0)
        assert len(cache) == 1
        assert len(calls) == 1
        b, e = calls[0]
        assert cache.get("system", b, e, live_meeting.quantize_pcm16(first[b:e])) is not None
    finally:
        release.set()
        eng.stop()


def test_live_meeting_gives_up_after_max_errors():
    class DummyPipe:
        def load(self):
            pass

        def release(self):
            pass

    eng = engine.Engine(lambda m, d: DummyPipe())
    eng.start()
    try:
        def failing_recognize(pipe, audio, begin, end, language, speaker):
            raise RuntimeError("synthetic failure")

        lm = live_meeting.LiveMeeting(
            eng,
            "M",
            "CPU",
            "auto",
            poll_s=0.01,
            recognize=failing_recognize,
        )
        lm.start()
        lm.feed("system", make_speech(60.0, seed=41, click_first=True))

        deadline = time.monotonic() + 5.0
        while not lm.gave_up and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lm.gave_up
        assert lm.errors == live_meeting.MAX_ERRORS
        assert lm.done == 0
        lm.stop(timeout=5.0)
    finally:
        eng.stop()


def test_live_meeting_should_wait_and_callback_exceptions():
    class DummyPipe:
        def load(self):
            pass

        def release(self):
            pass

    eng = engine.Engine(lambda m, d: DummyPipe())
    eng.start()
    try:
        wait_flag = True
        raise_next = False
        done_event = threading.Event()

        def should_wait():
            nonlocal raise_next
            if raise_next:
                raise_next = False
                raise ValueError("transient wait check failure")
            return wait_flag

        def on_update(_segs):
            if lm.done >= 1:
                done_event.set()
            raise RuntimeError("transient ui failure")

        lm = live_meeting.LiveMeeting(
            eng,
            "M",
            "CPU",
            "auto",
            on_update=on_update,
            should_wait=should_wait,
            poll_s=0.01,
            recognize=lambda pipe, a, b, e, lang, spk: [
                merge.Segment(b / RATE, e / RATE, "ok", spk)
            ],
        )
        lm.start()
        lm.feed("system", make_speech(20.0, seed=51, click_first=True))
        time.sleep(0.06)
        assert lm.submitted == 0

        raise_next = True
        wait_flag = False
        assert done_event.wait(5.0)
        assert lm.done >= 1
        lm.stop(timeout=5.0)
    finally:
        eng.stop()


def test_live_meeting_stop_without_start_and_idempotent():
    eng = SpyEngine(lambda m, d: None)
    lm1 = live_meeting.LiveMeeting(eng, "M", "CPU", "auto", poll_s=0.01)
    cache1 = lm1.stop()
    assert cache1 is lm1.cache
    assert lm1.stop() is cache1
    assert eng.hold_calls == []
    assert eng.unhold_calls == []

    lm2 = live_meeting.LiveMeeting(eng, "M", "CPU", "auto", poll_s=0.01)
    lm2.start()
    lm2.stop()
    lm2.stop()
    assert eng.hold_calls == [("M", "CPU")]
    assert eng.unhold_calls == [("M", "CPU")]


def test_live_meeting_two_tracks_settling_a_forced_chunk_together_is_not_behind():
    # Both tracks settle their first 5-minute chunk at the same moment: 600 s of
    # audio to recognize, but live work is only one chunk (~315 s) behind the
    # recording, under the limit (max_behind_s + 315 s). Found by the benchmark.
    done = threading.Event()

    def recognize(pipe, chunk, begin, end, language, speaker):
        return [merge.Segment(begin / RATE, end / RATE, "x", speaker)]

    eng = engine.Engine(pipeline_factory=lambda m, d: object())
    eng.start()
    try:
        lm = live_meeting.LiveMeeting(eng, "M", "GPU", "pl", poll_s=0.01, recognize=recognize,
                                      on_update=lambda segments: len(segments) >= 2 and done.set())
        lm.start()
        tracks = {"microphone": make_speech(330, seed=1), "system": make_speech(330, seed=2)}
        for pos in range(0, 330 * RATE, 16_000):
            for name, track in tracks.items():
                lm.feed(name, track[pos:pos + 16_000])
        assert done.wait(10.0)
        assert not lm.gave_up and lm.done == 2
        lm.stop()
    finally:
        eng.stop()
