# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Screenshots of the real Ucho windows for docs/screens/, on synthetic data only.

Builds the app with a temporary settings file and a temporary recordings
folder filled with made-up meetings, walks through the screens in both
themes and both window layouts, and grabs each window by its rectangle
(DwmGetWindowAttribute / GetWindowRect via ctypes, PIL ImageGrab).

Nothing is recorded: the "recording" shots use a fake recorder that only
reports levels. No model is loaded.

    python tools/screenshots.py                    # all shots into docs/screens
    python tools/screenshots.py --only meeting     # shots whose name contains "meeting"
    python tools/screenshots.py --size 1093x535 --language pl --suffix=-pl-1366x768
                                # the window content of a 1366 x 768 screen at 125 %
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MEETINGS = [
    ("2026-11-13_15-30_Plan-wdrozenia", "Plan wdrożenia w zakładzie", "2026-11-13 15:30", 2710,
     "Wdrożenie w dwóch etapach: najpierw dział jakości, potem magazyn. Termin pilota przesunięty o tydzień.",
     ["Anna", "Piotr"]),
    ("2026-11-12_10-00_Weekly-sync", "Weekly sync", "2026-11-12 10:00", 1525,
     "Release moved to Thursday; the installer needs one more test on a clean Windows account.", []),
    ("2026-11-10_14-00_Budzet-Q1", "Budżet na pierwszy kwartał", "2026-11-10 14:00", 3302,
     "Budżet zatwierdzony bez zmian, poza dodatkową ofertą od drugiego dostawcy do piątku.",
     ["Marta"]),
    ("2026-11-07_09-15_Przeglad-sprintu", "Przegląd sprintu", "2026-11-07 09:15", 1840, "", []),
    ("2026-11-05_16-00_Customer-call", "Customer call: pilot scope", "2026-11-05 16:00", 2210,
     "The customer wants the pilot on one production line first, with weekly reports.", []),
]

TRANSCRIPT = [
    (4, "me", "Dzień dobry, zaczynamy od budżetu na pierwszy kwartał."),
    (11, "meeting:1", "Dobrze. Dostawca stali podniósł cenę o dziesięć procent od stycznia."),
    (24, "me", "To znaczy, że potrzebujemy drugiej oferty. Kto może ją zebrać do piątku?"),
    (31, "meeting:2", "Zajmę się tym, mam kontakt do dwóch hurtowni."),
    (45, "me", "Świetnie. Then let us keep the rest of the budget as it is."),
    (52, "meeting:1", "Agreed. One more thing: the training for the new line is scheduled for March."),
    (68, "me", "Dopiszmy to do planu. Czy szkolenie mieści się w budżecie?"),
    (75, "meeting:2", "Tak, było zaplanowane już w zeszłym roku, kwota się nie zmienia."),
    (90, "me", "W takim razie budżet zatwierdzamy, a ofertę porównamy w przyszłym tygodniu."),
    (104, "meeting:1", "Wyślę podsumowanie mailem jeszcze dzisiaj."),
]


def synthetic_archive(folder: Path) -> None:
    for name, title, date, duration, tldr, participants in MEETINGS:
        session = folder / name
        session.mkdir(parents=True)
        meta = {"name": title, "date": date, "duration_s": duration, "device": "GPU",
                "model": "OpenVINO/whisper-large-v3-int8-ov"}
        if tldr:
            meta["tldr"] = tldr
        if participants:
            meta["participants"] = participants
        if name.endswith("Budzet-Q1"):
            meta["segments"] = [{"start": s, "end": s + 6, "text": text, "speaker": who}
                                for s, who, text in TRANSCRIPT]
            meta["speaker_names"] = {"meeting:1": "Anna"}
            meta["key_points"] = ["Cena stali wzrosła o 10% od stycznia.",
                                  "Szkolenie dla nowej linii w marcu, w ramach budżetu."]
            meta["decisions"] = ["Budżet zatwierdzony bez zmian.",
                                 "Druga oferta od dostawcy do piątku."]
            (session / "transcript.md").write_text("# transcript", encoding="utf-8")
            (session / "summary.md").write_text("# summary", encoding="utf-8")
        else:
            meta["segments"] = [{"start": 3, "end": 8, "text": "Krótki fragment rozmowy o planie.",
                                 "speaker": "me"}]
            (session / "transcript.md").write_text("# transcript", encoding="utf-8")
            if name.endswith("Przeglad-sprintu"):
                # An (empty) audio track and no summary: the transcript screen then
                # shows its widest action row, "Transcribe again" and "Summarize" too.
                (session / "microphone.flac").write_bytes(b"")
        (session / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class FakeRecorder:
    """Stands in for audio.Recorder in the "recording" shots: levels only, no audio."""

    warnings: list = []
    levels = {"microphone": 0.62, "system": 0.48}


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS: the visible frame without the invisible resize border.
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect),
                                                  ctypes.sizeof(rect)) != 0:
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screens")
    parser.add_argument("--only", default="")
    parser.add_argument("--size", default="", help="logical window size, e.g. 1093x535")
    parser.add_argument("--language", default="en")
    parser.add_argument("--suffix", default="", help="appended to every file name, e.g. -pl-1366")
    args = parser.parse_args(argv)

    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    from PIL import ImageGrab

    from ucho import app as app_mod, config, i18n

    temp = Path(tempfile.mkdtemp(prefix="ucho-shots-"))
    recordings = temp / "recordings"
    synthetic_archive(recordings)
    settings_file = temp / "settings.json"
    settings_file.write_text(json.dumps({
        "recordings_dir": str(recordings), "ui_language": args.language, "setup_complete": True,
        "dictation_preload": False, "tray": True,
    }), encoding="utf-8")
    settings = config.Settings(settings_file)
    i18n.set_language(args.language)
    args.out.mkdir(parents=True, exist_ok=True)

    options = app_mod.Options(smoke_test=None)
    app = app_mod.App(settings, options)
    app.smoke = True  # no taskbar/Jump List/autostart side effects, no model preload
    app.probe_hardware = True  # the real hardware profile of this machine
    if args.size:
        w, h = (int(x) for x in args.size.split("x"))
        app.geometry(f"{w}x{h}+40+40")
    else:
        app.geometry("+40+40")
    app.dictation.last_text = ("Hi Marta, thanks for the call. I am sending the second supplier quote "
                               "by Friday, and the training stays in March.")
    app.dictation.model_state, app.dictation.model_device = "ready", "GPU"
    # Another program (e.g. an older Ucho) may hold a hotkey on the machine that
    # takes the shots; the docs show the normal state.
    app.hotkey_errors = {}
    session_dir = recordings / "2026-11-10_14-00_Budzet-Q1"

    def recording(on: bool):
        m = app.meeting
        if on:
            m.recorder, m.recording, m.recording_start = FakeRecorder(), True, time.time() - 754
        else:
            m.recorder, m.recording = None, False

    def live(on: bool):
        # Live transcription while recording (ucho/live_meeting.py), without a model.
        from types import SimpleNamespace

        from ucho.merge import Segment

        m = app.meeting
        recording(on)
        m.live = SimpleNamespace(gave_up=False) if on else None
        m.live_segments = [Segment(start, start + 5, text, who)
                           for start, who, text in TRANSCRIPT[:6]] if on else []
        if on:
            m._emit("live", m.live_segments)

    def dictating(on: bool):
        d = app.dictation
        d.recording = on
        d.recording_start = time.monotonic() - 7
        d.microphone.level = 0.21 if on else 0.0

    def transcribing(on: bool):
        # The live transcript screen of a running transcription, without a model:
        # only the controller state the screen reads (#14: the progress row).
        m = app.meeting
        m.transcribing, m.session_dir = on, session_dir if on else None
        m.progress = 0.12
        m.status_text = i18n.t("Transcribing on {device}...", device="GPU")
        m.live_text = [text for _start, _who, text in TRANSCRIPT[:4]] if on else []
        m.last_meta = {"name": "Budżet na pierwszy kwartał"} if on else {}

    shots: list[tuple[str, callable, object]] = []

    def shot(name, prepare, window=None):
        shots.append((name, prepare, window))

    for mode in ("light", "dark"):
        shot(f"meeting-{mode}", lambda m=mode: (app.set_theme(m), recording(False), app.show("meeting")))
        shot(f"meeting-recording-{mode}", lambda: (recording(True), app.show("meeting")))
        shot(f"meeting-live-{mode}", lambda: (live(True), app.show("meeting")))
        shot(f"dictation-{mode}", lambda: (live(False), recording(False), app.show("dictation")))
        shot(f"recordings-search-{mode}", lambda: app.show("recordings", query="budżet"))
        shot(f"transcript-{mode}", lambda: app.show("transcript", folder=session_dir, query=""))
        shot(f"transcript-transcribing-{mode}",
             lambda: (transcribing(True), app.show("transcript", live=True)))
        shot(f"settings-hardware-{mode}",
             lambda: (transcribing(False), app.show("settings", section="hardware")))
        shot(f"settings-llm-{mode}", lambda: app.show("settings", section="llm"))
        shot(f"settings-titles-{mode}", lambda: app.show("settings", section="titles"))
    shot("transcript-all-actions-light",
         lambda: (app.set_theme("light"),
                  app.show("transcript", folder=recordings / "2026-11-07_09-15_Przeglad-sprintu")))
    shot("wizard-language-light", lambda: (app.set_theme("light"), app.show_wizard(0)))
    shot("wizard-hardware-light", lambda: app.show_wizard(2))
    shot("wizard-llm-light", lambda: app.show_wizard(3))
    shot("wizard-consent-dark", lambda: (app.set_theme("dark"), app.show_wizard(4)))
    shot("layout-tray-main-light", lambda: (app.set_theme("light"), app.wizard().finish(),
                                            app.set_layout("tray"), app.show("meeting")))
    shot("layout-tray-dictation-light", lambda: (dictating(True), app.show_dictation()), "dictation")
    shot("layout-tray-dictation-dark", lambda: (app.set_theme("dark"), dictating(False)), "dictation")

    selected = [s for s in shots if args.only in s[0]]
    # Prepare needs the previous steps (theme, layout), so all run; only selected are saved.
    queue_ = list(shots)

    def next_shot():
        if not queue_:
            app.quit_app()
            return
        name, prepare, window = queue_.pop(0)
        prepare()
        slow = "settings" in name or "wizard-hardware" in name or "wizard-llm" in name
        app.after(1500 if slow else 900, lambda: capture(name, window))

    def capture(name, window):
        if (name, ) and any(s[0] == name for s in selected):
            target = app.dictation_window if window == "dictation" else app
            target.attributes("-topmost", True)
            target.lift()
            target.update()
            time.sleep(0.25)
            target.update()
            left, top, right, bottom = window_rect(app.window_handle(target))
            # 2 px inside the frame: the outermost row can show what lies behind the window.
            box = (left + 2, top + 2, right - 2, bottom - 2)
            ImageGrab.grab(bbox=box, all_screens=True).save(args.out / f"{name}{args.suffix}.png",
                                                            optimize=True)
            target.attributes("-topmost", False)
            print("saved", name, box)
        next_shot()

    app.after(1200, next_shot)
    try:
        app.mainloop()
    finally:
        app.shutdown()
        app.close()
        shutil.rmtree(temp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
