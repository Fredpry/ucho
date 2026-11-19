# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""whisper.cpp check on a macOS runner (job `macos-asr` in .github/workflows/macos.yml).

Speaks one English and one Polish sentence with the macOS `say` voices into
16 kHz WAV files (synthetic speech, no recordings), downloads the hidden CI
model `ggml-base-q5_1.bin` at the pinned revision through
`transcribe.model_path(key, download=True)`, transcribes both files with
`ucho.whispercpp.Engine` in Auto language mode and checks:

- the engine runs on Metal (`device == "METAL"`) and `whispercpp.log` has the
  backend line whisper.cpp writes for Metal;
- the detected language is right and enough keywords are recognized.

The result goes to stdout and, as Markdown, to `$GITHUB_STEP_SUMMARY` when set.
Exit code 1 when any check fails. Run from the repository root:

    UCHO_DATA_DIR=/tmp/ucho python tools/macos_asr_check.py --out /tmp/asr
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same sentences as the M3a spike (docs/macos-spike.md, point 6): base gave
# 5/5 English and 4/5 Polish keywords ("Spodka nie" for "Spotkanie").
SAMPLES = [
    {
        "language": "en",
        "voice": "Samantha",
        "text": "The quarterly budget meeting starts at nine o'clock tomorrow morning "
                "in the small conference room.",
        "keywords": ["budget", "meeting", "tomorrow", "morning", "conference"],
        "min_hits": 4,
    },
    {
        "language": "pl",
        "voice": "Zosia",
        "text": "Spotkanie w sprawie budżetu zaczyna się jutro o dziewiątej rano "
                "w małej sali konferencyjnej.",
        "keywords": ["spotkanie", "budżetu", "jutro", "rano", "sali"],
        "min_hits": 3,
    },
]


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", text).lower()))


def speak(sample: dict, folder: Path) -> Path:
    path = folder / f"say-{sample['language']}.wav"
    subprocess.run(["say", "-v", sample["voice"], "-o", str(path), "--file-format=WAVE",
                    "--data-format=LEI16@16000", sample["text"]], check=True, timeout=120)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="folder for the WAV files")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    import soundfile as sf

    from ucho import config, transcribe, whispercpp

    key = whispercpp.MODEL_BASE
    checks: list[tuple[str, bool, str]] = []

    start = time.monotonic()
    folder = transcribe.model_path(key, download=True)
    _, filename = config.split_model_id(key)
    size_mb = (folder / filename).stat().st_size / 1e6
    checks.append(("model download", True,
                   f"`{key}` at `{config.REVISIONS[whispercpp.REPO][:12]}`, {size_mb:.1f} MB, "
                   f"{time.monotonic() - start:.1f} s"))

    engine = whispercpp.Engine(key)
    start = time.monotonic()
    engine.load()
    load_s = time.monotonic() - start
    log_path = config.LOG_FILE.with_name("whispercpp.log")
    init_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    metal_lines = [line.strip() for line in init_log.splitlines()
                   if whispercpp._METAL_BACKEND.search(line)]
    checks.append(("device", engine.device == "METAL", f"`{engine.device}`, load {load_s:.1f} s"))
    checks.append(("Metal line in whispercpp.log", bool(metal_lines),
                   "; ".join(f"`{line}`" for line in metal_lines[:2]) or "not found"))

    for sample in SAMPLES:
        wav = speak(sample, args.out)
        audio, rate = sf.read(str(wav), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        assert rate == whispercpp.SAMPLE_RATE, rate
        start = time.monotonic()
        result = engine.generate(audio, language=None)
        took = time.monotonic() - start
        text = " ".join(result.texts)
        # Substring match: base glues words now and then ("ranow małej").
        found = _normalized(text)
        hits = [word for word in sample["keywords"] if word in found]
        expected = f"<|{sample['language']}|>"
        lang_ok = result.languages == [expected]
        enough = len(hits) >= sample["min_hits"]
        checks.append((
            f"{sample['language'].upper()} ({sample['voice']}, {len(audio) / rate:.1f} s)",
            lang_ok and enough,
            f"language `{','.join(result.languages)}` (expected `{expected}`), "
            f"keywords {len(hits)}/{len(sample['keywords'])} (min {sample['min_hits']}), "
            f"{took:.1f} s: \"{text}\"",
        ))
    engine.release()

    ok = all(passed for _, passed, _ in checks)
    lines = [f"### whisper.cpp on {os.environ.get('UCHO_RUNNER', sys.platform)}: "
             f"{'OK' if ok else 'FAILED'}", "", "| Check | Result | Details |", "|---|---|---|"]
    for name, passed, details in checks:
        details = details.replace("|", r"\|")
        lines.append(f"| {name} | {'ok' if passed else 'FAILED'} | {details} |")
    report = "\n".join(lines) + "\n"
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
