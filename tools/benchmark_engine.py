# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Dictation latency while a meeting is being transcribed, on synthetic speech.

Manual check for the shared engine (ucho/engine.py, plan v0.2 W3: "a 10 s
dictation is answered in under 3 s while a meeting transcribes"). Nothing is
recorded: the speech comes from the offline Windows voice (System.Speech,
"Microsoft Paulina" for Polish), written to WAV files in a temporary folder
and fed straight into the engine.

    python tools/benchmark_engine.py                       # shared and separate model, GPU
    python tools/benchmark_engine.py --device CPU --meeting-min 4
    python tools/benchmark_engine.py --live [--live-during-meeting] [--json out.json]
    python tools/benchmark_engine.py --make-wer-set C:\\bench\\tts-pl   # 20 sentences + .txt
    python tools/benchmark_wer.py C:\\bench\\tts-pl --language pl --model OpenVINO/whisper-large-v3-fp16-ov

Models must already be downloaded (HF_HOME or <repo>\\models); nothing is
downloaded and no network is used.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INT8 = "OpenVINO/whisper-large-v3-int8-ov"
FP16 = "OpenVINO/whisper-large-v3-fp16-ov"

# Short dictations of the kind people send to a terminal, a chat or an e-mail.
# Made up for this test; no real names or data.
SENTENCES = [
    "Dzień dobry, przesyłam poprawioną wersję oferty do jutra.",
    "Proszę o potwierdzenie terminu spotkania w czwartek o dziesiątej.",
    "Dodaj do listy zadań przegląd umowy z dostawcą.",
    "Raport sprzedaży za październik jest gotowy do wysyłki.",
    "Napisz test, który sprawdza migrację starych ustawień.",
    "Spotkanie zespołu przenosimy na piątek, salę rezerwuję sama.",
    "Faktura numer dwadzieścia trzy została opłacona wczoraj.",
    "Zmień kolor przycisku na ciemniejszy i sprawdź kontrast.",
    "Klient prosi o wycenę wdrożenia w dwóch zakładach.",
    "W załączniku jest harmonogram prac na najbliższy miesiąc.",
    "Uruchom ponownie serwer testowy po aktualizacji sterowników.",
    "Dziękuję za szybką odpowiedź, wracam z decyzją w poniedziałek.",
    "Budżet na szkolenia zostaje bez zmian do końca roku.",
    "Sprawdź, czy instalator działa na czystym koncie Windows.",
    "Prezentację dla zarządu skrócimy do dziesięciu slajdów.",
    "Wyślij mi proszę notatkę z wczorajszej rozmowy.",
    "Nowa linia produkcyjna rusza w marcu, po szkoleniu operatorów.",
    "Popraw literówkę w nagłówku i opublikuj stronę ponownie.",
    "Zamówienie dotrze do magazynu w przyszłym tygodniu.",
    "Proszę o kontakt telefoniczny w sprawie przedłużenia umowy.",
]

# A made-up meeting monologue, repeated and varied until it reaches the wanted length.
MEETING = [
    "Zaczynamy od przeglądu zadań z poprzedniego tygodnia.",
    "Dostawca stali podniósł cenę o dziesięć procent od stycznia.",
    "Potrzebujemy drugiej oferty, żeby porównać warunki dostawy.",
    "Szkolenie dla operatorów nowej linii zaplanowaliśmy na marzec.",
    "Wdrożenie podzielimy na dwa etapy, najpierw dział jakości, potem magazyn.",
    "Termin pilota przesuwamy o tydzień, bo czekamy na sprzęt.",
    "Raport z testów wyślę do piątku, razem z listą poprawek.",
    "Czy ktoś ma pytania do budżetu na pierwszy kwartał?",
]


def speak(lines: list[str], wav: Path, voice: str = "Microsoft Paulina Desktop", rate: int = 0) -> None:
    """Writes `lines` as one 16 kHz mono WAV with the offline Windows voice."""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try { $s.SelectVoice($env:TTS_VOICE) } catch { };"
        "$s.Rate = [int]$env:TTS_RATE;"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        "$s.SetOutputToWaveFile($env:TTS_OUT, $f);"
        "$p = New-Object System.Speech.Synthesis.PromptBuilder;"
        "foreach ($l in (Get-Content -Encoding UTF8 $env:TTS_TEXT)) { $p.AppendText($l); "
        "$p.AppendBreak([TimeSpan]::FromMilliseconds(700)) };"
        "$s.Speak($p); $s.Dispose()"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as text:
        text.write("\n".join(lines))
    env = dict(os.environ, TTS_VOICE=voice, TTS_RATE=str(rate), TTS_OUT=str(wav), TTS_TEXT=text.name)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, check=True,
                       capture_output=True)
    finally:
        os.unlink(text.name)


def load(wav: Path):
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(str(wav), dtype="float32")
    assert rate == 16_000, rate
    return np.ascontiguousarray(audio, dtype=np.float32)


class _Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def memory_gb() -> dict:
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters),
                                                 wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                                     counters.cb)
    gb = 1024 ** 3
    return {"working_set": round(counters.WorkingSetSize / gb, 2),
            "peak_working_set": round(counters.PeakWorkingSetSize / gb, 2),
            "private": round(counters.PrivateUsage / gb, 2),
            "peak_private": round(counters.PeakPagefileUsage / gb, 2)}


def scenario(name: str, dictation_model: str, device: str, meeting_files: dict,
             dictations: list, spacing_s: float, meeting_language: str = "auto") -> dict:
    from ucho import engine as engine_mod

    print(f"\n== {name}: transcription {INT8.split('/')[-1]}, dictation "
          f"{dictation_model.split('/')[-1]}, {device}")
    eng = engine_mod.Engine()
    eng.start()
    started = time.monotonic()
    eng.configure((dictation_model, device), preload=True).wait(600)
    load_s = time.monotonic() - started
    # Warm the transcription model as well, so the first chunk does not pay the compile.
    eng.submit(lambda job: eng.pipeline(INT8, device).load()).wait(600)
    memory_loaded = memory_gb()
    print(f"models loaded in {load_s:.1f} s, memory {memory_loaded}")

    idle = []
    for audio in dictations[:3]:
        t0 = time.monotonic()
        text, _seconds, used = eng.dictate(audio, dictation_model, device, "pl").wait(120)
        idle.append(time.monotonic() - t0)
    print("idle dictation latency:", [round(x, 2) for x in idle])

    t0 = time.monotonic()
    job = eng.transcribe(meeting_files, INT8, device, meeting_language)
    busy = []
    texts = []
    time.sleep(spacing_s)
    for audio in dictations:
        if job.done.is_set():
            break
        d0 = time.monotonic()
        text, _seconds, used = eng.dictate(audio, dictation_model, device, "pl").wait(300)
        busy.append(time.monotonic() - d0)
        texts.append(text)
        print(f"  dictation during transcription: {busy[-1]:.2f} s  ({len(text)} chars)")
        time.sleep(spacing_s)
    segments, used_device = job.wait(3600)
    meeting_s = time.monotonic() - t0
    memory_after = memory_gb()
    eng.stop()
    result = {
        "scenario": name, "device": used_device, "dictation_model": dictation_model,
        "meeting_language": meeting_language,
        "models_load_s": round(load_s, 1), "memory_after_load_gb": memory_loaded,
        "memory_end_gb": memory_after,
        "dictation_idle_s": [round(x, 2) for x in idle],
        "dictation_busy_s": [round(x, 2) for x in busy],
        "dictation_busy_max_s": round(max(busy), 2) if busy else None,
        "dictation_busy_median_s": round(statistics.median(busy), 2) if busy else None,
        "meeting_transcription_s": round(meeting_s, 1), "meeting_segments": len(segments),
        "sample_text": texts[0] if texts else "",
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def _build_meeting(temp: Path, meeting_min: float) -> tuple[Path, float]:
    import numpy as np
    import soundfile as sf

    block = temp / "block.wav"
    parts = []
    for rate in (0, 1, -1, 2):
        speak(MEETING, block, rate=rate)
        parts.append(load(block))
    meeting = np.concatenate(parts)
    target = int(meeting_min * 60 * 16_000)
    meeting = np.tile(meeting, target // meeting.size + 1)[:target]
    meeting_file = temp / "meeting.flac"
    sf.write(str(meeting_file), meeting, 16_000, format="FLAC")
    minutes = meeting.size / 16_000 / 60
    print(f"meeting: {minutes:.1f} min of synthetic speech")
    return meeting_file, minutes


def _run_live_fast(eng, model: str, device: str, audio, *, use_prompt: bool = True) -> tuple[str, int]:
    from ucho import live

    session = live.LiveDictation(eng, model, device, "auto", use_prompt=use_prompt)
    block_n = int(0.25 * live.SAMPLE_RATE)
    for offset in range(0, audio.size, block_n):
        session.feed(audio[offset: offset + block_n])
    text, _compute_s, _used = session.finish(audio).wait(600)
    return text, session.submitted


def _run_live_realtime(eng, model: str, device: str, audio, block_s: float = 0.25) -> tuple[float, int, str]:
    from ucho import live

    session = live.LiveDictation(eng, model, device, "auto")
    block_n = int(block_s * live.SAMPLE_RATE)
    started = time.monotonic()
    for k, offset in enumerate(range(0, audio.size, block_n)):
        due = started + k * block_s
        now = time.monotonic()
        if due > now:
            time.sleep(due - now)
        session.feed(audio[offset: offset + block_n])
    t_finish = time.monotonic()
    text, _compute_s, _used = session.finish(audio).wait(600)
    return time.monotonic() - t_finish, session.submitted, text


def _trim_end(audio, keep_s: float = 0.25):
    """Cuts the silence after the last word down to `keep_s`.

    The TTS voice ends with a 0.7 s pause, long enough for a cut, so the last
    piece would already be recognized when the hotkey is pressed. People press
    it right after the last word (the app adds dictation.TAIL_S = 0.25 s).
    """
    import numpy as np

    frame = 800
    count = audio.size // frame
    rms = np.sqrt(np.square(audio[: count * frame].reshape(count, frame)).mean(axis=1))
    loud = np.flatnonzero(rms > 0.01)
    if loud.size == 0:
        return audio
    end = min(audio.size, (int(loud[-1]) + 1) * frame + int(keep_s * 16_000))
    return np.ascontiguousarray(audio[:end])


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def live_benchmark(args: argparse.Namespace) -> int:
    import shutil

    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import benchmark_wer

    from ucho import engine as engine_mod

    temp = Path(tempfile.mkdtemp(prefix="ucho-live-bench-"))
    eng = engine_mod.Engine()
    try:
        long_wav = temp / "long.wav"
        speak(SENTENCES[0:13], long_wav)
        long_audio = load(long_wav)
        long_ref = " ".join(SENTENCES[0:13])
        long_s = long_audio.size / 16_000
        print(f"LONG dictation: {long_s:.1f} s ({len(SENTENCES[0:13])} sentences)")

        items: list[tuple[str, object, str]] = [("long", long_audio, long_ref)]
        for idx in range(4):
            chunk = SENTENCES[idx * 5: (idx + 1) * 5]
            wav = temp / f"group_{idx + 1}.wav"
            speak(chunk, wav)
            items.append((f"group_{idx + 1}", load(wav), " ".join(chunk)))

        model, device = args.live_model, args.device
        eng.start()
        eng.configure((model, device), preload=True).wait(600)

        # WER: whole vs live vs live-no-prompt.
        modes = ("whole", "live", "live_no_prompt")
        pooled_errors = {m: benchmark_wer.Errors() for m in modes}
        wer_rows: list[dict] = []
        print("\n== WER: whole vs live vs live-no-prompt (language=auto)")
        print(f"{'item':<10}  {'words':>5}  {'whole':>7}  {'live':>7}  {'no-prompt':>9}  {'pieces':>6}")
        for name, audio, ref in items:
            whole_text, _sec, _dev = eng.dictate(audio, model, device, "auto").wait(600)
            live_text, pieces = _run_live_fast(eng, model, device, audio, use_prompt=True)
            noprompt_text, _np_pieces = _run_live_fast(eng, model, device, audio, use_prompt=False)

            row_errs = {
                "whole": benchmark_wer.word_errors(ref, whole_text),
                "live": benchmark_wer.word_errors(ref, live_text),
                "live_no_prompt": benchmark_wer.word_errors(ref, noprompt_text),
            }
            for m in modes:
                pooled_errors[m] = pooled_errors[m] + row_errs[m]
            w_wer = {m: benchmark_wer.wer(ref, txt) for m, txt in
                     (("whole", whole_text), ("live", live_text), ("live_no_prompt", noprompt_text))}
            wer_rows.append({
                "item": name,
                "reference_words": row_errs["whole"].reference_length,
                "pieces": pieces,
                "wer": {m: (round(v, 4) if v is not None else None) for m, v in w_wer.items()},
                # Synthetic speech only, so the texts may be kept for a closer look.
                "texts": {"whole": whole_text, "live": live_text, "live_no_prompt": noprompt_text},
            })
            print(f"{name:<10}  {row_errs['whole'].reference_length:>5}  "
                  f"{_pct(w_wer['whole']):>7}  {_pct(w_wer['live']):>7}  "
                  f"{_pct(w_wer['live_no_prompt']):>9}  {pieces:>6}")

        pooled_wer = {m: benchmark_wer.rate(pooled_errors[m]) for m in modes}
        print(f"{'POOLED':<10}  {pooled_errors['whole'].reference_length:>5}  "
              f"{_pct(pooled_wer['whole']):>7}  {_pct(pooled_wer['live']):>7}  "
              f"{_pct(pooled_wer['live_no_prompt']):>9}")

        # Latency for LONG, --live-runs times each, stopped right after the last word.
        long_audio = _trim_end(long_audio)
        long_s = long_audio.size / 16_000
        today_latencies: list[float] = []
        live_latencies: list[float] = []
        live_pieces: list[int] = []
        for _ in range(args.live_runs):
            t0 = time.monotonic()
            eng.dictate(long_audio, model, device, "auto").wait(600)
            today_latencies.append(time.monotonic() - t0)

            lat, pieces, _txt = _run_live_realtime(eng, model, device, long_audio)
            live_latencies.append(lat)
            live_pieces.append(pieces)

        print(f"\n== Stop-to-text latency on LONG ({long_s:.1f} s), {args.live_runs} run(s)")
        print(f"today: median {statistics.median(today_latencies):.2f} s, "
              f"max {max(today_latencies):.2f} s")
        print(f"live:  median {statistics.median(live_latencies):.2f} s, "
              f"max {max(live_latencies):.2f} s, pieces {live_pieces}")

        report: dict = {
            "model": model,
            "device": device,
            "language": "auto",
            "long_s": round(long_s, 2),
            "wer_items": wer_rows,
            "wer_pooled": {m: (round(v, 4) if v is not None else None) for m, v in pooled_wer.items()},
            "latency_today_s": [round(x, 3) for x in today_latencies],
            "latency_today_median_s": round(statistics.median(today_latencies), 3),
            "latency_today_max_s": round(max(today_latencies), 3),
            "latency_live_s": [round(x, 3) for x in live_latencies],
            "latency_live_median_s": round(statistics.median(live_latencies), 3),
            "latency_live_max_s": round(max(live_latencies), 3),
            "live_pieces": live_pieces,
        }

        if args.live_during_meeting:
            meeting_file, _mins = _build_meeting(temp, args.meeting_min)
            eng.submit(lambda _j: eng.pipeline(INT8, device).load()).wait(600)

            t0 = time.monotonic()
            eng.transcribe({"system": meeting_file}, INT8, device, args.meeting_language).wait(3600)
            alone_s = time.monotonic() - t0

            meeting_job = eng.transcribe({"system": meeting_file}, INT8, device, args.meeting_language)
            during_lat, during_pieces, _txt = _run_live_realtime(eng, model, device, long_audio)
            meeting_job.wait(3600)
            # The job's own times: the live run above blocks for the whole dictation.
            during_s = meeting_job.finished - meeting_job.submitted

            print("\n== Live during meeting")
            print(f"stop-to-text latency: {during_lat:.2f} s ({during_pieces} pieces)")
            print(f"meeting alone: {alone_s:.1f} s, with live dictation: {during_s:.1f} s "
                  f"({during_s - alone_s:+.1f} s)")
            report["during_meeting"] = {
                "stop_to_text_s": round(during_lat, 3),
                "pieces": during_pieces,
                "meeting_alone_s": round(alone_s, 2),
                "meeting_with_live_s": round(during_s, 2),
            }

        if args.json:
            args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        eng.stop()
        shutil.rmtree(temp, ignore_errors=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="GPU")
    parser.add_argument("--meeting-min", type=float, default=6.0)
    parser.add_argument("--dictations", type=int, default=6)
    parser.add_argument("--spacing", type=float, default=3.0, help="seconds between dictations")
    parser.add_argument("--only", choices=["shared", "separate"], default=None)
    parser.add_argument("--meeting-language", default="auto",
                        help="auto (12 s chunks, as in the app by default) or pl/en (chunks up to 5 min)")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--make-wer-set", type=Path, default=None,
                        help="write the 20 sentences as WAV + reference .txt into this folder and exit")
    parser.add_argument("--live", action="store_true",
                        help="benchmark live piecewise dictation (WER and stop-to-text latency)")
    parser.add_argument("--live-model", default=FP16)
    parser.add_argument("--live-runs", type=int, default=3)
    parser.add_argument("--live-during-meeting", action="store_true",
                        help="also run live dictation during a synthetic meeting transcription")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.live:
        return live_benchmark(args)

    if args.make_wer_set:
        args.make_wer_set.mkdir(parents=True, exist_ok=True)
        for i, sentence in enumerate(SENTENCES, start=1):
            stem = args.make_wer_set / f"tts_pl_{i:02d}"
            speak([sentence], stem.with_suffix(".wav"))
            stem.with_suffix(".txt").write_text(sentence + "\n", encoding="utf-8")
        print(f"{len(SENTENCES)} files in {args.make_wer_set}")
        return 0

    import numpy as np

    temp = Path(tempfile.mkdtemp(prefix="ucho-engine-bench-"))
    try:
        # The meeting: the monologue, repeated with different speaking rates.
        meeting_file, _mins = _build_meeting(temp, args.meeting_min)

        dictations = []
        for i in range(args.dictations):
            wav = temp / f"dictation_{i}.wav"
            speak(SENTENCES[2 * i % len(SENTENCES): 2 * i % len(SENTENCES) + 2], wav)
            audio = load(wav)
            # About 10 s, as in the acceptance criterion.
            audio = np.concatenate([audio, np.zeros(max(0, 10 * 16_000 - audio.size), np.float32)])
            dictations.append(audio[: 10 * 16_000])
        print(f"{len(dictations)} dictations of 10 s")

        results = []
        if args.only in (None, "shared"):
            results.append(scenario("shared", INT8, args.device, {"system": meeting_file},
                                    dictations, args.spacing, args.meeting_language))
        if args.only in (None, "separate"):
            results.append(scenario("separate", FP16, args.device, {"system": meeting_file},
                                    dictations, args.spacing, args.meeting_language))
        if args.json:
            args.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        import shutil

        shutil.rmtree(temp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
