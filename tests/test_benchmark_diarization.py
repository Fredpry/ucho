# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for pure functions of tools/benchmark_diarization.py (no TTS, no models, no sherpa_onnx)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "benchmark_diarization.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_diarization", _PATH)
bd = importlib.util.module_from_spec(_SPEC)
sys.modules["benchmark_diarization"] = bd
_SPEC.loader.exec_module(bd)


# --- der() -----------------------------------------------------------------------


def test_der_perfect_hypothesis_is_zero():
    ref = [(0.5, 2.0, "a"), (2.5, 4.0, "b")]
    hyp = [(0.5, 2.0, "a"), (2.5, 4.0, "b")]
    scores, mapping = bd.der(ref, hyp, length_s=5.0)
    assert scores == {"der": 0.0, "miss": 0.0, "fa": 0.0, "confusion": 0.0}
    assert mapping == {"a": "a", "b": "b"}


def test_der_swapped_labels_mapped_to_zero_error():
    ref = [(0.0, 2.0, "paulina"), (2.5, 4.5, "adam")]
    hyp = [(0.0, 2.0, 2), (2.5, 4.5, 1)]
    scores, mapping = bd.der(ref, hyp, length_s=5.0)
    assert mapping == {2: "paulina", 1: "adam"}
    assert scores["der"] == pytest.approx(0.0)
    assert scores["confusion"] == pytest.approx(0.0)


def test_der_speaker_split_into_two_hyp_labels_gives_confusion():
    # One reference speaker split evenly into two hypothesis labels: only one can map to "a".
    ref = [(0.0, 2.0, "a")]
    hyp = [(0.0, 1.0, 0), (1.0, 2.0, 1)]
    scores, mapping = bd.der(ref, hyp, length_s=2.5)
    assert len(mapping) == 1
    assert scores["miss"] == pytest.approx(0.0)
    assert scores["fa"] == pytest.approx(0.0)
    assert scores["confusion"] == pytest.approx(0.5)
    assert scores["der"] == pytest.approx(0.5)


def test_der_missing_hypothesis_is_all_miss():
    ref = [(1.0, 3.0, "a")]
    scores, mapping = bd.der(ref, [], length_s=4.0)
    assert mapping == {}
    assert scores["miss"] == pytest.approx(1.0)
    assert scores["fa"] == pytest.approx(0.0)
    assert scores["confusion"] == pytest.approx(0.0)
    assert scores["der"] == pytest.approx(1.0)


def test_der_extra_hypothesis_speech_is_false_alarm():
    ref = [(0.0, 2.0, "a")]
    hyp = [(0.0, 2.0, 0), (3.0, 4.0, 0)]
    scores, mapping = bd.der(ref, hyp, length_s=5.0)
    assert mapping == {0: "a"}
    assert scores["miss"] == pytest.approx(0.0)
    assert scores["confusion"] == pytest.approx(0.0)
    assert scores["fa"] == pytest.approx(0.5)
    assert scores["der"] == pytest.approx(0.5)


def test_der_collar_excludes_boundary_frames():
    # Hypothesis starts 0.1 s late and ends 0.1 s early; a 0.25 s collar excludes both boundaries.
    ref = [(1.0, 4.0, "a")]
    hyp = [(1.1, 3.9, 0)]
    raw, _ = bd.der(ref, hyp, length_s=5.0, collar=0.0)
    collared, _ = bd.der(ref, hyp, length_s=5.0, collar=0.25)
    assert raw["miss"] > 0.0
    assert raw["der"] > 0.0
    assert collared["der"] == pytest.approx(0.0)
    assert collared["miss"] == pytest.approx(0.0)


def test_der_empty_reference_returns_zeros():
    scores, mapping = bd.der([], [(0.5, 1.5, 0)], length_s=2.0)
    assert scores == {"der": 0.0, "miss": 0.0, "fa": 0.0, "confusion": 0.0}
    assert mapping == {}


def test_der_zero_cooccurrence_hyp_label_stays_unmapped():
    ref = [(0.0, 1.0, "a")]
    hyp = [(0.0, 1.0, 0), (2.0, 3.0, 99)]
    _scores, mapping = bd.der(ref, hyp, length_s=4.0)
    assert mapping == {0: "a"}
    assert 99 not in mapping


def test_der_greedy_mapping_for_more_than_eight_speakers():
    ref = [(float(i), float(i) + 0.8, f"s{i}") for i in range(10)]
    hyp = [(float(i), float(i) + 0.8, 100 + i) for i in range(10)]
    scores, mapping = bd.der(ref, hyp, length_s=11.0)
    assert scores["der"] == pytest.approx(0.0)
    assert mapping == {100 + i: f"s{i}" for i in range(10)}


# --- attribution() ---------------------------------------------------------------


def test_attribution_largest_overlap_and_unassigned():
    ref = [
        (0.0, 2.0, "paulina"),
        (3.0, 5.0, "adam"),
        (6.0, 7.0, "paulina"),
        (8.0, 9.0, "adam"),
    ]
    # Utterance 1: mostly hyp 0 (paulina). Utterance 2: mostly hyp 1 (adam).
    # Utterance 3: covered by hyp 1 (wrong speaker). Utterance 4: no overlap (unassigned).
    hyp = [
        (0.0, 1.6, 0),
        (1.6, 2.0, 1),
        (3.2, 5.0, 1),
        (6.0, 7.0, 1),
    ]
    mapping = {0: "paulina", 1: "adam"}
    acc, unassigned = bd.attribution(ref, hyp, mapping)
    assert acc == pytest.approx(2 / 4)
    assert unassigned == pytest.approx(1 / 4)


def test_attribution_empty_reference():
    assert bd.attribution([], [(0.0, 1.0, 0)], {0: "a"}) == (0.0, 0.0)


# --- meeting() -------------------------------------------------------------------


def _tiny_pools() -> dict[str, list[np.ndarray]]:
    t = np.linspace(0.0, 1.0, bd.SAMPLE_RATE, endpoint=False, dtype=np.float32)
    return {
        "paulina": [0.4 * np.sin(2 * np.pi * 220 * t).astype(np.float32)],
        "adam": [0.4 * np.sin(2 * np.pi * 110 * t).astype(np.float32)],
        "zira": [0.4 * np.sin(2 * np.pi * 330 * t).astype(np.float32)],
    }


def test_meeting_is_deterministic_sorted_and_matches_tail_length():
    pools = _tiny_pools()
    speakers = ["paulina", "adam", "zira"]
    audio_a, ref_a = bd.meeting(pools, speakers, minutes=0.2, seed=101)
    audio_b, ref_b = bd.meeting(pools, speakers, minutes=0.2, seed=101)
    audio_c, _ref_c = bd.meeting(pools, speakers, minutes=0.2, seed=102)

    assert ref_a == ref_b
    assert np.array_equal(audio_a, audio_b)
    assert not np.array_equal(audio_a[: min(audio_a.size, audio_c.size)], audio_c[: min(audio_a.size, audio_c.size)])

    starts = [r[0] for r in ref_a]
    assert starts == sorted(starts)
    assert all(end > start and spk in speakers for start, end, spk in ref_a)

    expected_end = int(max(r[1] for r in ref_a) * bd.SAMPLE_RATE) + bd.SAMPLE_RATE
    assert audio_a.size == expected_end
    assert audio_a.dtype == np.float32
    assert float(np.max(np.abs(audio_a))) <= 1.0
