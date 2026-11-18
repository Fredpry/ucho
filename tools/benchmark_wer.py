# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Word and character error rate of Ucho transcripts against reference texts.

Manual quality regression check, run before a release (not in CI). See
docs/benchmarks.md for where to get legally usable Polish test data.

Input folder layout (one utterance or recording per stem):

    clip_001.flac        audio (.wav, .flac or .mp3)
    clip_001.txt         reference transcript (UTF-8)
    clip_001.hyp.txt     hypothesis, written by this tool (or by you)

Usage:

    python tools/benchmark_wer.py DATA_DIR                      # transcribe + score
    python tools/benchmark_wer.py DATA_DIR --wer-only           # score existing *.hyp.txt
    python tools/benchmark_wer.py DATA_DIR --from-tsv test.tsv  # write references from a
                                                                # Common Voice TSV, then exit
    python tools/benchmark_wer.py DATA_DIR --baseline old.json  # show the change vs a past run
    python tools/benchmark_wer.py DATA_DIR --backend parakeet   # optional Parakeet backend
                                                                # (needs sherpa-onnx)

Scoring needs only the standard library. Transcription imports `ucho.transcribe`
(OpenVINO) lazily, so `--wer-only` works in the dev environment without models.
Nothing here touches the network: models are loaded from the local cache only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Sequence

AUDIO_SUFFIXES = (".wav", ".flac", ".mp3")
HYP_SUFFIX = ".hyp.txt"
DEFAULT_JSON = "wer_results.json"

# Apostrophes join the word ("don't" -> "dont") instead of splitting it, so
# "don't" and "dont" score as the same word. Every other punctuation or symbol
# character becomes a space ("time-line" -> "time line" on both sides).
APOSTROPHES = {"'", "’", "ʼ", "`"}


# --- normalization ----------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and symbols, collapse whitespace.

    NFKC first, so full-width forms and ligatures compare equal to plain ones.
    Digits are kept as they are: "5" and "pięć" stay different words (Whisper
    writes digits, references usually spell numbers out; see docs/benchmarks.md).
    """
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for char in text:
        if char in APOSTROPHES:
            continue
        category = unicodedata.category(char)
        if category[0] in "PS" or category[0] == "Z" or char.isspace():
            out.append(" ")
        else:
            out.append(char)
    return " ".join("".join(out).split())


def words(text: str) -> list[str]:
    return normalize(text).split()


# --- edit distance -----------------------------------------------------------


@dataclass
class Errors:
    """Alignment counts. Reference length = hits + substitutions + deletions."""

    hits: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0

    @property
    def total(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def reference_length(self) -> int:
        return self.hits + self.substitutions + self.deletions

    def __add__(self, other: Errors) -> Errors:
        return Errors(
            self.hits + other.hits,
            self.substitutions + other.substitutions,
            self.deletions + other.deletions,
            self.insertions + other.insertions,
        )


def align(reference: Sequence, hypothesis: Sequence) -> Errors:
    """Levenshtein alignment with unit costs, counting S, D and I.

    Two rows of the dynamic-programming table, each cell holding
    (cost, substitutions, deletions, insertions), so memory is O(len(hypothesis)).
    On equal cost the order of preference is substitution, deletion, insertion
    (the total is the same either way; only the S/D/I split depends on it).
    """
    m = len(hypothesis)
    previous = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, len(reference) + 1):
        current = [(i, 0, i, 0)]
        ref_item = reference[i - 1]
        for j in range(1, m + 1):
            diag = previous[j - 1]
            if ref_item == hypothesis[j - 1]:
                best = diag
            else:
                best = (diag[0] + 1, diag[1] + 1, diag[2], diag[3])
            up = previous[j]
            if up[0] + 1 < best[0]:
                best = (up[0] + 1, up[1], up[2] + 1, up[3])
            left = current[j - 1]
            if left[0] + 1 < best[0]:
                best = (left[0] + 1, left[1], left[2], left[3] + 1)
            current.append(best)
        previous = current
    _, subs, dels, ins = previous[m]
    return Errors(len(reference) - subs - dels, subs, dels, ins)


def rate(errors: Errors) -> float | None:
    """Error count over reference length. None when the reference is empty and
    the hypothesis is not (the rate is undefined, not zero and not infinite)."""
    if errors.reference_length == 0:
        return 0.0 if errors.total == 0 else None
    return errors.total / errors.reference_length


def word_errors(reference: str, hypothesis: str) -> Errors:
    return align(words(reference), words(hypothesis))


def char_errors(reference: str, hypothesis: str) -> Errors:
    # On the normalized string, single spaces included (same convention as jiwer).
    return align(list(normalize(reference)), list(normalize(hypothesis)))


def wer(reference: str, hypothesis: str) -> float | None:
    return rate(word_errors(reference, hypothesis))


def cer(reference: str, hypothesis: str) -> float | None:
    return rate(char_errors(reference, hypothesis))


# --- data folder ---------------------------------------------------------------


@dataclass
class Item:
    name: str
    reference: Path
    hypothesis: Path
    audio: Path | None = None


def _stem(path: Path) -> str:
    return path.name[: -len(HYP_SUFFIX)] if path.name.endswith(HYP_SUFFIX) else path.stem


def find_items(folder: Path, wer_only: bool) -> list[Item]:
    """Pairs files by stem. With `wer_only` the audio is not needed, only
    `<stem>.txt` and `<stem>.hyp.txt`; otherwise every audio file with a reference."""
    items = []
    if wer_only:
        for hyp in sorted(folder.glob("*" + HYP_SUFFIX)):
            ref = folder / f"{_stem(hyp)}.txt"
            if ref.exists():
                items.append(Item(_stem(hyp), ref, hyp))
        return items
    for audio in sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES):
        ref = folder / f"{audio.stem}.txt"
        if ref.exists():
            items.append(Item(audio.stem, ref, folder / f"{audio.stem}{HYP_SUFFIX}", audio))
    return items


def references_from_tsv(tsv: Path, folder: Path, limit: int | None = None) -> int:
    """Writes `<stem>.txt` from a Common Voice TSV (columns `path` and `sentence`)
    for clips present in `folder`. Returns the number of references written."""
    present = {p.stem: p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES}
    written = 0
    with tsv.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            stem = Path(row.get("path") or "").stem
            sentence = (row.get("sentence") or "").strip()
            if stem in present and sentence:
                (folder / f"{stem}.txt").write_text(sentence + "\n", encoding="utf-8")
                written += 1
                if limit is not None and written >= limit:
                    break
    return written


# --- transcription (lazy: needs OpenVINO and a downloaded model) ----------------


class Transcriber:
    def __init__(self, model: str, device: str, language: str, models_dir: Path | None = None,
                 backend: str = "whisper", threads: int | None = None):
        # Both variables before anything imports huggingface_hub (ucho.config only
        # sets HF_HOME if it is unset). HF_HUB_OFFLINE: the model must already be
        # on disk, this tool never downloads.
        if models_dir is not None:
            os.environ["HF_HOME"] = str(models_dir)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ucho import config, transcribe

        if language not in config.LANGUAGES:
            raise SystemExit(f"Unknown language {language!r}, choose from: {', '.join(config.LANGUAGES)}")
        self._transcribe = transcribe
        self.language_token = config.LANGUAGES[language]
        if backend == "parakeet":
            from ucho import parakeet

            self.engine = parakeet.Engine(model or parakeet.REPO_ID, device, threads)
        else:
            self.engine = transcribe.Engine(model or config.DEFAULT_MODEL, device)
        # Load (and on GPU compile) before the first file, so that file's
        # processing time and speed are not inflated by the one-off load.
        start = time.perf_counter()
        self.engine.load()
        self.load_s = round(time.perf_counter() - start, 2)

    def __call__(self, audio: Path) -> str:
        from ucho.merge import merge_tracks

        # Same path as the app: silence cutting, chunking, hallucination filter.
        segments = self._transcribe.transcribe_file(audio, self.engine, "", self.language_token)
        return " ".join(s.text for s in merge_tracks(segments)).strip()

    def close(self) -> None:
        self.engine.release()


def peak_memory_gb() -> float | None:
    """Peak working set of this process in GB (Windows only, None elsewhere).

    Includes Python, numpy and the runtime, not just the model. On Intel iGPUs
    a GPU model lives in shared memory that this number does not show.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t)] + [
                (name, ctypes.c_size_t) for name in (
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                                     wintypes.DWORD]
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                                ctypes.byref(counters), counters.cb):
            return None
        return round(counters.PeakWorkingSetSize / 1024 ** 3, 2)
    except Exception:
        return None


def audio_seconds(path: Path) -> float | None:
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:
        return None


# --- report -----------------------------------------------------------------------


@dataclass
class Result:
    name: str
    reference_words: int
    word: dict
    char: dict
    wer: float | None
    cer: float | None
    audio_s: float | None = None
    processing_s: float | None = None
    # Audio seconds per processing second, "15x faster than real time" as in the
    # README and `python -m ucho.hardware --benchmark` (higher is faster).
    speed: float | None = None


def score(name: str, reference: str, hypothesis: str) -> Result:
    w = word_errors(reference, hypothesis)
    c = char_errors(reference, hypothesis)
    return Result(name, w.reference_length, asdict(w), asdict(c), rate(w), rate(c))


def total(results: list[Result]) -> dict:
    """Corpus-level rates: all errors over all reference units (micro average),
    so long files weigh more than short ones, as in jiwer and most papers."""
    w = sum((Errors(**r.word) for r in results), Errors())
    c = sum((Errors(**r.char) for r in results), Errors())
    audio = sum(r.audio_s or 0.0 for r in results)
    processing = sum(r.processing_s or 0.0 for r in results)
    return {
        "files": len(results),
        "reference_words": w.reference_length,
        "word": asdict(w),
        "char": asdict(c),
        "wer": rate(w),
        "cer": rate(c),
        "audio_s": round(audio, 2) if audio else None,
        "processing_s": round(processing, 2) if processing else None,
        "speed": round(audio / processing, 2) if audio and processing else None,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def format_table(results: list[Result], summary: dict) -> str:
    width = max([len("TOTAL"), *(len(r.name) for r in results)])
    header = (f"{'file':<{width}}  {'words':>6}  {'S':>4}  {'D':>4}  {'I':>4}  "
              f"{'WER':>7}  {'CER':>7}  {'audio s':>8}  {'proc s':>7}  {'speed':>6}")
    lines = [header, "-" * len(header)]

    def row(name, n, w, wer_value, cer_value, audio, proc, speed):
        lines.append(
            f"{name:<{width}}  {n:>6}  {w['substitutions']:>4}  {w['deletions']:>4}  "
            f"{w['insertions']:>4}  {_pct(wer_value):>7}  {_pct(cer_value):>7}  "
            f"{_num(audio):>8}  {_num(proc):>7}  {_num(speed):>6}"
        )

    for r in results:
        row(r.name, r.reference_words, r.word, r.wer, r.cer, r.audio_s, r.processing_s, r.speed)
    lines.append("-" * len(header))
    row("TOTAL", summary["reference_words"], summary["word"], summary["wer"], summary["cer"],
        summary["audio_s"], summary["processing_s"], summary["speed"])
    return "\n".join(lines)


def compare(summary: dict, baseline: dict) -> str:
    """One line per metric: baseline -> now (difference in percentage points)."""
    lines = []
    for key in ("wer", "cer"):
        old, new = baseline.get("total", {}).get(key), summary.get(key)
        if old is None or new is None:
            lines.append(f"{key.upper()}: {_pct(old)} -> {_pct(new)}")
        else:
            lines.append(f"{key.upper()}: {_pct(old)} -> {_pct(new)} ({100 * (new - old):+.1f} pp)")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WER/CER of Ucho transcripts against reference texts (manual, offline).")
    parser.add_argument("folder", type=Path, help="folder with audio, <stem>.txt and <stem>.hyp.txt")
    parser.add_argument("--wer-only", action="store_true",
                        help="do not transcribe, score existing <stem>.hyp.txt files")
    parser.add_argument("--backend", choices=("whisper", "parakeet"), default="whisper",
                        help="whisper (OpenVINO, the app default) or parakeet (optional, sherpa-onnx, "
                             "CPU only)")
    parser.add_argument("--model", default="",
                        help="model repo id (default: the backend's default model)")
    parser.add_argument("--threads", type=int, default=None,
                        help="CPU threads for --backend parakeet (default: 2, or 1 with fewer than "
                             "4 logical CPUs; see ucho/parakeet.py)")
    parser.add_argument("--models-dir", type=Path, default=None,
                        help="Hugging Face cache holding the models, e.g. the installed app's models "
                             "folder (default: HF_HOME, else <repo>/models)")
    parser.add_argument("--device", default="GPU", help="GPU, CPU, NPU or AUTO (default: GPU, falls back to CPU)")
    parser.add_argument("--language", default="auto", help="auto, pl or en (default: auto, as in the app)")
    parser.add_argument("--limit", type=int, default=None, help="process at most N files")
    parser.add_argument("--json", type=Path, default=None,
                        help=f"where to write results (default: FOLDER/{DEFAULT_JSON})")
    parser.add_argument("--baseline", type=Path, default=None, help="earlier results JSON to compare with")
    parser.add_argument("--from-tsv", type=Path, default=None,
                        help="write <stem>.txt references from a Common Voice TSV and exit")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    folder: Path = args.folder
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2

    if args.from_tsv:
        count = references_from_tsv(args.from_tsv, folder, args.limit)
        print(f"Wrote {count} reference files to {folder}")
        return 0 if count else 2

    items = find_items(folder, args.wer_only)[: args.limit]
    if not items:
        wanted = "<stem>.txt + <stem>.hyp.txt" if args.wer_only else "audio + <stem>.txt"
        print(f"No pairs found in {folder} (expected {wanted})", file=sys.stderr)
        return 2

    transcriber = None if args.wer_only else Transcriber(
        args.model, args.device, args.language, args.models_dir, args.backend, args.threads)
    results: list[Result] = []
    try:
        for n, item in enumerate(items, 1):
            reference = item.reference.read_text(encoding="utf-8")
            audio_s = processing_s = None
            if transcriber is not None:
                print(f"[{n}/{len(items)}] {item.name}", file=sys.stderr)
                start = time.perf_counter()
                hypothesis = transcriber(item.audio)
                processing_s = time.perf_counter() - start
                audio_s = audio_seconds(item.audio)
                item.hypothesis.write_text(hypothesis + "\n", encoding="utf-8")
            else:
                hypothesis = item.hypothesis.read_text(encoding="utf-8")
            result = score(item.name, reference, hypothesis)
            result.audio_s = round(audio_s, 2) if audio_s else None
            result.processing_s = round(processing_s, 2) if processing_s else None
            if audio_s and processing_s:
                result.speed = round(audio_s / processing_s, 2)
            results.append(result)
    finally:
        if transcriber is not None:
            transcriber.close()

    summary = total(results)
    print(format_table(results, summary))

    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "wer-only" if args.wer_only else "transcribe",
        "backend": None if args.wer_only else args.backend,
        "model": None if args.wer_only else (transcriber.engine.repo_id if transcriber else None),
        "threads": getattr(transcriber.engine, "threads", None) if transcriber else None,
        "device_requested": None if args.wer_only else args.device,
        "device_used": None if args.wer_only else (transcriber.engine.device if transcriber else None),
        "model_load_s": transcriber.load_s if transcriber else None,
        "language": None if args.wer_only else args.language,
        "peak_working_set_gb": None if args.wer_only else peak_memory_gb(),
        "normalization": "NFKC, lowercase, punctuation and symbols removed, whitespace collapsed",
        "total": summary,
        "files": [asdict(r) for r in results],
    }
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        print()
        print(compare(summary, baseline))
    out = args.json or folder / DEFAULT_JSON
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nResults: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
