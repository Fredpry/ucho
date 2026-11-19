# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Optional speaker detection (diarization) inside a meeting track via sherpa-onnx.

Ucho records the microphone and the system audio as two separate tracks, which
splits "Me" from "Meeting" without a diarization model and avoids confusing
similar voices (docs/architecture.md, section 1). Speaker detection inside a
track is therefore off by default and runs only as one extra pass per track
after the text has been recognized: it may relabel speaker codes ("meeting:1",
"meeting:2", ...) but never touches segment text, start or end timestamps.

Models and licenses (CPU only via sherpa-onnx):
- Segmentation: pyannote segmentation-3.0 (MIT, CNRS), exported to ONNX by
  the sherpa-onnx project.
- Speaker embeddings: ERes2Net trained on VoxCeleb by the 3D-Speaker project
  (Apache-2.0), exported to ONNX by the sherpa-onnx project. On the synthetic
  test set it told 4-5 voices apart far better than NeMo TitaNet-S (DER 15-20 %
  against 35-44 %), at about 3x the CPU time.

No voice profiles are kept: embeddings live only in memory during one pass over
a track and are never written anywhere.

The pass runs in a separate short-lived process (`python -m ucho.diarize --worker <track>`)
because sherpa-onnx holds the GIL for seconds at a time (measured up to 3.2 s with a
progress callback, the whole run without one), which would freeze the window and delay
a dictation; the worker runs at below-normal priority, gets only the path of the track,
prints only the turns (times and speaker numbers), and exits, so no voice data outlives
the pass.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

import numpy as np

from . import config, parakeet, platforms, transcribe
from .i18n import t
from .merge import Segment, diarized_code

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# Measured on synthetic 6-minute meetings from offline Windows TTS voices
# (docs/benchmarks.md, "Speaker detection"). Window shift 0.5 instead of the
# sherpa-onnx default 0.1 was about 4x faster with the same or lower DER.
CLUSTER_THRESHOLD = 0.5
WINDOW_SHIFT = 0.5
MIN_DURATION_ON = 0.3
MIN_DURATION_OFF = 0.5
STEP_WAIT_S = 0.1
WORKER_POLL_S = 0.2
MIN_AUDIO_S = 2.0
# No console window; below-normal priority keeps the UI and dictation responsive.
_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
)


available = config.sherpa_available

def models_downloaded() -> bool:
    """True when every speaker detection model repo is downloaded and its ONNX file exists."""
    for repo, info in config.SPEAKER_MODELS.items():
        if not transcribe.model_downloaded(repo):
            return False
        try:
            path = transcribe.model_path(repo, download=False) / info["file"]
        except Exception:
            return False
        if not path.is_file():
            return False
    return True


def ready() -> bool:
    """True when both the sherpa-onnx package and the speaker models are present."""
    return available() and models_downloaded()


def download_models(tqdm_class: type | None = None) -> None:
    """Downloads every speaker detection model in `config.SPEAKER_MODELS`.

    One `tqdm_class` for all of them, so the progress adds up across the repos.
    """
    for repo in config.SPEAKER_MODELS:
        transcribe.model_path(repo, download=True, tqdm_class=tqdm_class)


def model_files() -> tuple[Path, Path]:
    """Local (segmentation, embedding) ONNX file paths; raises RuntimeError if any is missing."""
    paths: list[Path] = []
    missing: list[str] = []
    for repo, info in config.SPEAKER_MODELS.items():
        name = info["file"]
        try:
            path = transcribe.model_path(repo, download=False) / name
        except Exception:
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
        else:
            paths.append(path)
    if missing:
        raise RuntimeError(f"Speaker detection model files missing: {', '.join(missing)}")
    return paths[0], paths[1]


@dataclass
class Turn:
    start: float
    end: float
    speaker: int


class Diarizer:
    """Stateless wrapper around sherpa-onnx offline speaker diarization."""

    def __init__(
        self,
        threshold: float = CLUSTER_THRESHOLD,
        threads: int | None = None,
        factory: Callable | None = None,
    ):
        self.threshold = threshold
        self.threads = threads or parakeet.default_threads()
        self.in_process = factory is not None
        self._factory = factory or _sherpa_diarization

    def diarize_file(self, file: Path, cancel: threading.Event | None = None) -> list[Turn]:
        if self.in_process:
            return self.diarize(transcribe.load_audio(Path(file)), cancel)
        return run_worker(Path(file), self.threshold, self.threads, cancel)

    def diarize(self, audio: np.ndarray, cancel: threading.Event | None = None) -> list[Turn]:
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = np.ascontiguousarray(audio.reshape(-1), dtype=np.float32)
        if audio.size < MIN_AUDIO_S * SAMPLE_RATE:
            return []
        if cancel is not None and cancel.is_set():
            return []

        started = time.monotonic()
        sd = self._factory(self.threshold, self.threads)

        def callback(_processed: int, _total: int) -> int:
            return 1 if (cancel is not None and cancel.is_set()) else 0

        result = sd.process(audio, callback=callback)
        del sd
        if cancel is not None and cancel.is_set():
            return []

        raw = result.sort_by_start_time() if hasattr(result, "sort_by_start_time") else result
        turns: list[Turn] = []
        for item in raw or []:
            start = float(item.start)
            end = float(item.end)
            if end > start:
                turns.append(Turn(start, end, int(item.speaker)))
        turns.sort(key=lambda t: (t.start, t.end))
        speakers = len({t.speaker for t in turns})
        log.info(
            "Speaker detection: %.1f s audio, %d turns, %d speakers in %.1f s",
            audio.size / SAMPLE_RATE, len(turns), speakers, time.monotonic() - started,
        )
        return turns


def assign_speakers(segments: list[Segment], turns: list[Turn], track_speaker: str) -> list[Segment]:
    """Relabels segments from diarization turns without changing start, end or text."""
    if not segments:
        return []
    copied = [Segment(s.start, s.end, s.text, s.speaker) for s in segments]
    if not turns:
        return copied

    assigned: list[int | None] = []
    for seg in segments:
        if seg.end <= seg.start:
            chosen: int | None = None
            for turn in turns:
                if turn.end > turn.start and turn.start <= seg.start < turn.end:
                    chosen = turn.speaker
                    break
            assigned.append(chosen)
            continue

        overlaps: dict[int, float] = {}
        first_turn: dict[int, float] = {}
        for turn in turns:
            overlap = min(seg.end, turn.end) - max(seg.start, turn.start)
            if overlap > 0.0:
                spk = turn.speaker
                overlaps[spk] = overlaps.get(spk, 0.0) + overlap
                if spk not in first_turn or turn.start < first_turn[spk]:
                    first_turn[spk] = turn.start
        if not overlaps:
            assigned.append(None)
        else:
            winner = max(
                overlaps,
                key=lambda spk: (round(overlaps[spk], 9), -first_turn[spk], -spk),
            )
            assigned.append(winner)

    distinct = {spk for spk in assigned if spk is not None}
    if len(distinct) < 2:
        return copied

    earliest_seg: dict[int, float] = {}
    for seg, spk in zip(segments, assigned, strict=True):
        if spk is not None and (spk not in earliest_seg or seg.start < earliest_seg[spk]):
            earliest_seg[spk] = seg.start
    ordered = sorted(distinct, key=lambda spk: (earliest_seg[spk], spk))
    codes = {spk: diarized_code(track_speaker, idx) for idx, spk in enumerate(ordered, start=1)}

    return [
        Segment(seg.start, seg.end, seg.text, codes[spk] if spk is not None else seg.speaker)
        for seg, spk in zip(segments, assigned, strict=True)
    ]


def worker_python() -> str:
    """Python interpreter with standard streams (`python.exe` when the app runs under `pythonw.exe`)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return sys.executable


def run_worker(
    file: Path,
    threshold: float,
    threads: int,
    cancel: threading.Event | None = None,
    popen: Callable = subprocess.Popen,
) -> list[Turn]:
    """Runs speaker detection on `file` in a short-lived worker process and returns its turns."""
    started = time.monotonic()
    command = platforms.shell().worker_command(
        "ucho.diarize",
        [
            "--worker",
            "--threshold",
            repr(float(threshold)),
            "--threads",
            str(int(threads)),
            str(file),
        ],
    )
    # No console window; below-normal priority keeps the UI and dictation responsive.
    # The environment is inherited (HF_HOME is set by ucho.config).
    proc = popen(
        command,
        cwd=str(config.PROJECT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATION_FLAGS,
    )
    while True:
        try:
            out_bytes, err_bytes = proc.communicate(timeout=WORKER_POLL_S)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                return []

    stdout = (out_bytes or b"").decode("utf-8", errors="replace")
    stderr = (err_bytes or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        detail = (err_lines[-1] if err_lines else "")[:200]
        raise RuntimeError(f"Speaker detection worker failed (exit {proc.returncode}): {detail}")

    out_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not out_lines:
        raise RuntimeError("Speaker detection worker returned no result")
    try:
        payload = json.loads(out_lines[-1])
        if not isinstance(payload, dict) or not isinstance(payload.get("turns"), list):
            raise ValueError("missing turns list")
        turns: list[Turn] = []
        for item in payload["turns"]:
            start, end, speaker = item
            start_f = float(start)
            end_f = float(end)
            if end_f > start_f:
                turns.append(Turn(start_f, end_f, int(speaker)))
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Speaker detection worker returned no result") from error

    turns.sort(key=lambda t: (t.start, t.end))
    if cancel is not None and cancel.is_set():
        return []
    log.info("Speaker detection worker: %d turns in %.1f s", len(turns), time.monotonic() - started)
    return turns


def iter_diarize_track(
    file: Path,
    segments: list[Segment],
    track_speaker: str,
    diarizer: Diarizer,
    cancel: threading.Event | None = None,
) -> transcribe.Steps:
    """Step generator that runs diarizer on `file` in a helper thread and yields while waiting."""
    if not segments:
        return []
    copied = [Segment(s.start, s.end, s.text, s.speaker) for s in segments]
    if cancel is not None and cancel.is_set():
        return copied

    turns: list[Turn] = []
    failure: BaseException | None = None

    def run() -> None:
        nonlocal turns, failure
        try:
            turns = diarizer.diarize_file(Path(file), cancel=cancel)
        except BaseException as error:
            failure = error

    thread = threading.Thread(target=run, name="diarize", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(STEP_WAIT_S)
        if thread.is_alive():
            yield

    if failure is not None:
        log.warning("Speaker detection failed on a %s track: %s", track_speaker, type(failure).__name__)
        return copied
    if cancel is not None and cancel.is_set():
        return copied
    return assign_speakers(segments, turns, track_speaker)


def track_pass(diarizer: Diarizer):
    """Callable `(name, file, segments, speaker_code, cancel)` for `iter_transcribe_session`."""
    def run(
        _name: str,
        file: Path,
        segments: list[Segment],
        speaker_code: str,
        cancel: threading.Event | None = None,
    ) -> transcribe.Steps:
        return (yield from iter_diarize_track(Path(file), segments, speaker_code, diarizer, cancel))

    return run


def _sherpa_diarization(threshold: float, threads: int):
    try:
        import sherpa_onnx as so
    except ImportError as error:
        raise RuntimeError(
            t("Speaker detection needs the optional sherpa-onnx package. "
              "Run the installer again with -WithSpeakers.")
        ) from error

    seg_file, emb_file = model_files()
    sd_config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(seg_file),
                window_shift_ratio=WINDOW_SHIFT,
            ),
            num_threads=threads,
            provider="cpu",
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(
            model=str(emb_file),
            num_threads=threads,
            provider="cpu",
        ),
        clustering=so.FastClusteringConfig(
            num_clusters=-1,
            threshold=threshold,
        ),
        min_duration_on=MIN_DURATION_ON,
        min_duration_off=MIN_DURATION_OFF,
    )
    if not sd_config.validate():
        raise RuntimeError("Speaker detection model files are not valid")
    return so.OfflineSpeakerDiarization(sd_config)


def worker_main(argv: list[str] | None = None) -> int:
    """CLI entry point for the short-lived speaker detection worker process."""
    parser = argparse.ArgumentParser(prog="python -m ucho.diarize", add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--threshold", type=float, default=CLUSTER_THRESHOLD)
    parser.add_argument("--threads", type=int, default=parakeet.default_threads())
    parser.add_argument("file", nargs="?")
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        sys.stderr.write("Usage: python -m ucho.diarize --worker [--threshold FLOAT] [--threads INT] <file>\n")
        return 2
    if not args.worker or not args.file or unknown:
        sys.stderr.write("Usage: python -m ucho.diarize --worker [--threshold FLOAT] [--threads INT] <file>\n")
        return 2

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        turns = Diarizer(args.threshold, args.threads).diarize(transcribe.load_audio(Path(args.file)))
    except Exception as error:  # the worker reports any error to stderr and exits 1
        sys.stderr.write(f"{type(error).__name__}: {error}\n")
        return 1

    sys.stdout.write(json.dumps({"turns": [[t.start, t.end, t.speaker] for t in turns]}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(worker_main())
