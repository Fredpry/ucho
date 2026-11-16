# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Speaker detection (diarization) benchmark on synthetic multi-voice meetings.

Usage:

    python tools/benchmark_diarization.py --make-voices C:\\bench\\voices     # TTS sentences, once
    python tools/benchmark_diarization.py C:\\bench\\voices [--minutes 6] [--threshold 0.5] [--threads 2] [--telephony] [--scenarios ...] [--json out.json]

Needs the optional sherpa-onnx package and the speaker models already downloaded
(Settings -> Transcription), else it exits with code 2 and a one-line message.
No network is used.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import benchmark_engine  # noqa: E402

SAMPLE_RATE = 16000
FRAME_S = 0.01

POLISH_SENTENCES: list[str] = list(benchmark_engine.SENTENCES) + list(benchmark_engine.MEETING)

ENGLISH_SENTENCES: list[str] = [
    "Good morning, I sent the updated offer to everyone yesterday.",
    "Could we move the review to Thursday at ten?",
    "The supplier confirmed the delivery for next week.",
    "I will prepare the test report by Friday.",
    "Let us split the rollout into two stages.",
    "The training for the new line is planned for March.",
    "Please send me the notes from the last call.",
    "The budget for the first quarter stays as it is.",
    "We still need a second quote to compare the terms.",
    "I agree, the pilot can start one week later.",
    "The installer works on a clean Windows account.",
    "Thanks, I will come back with a decision on Monday.",
]

SCENARIOS: dict[str, list[str]] = {
    "2 voices M/F": ["paulina", "adam"],
    "3 voices": ["paulina", "adam", "zira"],
    "4 voices": ["paulina", "adam", "zira", "adam_hi"],
    "5 voices": ["paulina", "adam", "zira", "adam_hi", "paulina_lo"],
    "2 similar (F, F-8%)": ["paulina", "paulina_lo"],
}

_POWERSHELL_TTS = r"""
$Out = $env:TTS_OUT
$pl = Get-Content -Encoding UTF8 $env:TTS_PL
$en = Get-Content -Encoding UTF8 $env:TTS_EN
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($pair in @(@('paulina', 'Microsoft Paulina Desktop', $pl), @('zira', 'Microsoft Zira Desktop', $en))) {
    $name, $voice, $lines = $pair; $i = 0
    foreach ($line in $lines) {
        $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SelectVoice($voice)
        $s.SetOutputToWaveFile((Join-Path $Out ("{0}_{1:d2}.wav" -f $name, $i)), $fmt); $s.Speak($line); $s.Dispose(); $i++ } }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $null = $task.Wait(-1); $task.Result }
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$synth.Voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.DisplayName -like '*Adam*' } | Select-Object -First 1
$i = 0
foreach ($line in $pl) {
    $stream = Await ($synth.SynthesizeTextToStreamAsync($line)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
    $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
    $n = Await ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
    $bytes = New-Object byte[] $n; $reader.ReadBytes($bytes)
    [IO.File]::WriteAllBytes((Join-Path $Out ("adam_{0:d2}.wav" -f $i)), $bytes); $i++ }
"""


def make_voices(folder: Path) -> None:
    """Synthesizes the Polish and English sentence pools with three offline Windows TTS voices."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as pl_file,
        tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as en_file,
    ):
        pl_file.write("\n".join(POLISH_SENTENCES))
        en_file.write("\n".join(ENGLISH_SENTENCES))
        pl_path, en_path = pl_file.name, en_file.name
    env = dict(os.environ, TTS_OUT=str(folder), TTS_PL=pl_path, TTS_EN=en_path)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _POWERSHELL_TTS],
            env=env,
            check=True,
            capture_output=True,
        )
    finally:
        for path in (pl_path, en_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def load_clip(path: Path) -> np.ndarray:
    """Loads a WAV clip as 16 kHz float32 mono, trims leading/trailing silence and normalizes peak to 0.5."""
    audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.asarray(audio, dtype=np.float32)
    if rate != SAMPLE_RATE:
        import soxr

        audio = np.asarray(soxr.resample(audio, rate, SAMPLE_RATE), dtype=np.float32)
    # Exactly the trimming and scaling of the measurement in docs/benchmarks.md:
    # the diarization result reacts even to one sample more or less.
    loud = np.flatnonzero(np.abs(audio) > 0.01)
    if loud.size:
        audio = audio[max(0, int(loud[0]) - 160):int(loud[-1]) + 160]
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.0:
        audio = audio / peak * 0.5
    return np.ascontiguousarray(audio, dtype=np.float32)


def shifted(audio: np.ndarray, factor: float) -> np.ndarray:
    """Shifts pitch and formants (and speed) by `factor` via resampling."""
    import soxr

    out = soxr.resample(np.asarray(audio, dtype=np.float32), SAMPLE_RATE * factor, SAMPLE_RATE)
    return np.ascontiguousarray(out, dtype=np.float32)


def voices(folder: Path) -> dict[str, list[np.ndarray]]:
    """Loads the three base TTS voice pools from `folder` and builds the two shifted copies."""
    folder = Path(folder)
    pools: dict[str, list[np.ndarray]] = {}
    for name in ("paulina", "adam", "zira"):
        files = sorted(folder.glob(f"{name}_*.wav"))
        if not files:
            raise FileNotFoundError(f"No {name}_*.wav files in {folder}")
        pools[name] = [load_clip(p) for p in files]
    pools["paulina_lo"] = [shifted(clip, 0.92) for clip in pools["paulina"]]
    pools["adam_hi"] = [shifted(clip, 1.10) for clip in pools["adam"]]
    return pools


def meeting(
    pools: dict[str, list[np.ndarray]],
    speakers: list[str],
    minutes: float,
    seed: int,
    overlap_p: float = 0.1,
) -> tuple[np.ndarray, list[tuple[float, float, str]]]:
    """Builds a synthetic multi-speaker meeting track and its reference `(start, end, speaker)` list."""
    rng = np.random.default_rng(seed)
    total = int(minutes * 60 * SAMPLE_RATE)
    audio = np.zeros(total + 30 * SAMPLE_RATE, dtype=np.float32)
    ref: list[tuple[float, float, str]] = []
    t = int(0.5 * SAMPLE_RATE)
    last: str | None = None

    while t < total:
        choices = [s for s in speakers if s != last]
        spk = choices[int(rng.integers(len(choices)))]
        last = spk
        for _ in range(int(rng.integers(1, 4))):
            clip = pools[spk][int(rng.integers(len(pools[spk])))]
            gain = float(rng.uniform(0.6, 1.0))
            if t + clip.size > audio.size:
                audio = np.pad(audio, (0, t + clip.size - audio.size + SAMPLE_RATE))
            audio[t : t + clip.size] += clip * gain
            ref.append((t / SAMPLE_RATE, (t + clip.size) / SAMPLE_RATE, spk))
            t += clip.size + int(rng.uniform(0.15, 0.45) * SAMPLE_RATE)
        gap = float(rng.uniform(0.2, 1.2))
        if rng.random() < overlap_p:
            gap = -0.6
        t += int(gap * SAMPLE_RATE)
        t = max(t, int(ref[-1][1] * SAMPLE_RATE) - int(0.8 * SAMPLE_RATE))

    end = int(max(r[1] for r in ref) * SAMPLE_RATE) + SAMPLE_RATE
    if end > audio.size:
        audio = np.pad(audio, (0, end - audio.size))
    noise = rng.normal(0.0, 0.002, end).astype(np.float32)
    audio = np.clip(audio[:end] + noise, -1.0, 1.0).astype(np.float32)
    ref.sort(key=lambda r: (r[0], r[1]))
    return np.ascontiguousarray(audio, dtype=np.float32), ref


def telephony(audio: np.ndarray, seed: int = 7) -> np.ndarray:
    """Narrow-band (8 kHz) and noisy call simulation of the same length as `audio`."""
    import soxr

    rng = np.random.default_rng(seed)
    narrow = soxr.resample(np.asarray(audio, dtype=np.float32), SAMPLE_RATE, 8000)
    wide = np.asarray(soxr.resample(narrow, 8000, SAMPLE_RATE), dtype=np.float32)
    if wide.size < audio.size:
        wide = np.pad(wide, (0, audio.size - wide.size))
    else:
        wide = wide[: audio.size]
    noisy = wide + rng.normal(0.0, 0.01, audio.size).astype(np.float32)
    return np.ascontiguousarray(np.clip(noisy, -1.0, 1.0), dtype=np.float32)


def _optimal_mapping(
    ref_labels: list,
    hyp_labels: list,
    co: dict[tuple, int],
) -> dict:
    """One-to-one mapping `hyp_label -> ref_label` maximizing total co-occurring frames."""
    if not ref_labels or not hyp_labels:
        return {}
    if len(ref_labels) <= 8 and len(hyp_labels) <= 8:
        best_score = -1
        best_pairs: list[tuple] = []
        if len(hyp_labels) <= len(ref_labels):
            for perm in itertools.permutations(ref_labels, len(hyp_labels)):
                pairs = list(zip(hyp_labels, perm, strict=True))
                score = sum(co.get(pair, 0) for pair in pairs)
                if score > best_score:
                    best_score = score
                    best_pairs = pairs
        else:
            for perm in itertools.permutations(hyp_labels, len(ref_labels)):
                pairs = list(zip(perm, ref_labels, strict=True))
                score = sum(co.get(pair, 0) for pair in pairs)
                if score > best_score:
                    best_score = score
                    best_pairs = pairs
        return {h: r for h, r in best_pairs if co.get((h, r), 0) > 0}

    ranked = sorted(
        ((count, h, r) for (h, r), count in co.items() if count > 0),
        key=lambda item: item[0],
        reverse=True,
    )
    mapping: dict = {}
    used_ref: set = set()
    for _count, h, r in ranked:
        if h not in mapping and r not in used_ref:
            mapping[h] = r
            used_ref.add(r)
    return mapping


def der(
    ref: list[tuple[float, float, object]],
    hyp: list[tuple[float, float, object]],
    length_s: float,
    collar: float = 0.0,
) -> tuple[dict[str, float], dict]:
    """Frame-based diarization error rate (10 ms frames) and optimal hyp->ref speaker mapping."""
    n = int(length_s / FRAME_S) + 1
    ref_frames: list[set] = [set() for _ in range(n)]
    hyp_frames: list[set] = [set() for _ in range(n)]

    ref_labels_set: set = set()
    hyp_labels_set: set = set()

    for start, end, label in ref:
        ref_labels_set.add(label)
        a = max(0, int(start / FRAME_S))
        b = min(n, int(end / FRAME_S))
        for i in range(a, b):
            ref_frames[i].add(label)

    for start, end, label in hyp:
        hyp_labels_set.add(label)
        a = max(0, int(start / FRAME_S))
        b = min(n, int(end / FRAME_S))
        for i in range(a, b):
            hyp_frames[i].add(label)

    excluded = [False] * n
    if collar > 0.0:
        for start, end, _label in ref:
            for bound in (start, end):
                a = max(0, int(round((bound - collar) / FRAME_S)))
                b = min(n, int(round((bound + collar) / FRAME_S)))
                for i in range(a, b):
                    excluded[i] = True

    co: dict[tuple, int] = {}
    for i in range(n):
        if excluded[i]:
            continue
        r_set = ref_frames[i]
        h_set = hyp_frames[i]
        if r_set and h_set:
            for h in h_set:
                for r in r_set:
                    co[(h, r)] = co.get((h, r), 0) + 1

    ref_labels = sorted(ref_labels_set, key=str)
    hyp_labels = sorted(hyp_labels_set, key=str)
    mapping = _optimal_mapping(ref_labels, hyp_labels, co)

    miss = 0
    fa = 0
    confusion = 0
    total = 0
    for i in range(n):
        if excluded[i]:
            continue
        r_set = ref_frames[i]
        h_set = hyp_frames[i]
        nr = len(r_set)
        nh = len(h_set)
        if nr == 0 and nh == 0:
            continue
        mapped_hyp = {mapping[h] for h in h_set if h in mapping}
        correct = len(r_set & mapped_hyp)
        miss += max(0, nr - nh)
        fa += max(0, nh - nr)
        confusion += min(nr, nh) - correct
        total += nr

    if total == 0:
        return {"der": 0.0, "miss": 0.0, "fa": 0.0, "confusion": 0.0}, mapping
    return {
        "der": (miss + fa + confusion) / total,
        "miss": miss / total,
        "fa": fa / total,
        "confusion": confusion / total,
    }, mapping


def attribution(
    ref: list[tuple[float, float, object]],
    hyp: list[tuple[float, float, object]],
    mapping: dict,
) -> tuple[float, float]:
    """Per-utterance speaker accuracy and unassigned fraction under largest-overlap attribution."""
    if not ref:
        return 0.0, 0.0
    correct = 0
    unassigned = 0
    for r_start, r_end, r_spk in ref:
        overlaps: dict[object, float] = {}
        for h_start, h_end, h_spk in hyp:
            overlap = min(r_end, h_end) - max(r_start, h_start)
            if overlap > 0.0:
                overlaps[h_spk] = overlaps.get(h_spk, 0.0) + overlap
        if not overlaps:
            unassigned += 1
            continue
        winner = max(overlaps, key=lambda h: overlaps[h])
        if mapping.get(winner) == r_spk:
            correct += 1
    total = len(ref)
    return correct / total, unassigned / total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Speaker detection benchmark on synthetic multi-voice meetings (offline)."
    )
    parser.add_argument("voices", nargs="?", type=Path, help="folder with synthesized voice WAV clips")
    parser.add_argument("--make-voices", type=Path, default=None, help="synthesize TTS voice clips into DIR and exit")
    parser.add_argument("--minutes", type=float, default=6.0, help="meeting length in minutes (default: 6)")
    parser.add_argument("--threshold", type=float, default=None, help="clustering threshold (default: ucho.diarize)")
    parser.add_argument("--threads", type=int, default=None, help="CPU threads (default: ucho.parakeet)")
    parser.add_argument("--telephony", action="store_true", help="also test 8 kHz + noise variants")
    parser.add_argument("--scenarios", nargs="*", default=None, help="subset of scenario names to run")
    parser.add_argument("--json", type=Path, default=None, help="write result rows to JSON")
    args = parser.parse_args(argv)

    if args.make_voices is not None:
        make_voices(args.make_voices)
        return 0

    if args.voices is None or not args.voices.is_dir():
        print("Voice clips folder missing; run with --make-voices DIR first.", file=sys.stderr)
        return 2

    from ucho import diarize, parakeet

    if not diarize.available():
        print("sherpa-onnx is not installed (run install.ps1 -WithSpeakers).", file=sys.stderr)
        return 2
    if not diarize.models_downloaded():
        print("Speaker detection models are not downloaded (Settings -> Transcription).", file=sys.stderr)
        return 2

    threshold = diarize.CLUSTER_THRESHOLD if args.threshold is None else args.threshold
    threads = parakeet.default_threads() if args.threads is None else args.threads
    selected = list(SCENARIOS) if not args.scenarios else args.scenarios
    for name in selected:
        if name not in SCENARIOS:
            print(f"Unknown scenario: {name}", file=sys.stderr)
            return 2

    try:
        pools = voices(args.voices)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    diarizer = diarize.Diarizer(threshold=threshold, threads=threads)
    scenario_order = list(SCENARIOS)

    print(
        "| Scenario | Minutes | Voices | Found | DER | DER (0.25 s collar) | "
        "Miss | False alarm | Confusion | Utterances right | CPU time | Real-time factor |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")

    rows: list[dict] = []
    for base_name in selected:
        idx = scenario_order.index(base_name)
        speakers = SCENARIOS[base_name]
        base_audio, ref = meeting(pools, speakers, args.minutes, seed=100 + idx)
        variants = [(base_name, base_audio)]
        if args.telephony:
            variants.append((f"{base_name} +8kHz+noise", telephony(base_audio)))

        for run_name, audio in variants:
            duration_s = audio.size / SAMPLE_RATE
            t0 = time.perf_counter()
            turns = diarizer.diarize(audio)
            cpu_s = time.perf_counter() - t0
            hyp = [(t.start, t.end, t.speaker) for t in turns]
            found = len({t.speaker for t in turns})
            scores_raw, mapping = der(ref, hyp, duration_s, collar=0.0)
            scores_collar, _ = der(ref, hyp, duration_s, collar=0.25)
            acc, unassigned = attribution(ref, hyp, mapping)
            rtf = cpu_s / duration_s if duration_s > 0 else 0.0

            print(
                f"| {run_name} | {duration_s / 60:.1f} | {len(speakers)} | {found} | "
                f"{100 * scores_raw['der']:.1f} % | {100 * scores_collar['der']:.1f} % | "
                f"{100 * scores_raw['miss']:.1f} % | {100 * scores_raw['fa']:.1f} % | "
                f"{100 * scores_raw['confusion']:.1f} % | {100 * acc:.1f} % | "
                f"{cpu_s:.1f} s | {rtf:.3f} |"
            )
            rows.append({
                "scenario": run_name,
                "minutes": round(duration_s / 60, 2),
                "voices": len(speakers),
                "found": found,
                "der": round(scores_raw["der"], 4),
                "der_collar_025": round(scores_collar["der"], 4),
                "miss": round(scores_raw["miss"], 4),
                "fa": round(scores_raw["fa"], 4),
                "confusion": round(scores_raw["confusion"], 4),
                "utterances_right": round(acc, 4),
                "utterances_unassigned": round(unassigned, 4),
                "cpu_s": round(cpu_s, 2),
                "rtf": round(rtf, 4),
            })

    if args.json is not None:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
