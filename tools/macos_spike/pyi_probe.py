# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY M3a spike: the smallest frozen app (PyInstaller) with Tk and pywhispercpp.

    WhisperProbe.app/Contents/MacOS/WhisperProbe <model.bin> <speech.wav> <result.json>

Reads the 16 kHz WAV with the standard library (no libsndfile), transcribes it and
writes a JSON result. Also checks what else Ucho needs from a frozen bundle: OpenVINO
plugins (plugins.xml), catap's native library, sounddevice's PortAudio, soundfile's
libsndfile and pystray's darwin backend. Delete together with tools/macos_spike/.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import wave
from pathlib import Path


def main() -> None:
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
    extras(result)
    Path(out_path).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    sys.stdout.flush()
    os._exit(0 if "text" in result else 1)


def bounded(result: dict, key: str, func, seconds: float = 20) -> None:
    box: dict = {}

    def target():
        try:
            box["value"] = func()
        except BaseException as error:
            box["error"] = f"{type(error).__name__}: {error}"

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    result[key] = f"still blocked after {seconds} s" if thread.is_alive() else box.get("value", box.get("error"))


def extras(result: dict) -> None:
    def openvino_devices():
        import openvino as ov
        return ov.Core().available_devices

    def catap_devices():
        import catap
        native = Path(catap.__file__).parent / "native" / "libcatap_coreaudio.dylib"
        return {"native_lib": native.exists(), "devices": [str(getattr(x, "name", x)) for x in catap.list_audio_devices()]}

    def portaudio():
        import sounddevice as sd
        return sd.get_portaudio_version()[1]

    def libsndfile():
        import soundfile as sf
        return sf.__libsndfile_version__

    def pystray_backend():
        import pystray
        return pystray.Icon.__module__

    for key, func in (("openvino", openvino_devices), ("catap", catap_devices), ("sounddevice", portaudio),
                      ("soundfile", libsndfile), ("pystray", pystray_backend)):
        bounded(result, key, func)


if __name__ == "__main__":
    main()
