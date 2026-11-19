# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY M3a spike: the smallest frozen app (PyInstaller) with Tk and pywhispercpp.

    WhisperProbe.app/Contents/MacOS/WhisperProbe <model.bin> <speech.wav> <result.json>

Reads the 16 kHz WAV with the standard library (no libsndfile), transcribes it and
writes a JSON result. Delete together with tools/macos_spike/.
"""

from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path


def main() -> int:
    model_path, wav_path, out_path = sys.argv[1:4]
    result: dict = {"frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable}
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.update()
        result["tk"] = root.tk.call("info", "patchlevel")
        root.destroy()
    except Exception as error:
        result["tk_error"] = f"{type(error).__name__}: {error}"
    try:
        import numpy as np
        from pywhispercpp.model import Model

        with wave.open(wav_path, "rb") as reader:
            frames = reader.readframes(reader.getnframes())
            result["rate"] = reader.getframerate()
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        start = time.monotonic()
        model = Model(model_path, print_progress=False, print_realtime=False, redirect_whispercpp_logs_to=None)
        result["load_seconds"] = round(time.monotonic() - start, 2)
        start = time.monotonic()
        segments = model.transcribe(audio, language="en")
        result["transcribe_seconds"] = round(time.monotonic() - start, 2)
        result["text"] = " ".join(s.text.strip() for s in segments)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if "text" in result else 1


if __name__ == "__main__":
    sys.exit(main())
