# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY M3a spike: the smallest frozen app (PyInstaller) with Tk and pywhispercpp.

    WhisperProbe.app/Contents/MacOS/WhisperProbe <model.bin> <speech.wav> <result.json>
    FullProbe.app/Contents/MacOS/FullProbe tap <speech.wav> <result.json>

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
    if sys.argv[1] == "tap":
        tap(sys.argv[2], sys.argv[3])
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

    def genai():
        import openvino_genai
        return {"version": openvino_genai.__version__, "LLMPipeline": hasattr(openvino_genai, "LLMPipeline")}

    for key, func in (("openvino", openvino_devices), ("catap", catap_devices), ("sounddevice", portaudio),
                      ("soundfile", libsndfile), ("pystray", pystray_backend), ("openvino_genai", genai)):
        bounded(result, key, func)


def tcc_preflight() -> int:
    """Private TCC SPI (information only): 0 granted, 1 denied, 2 not determined."""
    import ctypes

    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/TCC")
    tcc.TCCAccessPreflight.restype = ctypes.c_int
    tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return int(tcc.TCCAccessPreflight(cf.CFStringCreateWithCString(None, b"kTCCServiceAudioCapture", 0x08000100), None))


def tap(wav_path: str, out_path: str) -> None:
    """System audio through catap while afplay plays the WAV: buffers, non-zero samples, peak."""
    import subprocess

    result: dict = {"pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False))}
    bounded(result, "tcc_preflight", tcc_preflight, 5)
    stats = {"buffers": 0, "frames": 0, "nonzero": 0, "peak": 0.0}
    try:
        import catap
        import numpy as np

        result["native_lib"] = (Path(catap.__file__).parent / "native" / "libcatap_coreaudio.dylib").exists()

        def on_buffer(buffer):
            stats["buffers"] += 1
            stats["frames"] += buffer.frame_count
            stats["format"] = f"{buffer.format.sample_rate} Hz {buffer.format.num_channels} ch {buffer.format.bits_per_sample} bit"
            if buffer.format.bits_per_sample == 32:
                samples = np.frombuffer(buffer.data, dtype=np.float32)
                stats["nonzero"] += int(np.count_nonzero(samples))
                if samples.size:
                    stats["peak"] = max(stats["peak"], float(np.abs(samples).max()))

        session = catap.RecordingSession.from_system_audio(on_buffer=on_buffer)
        bounded(result, "start", lambda: session.start() or "ok", 25)
        if result["start"] == "ok":
            player = subprocess.run(["afplay", wav_path], capture_output=True, timeout=20)
            result["afplay_rc"] = player.returncode
            time.sleep(0.5)
            bounded(result, "stop", lambda: session.stop() or "ok", 15)
            bounded(result, "captured_only_silence", lambda: bool(session.captured_only_silence), 5)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    bounded(result, "tcc_preflight_after", tcc_preflight, 5)
    result["stats"] = stats
    Path(out_path).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
