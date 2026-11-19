# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY macOS CI spike for task M3a (issue #22). Not part of the app.

Runs on GitHub macOS runners (.github/workflows/macos-spike.yml) and answers the
questions of docs/macos-plan.md, M3a and section 8. Every check runs in its own
subprocess with a timeout, so a crash or a hang is recorded as a result instead of
stopping the run. Each check writes <out>/<check>.json; `summary` turns them into
Markdown for $GITHUB_STEP_SUMMARY.

    python tools/macos_spike/spike.py all --out DIR
    python tools/macos_spike/spike.py run tk --out DIR
    python tools/macos_spike/spike.py summary --out DIR

Delete together with the workflow once M3b has its own macos.yml.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent

WHISPER_REPO = "ggerganov/whisper.cpp"
# Repo sha from the plan (M8), checked in the Hugging Face API on 2026-11-19.
WHISPER_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
WHISPER_FILES = ["ggml-tiny-q5_1.bin", "ggml-base-q5_1.bin"]

TEXT_EN = "The quarterly budget meeting starts at nine o'clock tomorrow morning in the small conference room."
KEYWORDS_EN = ["budget", "meeting", "tomorrow", "morning", "conference"]
TEXT_PL = "Spotkanie w sprawie budżetu zaczyna się jutro o dziewiątej rano w małej sali konferencyjnej."
KEYWORDS_PL = ["spotkanie", "budżetu", "jutro", "rano", "sali"]

# Carbon constants (CarbonEvents.h, Events.h).
CMD_KEY, SHIFT_KEY, OPTION_KEY, CONTROL_KEY = 1 << 8, 1 << 9, 1 << 11, 1 << 12
KVK_ANSI_R, KVK_ANSI_J, KVK_SPACE, KVK_ESCAPE = 0x0F, 0x26, 0x31, 0x35
HOTKEY_EXCLUSIVE = 1 << 0  # kEventHotKeyExclusive
HOTKEY_ERRORS = {0: "noErr", -9878: "eventHotKeyExistsErr", -9879: "eventHotKeyInvalidErr", -50: "paramErr"}


# ---------------------------------------------------------------- helpers

def run_cmd(args: list[str], timeout: float = 60, tail: int = 4000) -> dict:
    start = time.monotonic()
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"rc": done.returncode, "out": done.stdout[-tail:], "err": done.stderr[-tail:],
                "seconds": round(time.monotonic() - start, 2)}
    except subprocess.TimeoutExpired:
        return {"rc": "timeout", "seconds": round(time.monotonic() - start, 2)}
    except OSError as error:
        return {"rc": "oserror", "err": repr(error)}


def guard(details: dict, key: str, func: Callable[[], object]) -> object:
    """Store func() in details[key], or the exception, and never raise."""
    try:
        value = func()
        details[key] = value
        return value
    except BaseException as error:
        details[key] = {"error": f"{type(error).__name__}: {error}"}
        return None


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


def tk_root(title: str):
    import tkinter as tk

    root = tk.Tk()
    root.title(title)
    root.geometry("360x160+120+120")
    tk.Label(root, text=f"Ucho spike: {title}").pack(padx=20, pady=20)
    root.update()
    return root


class Ticker:
    """Counts Tk `after` ticks: proves the main loop keeps running (not blocked)."""

    def __init__(self, root, every_ms: int = 100):
        self.root, self.every, self.count = root, every_ms, 0
        root.after(every_ms, self._tick)

    def _tick(self):
        self.count += 1
        self.root.after(self.every, self._tick)


# ---------------------------------------------------------------- checks

def check_install(out: Path) -> dict:
    details: dict = {}
    report = out / "pip-report.json"
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        wheels = []
        for item in data.get("install", []):
            url = (item.get("download_info") or {}).get("url", "")
            wheels.append({"name": item["metadata"]["name"], "version": item["metadata"]["version"],
                           "file": url.rsplit("/", 1)[-1]})
        details["wheels"] = sorted(wheels, key=lambda w: w["name"].lower())
        details["sdists"] = [w for w in wheels if not w["file"].endswith(".whl")]
    for name in ("pip-status.txt", "pip-each.txt", "install-seconds.txt"):
        path = out / name
        if path.exists():
            details[name] = path.read_text(encoding="utf-8").strip()
    status_text = details.get("pip-status.txt", "missing")
    ok = status_text == "ok" and not details.get("sdists")
    return {"status": "ok" if ok else "fail",
            "summary": f"pip: {status_text}, {len(details.get('wheels', []))} distributions, "
                       f"{details.get('install-seconds.txt', '?')} s, sdists: {len(details.get('sdists', []))}",
            "details": details}


def check_env(out: Path) -> dict:
    d: dict = {"platform": platform.platform(), "mac_ver": platform.mac_ver()[0],
               "machine": platform.machine(), "python": sys.version, "cpu_count": os.cpu_count()}
    d["sw_vers"] = run_cmd(["sw_vers"]).get("out", "").strip()
    guard(d, "memory_gb", lambda: round(int(run_cmd(["sysctl", "-n", "hw.memsize"])["out"]) / 2**30, 1))
    d["cpu_brand"] = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"]).get("out", "").strip()
    d["hw_model"] = run_cmd(["sysctl", "-n", "hw.model"]).get("out", "").strip()
    d["displays"] = run_cmd(["system_profiler", "SPDisplaysDataType"], timeout=60).get("out", "")[-1500:]
    d["tmpdir_len"] = len(os.environ.get("TMPDIR", ""))
    sock = Path.home() / "Library" / "Application Support" / "Ucho" / "ucho.sock"
    d["socket_path_len"] = len(str(sock))
    d["console_user"] = run_cmd(["stat", "-f", "%Su", "/dev/console"]).get("out", "").strip()
    d["gui_session"] = run_cmd(["launchctl", "managername"]).get("out", "").strip()
    return {"status": "info",
            "summary": f"macOS {d['mac_ver']} {d['machine']}, {d['cpu_count']} CPU, {d.get('memory_gb')} GB, "
                       f"{d['cpu_brand'] or d['hw_model']}, session {d['gui_session']}",
            "details": d}


IMPORTS = ["numpy", "soundfile", "soxr", "PIL", "huggingface_hub", "hf_xet", "openvino", "openvino_genai",
           "openvino_tokenizers", "sounddevice", "pywhispercpp.model", "catap", "objc", "AppKit", "Quartz",
           "ApplicationServices", "AVFoundation", "CoreAudio", "sherpa_onnx", "tkinter", "customtkinter",
           "pystray", "PyInstaller"]


def check_imports(out: Path) -> dict:
    import importlib
    import importlib.metadata as md

    d: dict = {"modules": {}}
    failed = []
    for name in IMPORTS:
        start = time.monotonic()
        try:
            module = importlib.import_module(name)
            d["modules"][name] = {"ok": True, "seconds": round(time.monotonic() - start, 2),
                                  "version": str(getattr(module, "__version__", ""))}
        except BaseException as error:
            failed.append(name)
            d["modules"][name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def openvino_info():
        import openvino as ov
        core = ov.Core()
        return {dev: core.get_property(dev, "FULL_DEVICE_NAME") for dev in core.available_devices}

    guard(d, "openvino_devices", openvino_info)

    def genai_api():
        import openvino_genai as g
        return {n: hasattr(g, n) for n in ("LLMPipeline", "WhisperPipeline", "StructuredOutputConfig",
                                           "StreamingStatus", "GenerationConfig")}

    guard(d, "openvino_genai_api", genai_api)

    def sounddevice_info():
        import sounddevice as sd
        devices = [{"name": dev["name"], "in": dev["max_input_channels"], "out": dev["max_output_channels"],
                    "rate": dev["default_samplerate"]} for dev in sd.query_devices()]
        return {"portaudio": sd.get_portaudio_version()[1], "default": list(sd.default.device), "devices": devices}

    guard(d, "sounddevice", sounddevice_info)
    d["permissions"] = permission_snapshot()

    def sizes():
        result = {}
        for dist in md.distributions():
            total = 0
            for file in dist.files or []:
                with contextlib.suppress(OSError):
                    total += Path(dist.locate_file(file)).stat().st_size
            result[dist.metadata["Name"]] = round(total / 2**20, 1)
        return dict(sorted(result.items(), key=lambda kv: -kv[1]))

    guard(d, "installed_mb", sizes)
    if isinstance(d.get("installed_mb"), dict):
        pyobjc = sum(v for k, v in d["installed_mb"].items() if k.lower().startswith("pyobjc"))
        d["installed_mb_pyobjc_total"] = round(pyobjc, 1)
        d["installed_mb_total"] = round(sum(d["installed_mb"].values()), 1)
    return {"status": "fail" if failed else "ok",
            "summary": ("all imports ok" if not failed else f"failed: {', '.join(failed)}")
                       + f"; OpenVINO devices: {d.get('openvino_devices')}; installed {d.get('installed_mb_total')} MB",
            "details": d}


def permission_snapshot() -> dict:
    p: dict = {}

    def microphone():
        import AVFoundation
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized"}.get(status, status)

    def accessibility():
        import ApplicationServices
        return bool(ApplicationServices.AXIsProcessTrusted())

    def quartz(name):
        import Quartz
        return bool(getattr(Quartz, name)())

    def secure_input():
        import ctypes
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        return bool(carbon.IsSecureEventInputEnabled())

    guard(p, "microphone", microphone)
    guard(p, "accessibility_AXIsProcessTrusted", accessibility)
    guard(p, "screen_capture_preflight", lambda: quartz("CGPreflightScreenCaptureAccess"))
    guard(p, "post_event_preflight", lambda: quartz("CGPreflightPostEventAccess"))
    guard(p, "listen_event_preflight", lambda: quartz("CGPreflightListenEventAccess"))
    guard(p, "secure_event_input", secure_input)
    return p


def check_tk(out: Path) -> dict:
    import tkinter as tk
    import tkinter.font as tkfont

    d: dict = {"TkVersion": tk.TkVersion, "TclVersion": tk.TclVersion}
    root = tk_root("tk")
    d["patchlevel"] = root.tk.call("info", "patchlevel")
    d["windowingsystem"] = root.tk.call("tk", "windowingsystem")
    d["scaling"] = float(root.tk.call("tk", "scaling"))
    d["mapped"] = bool(root.winfo_ismapped())
    d["screen"] = [root.winfo_screenwidth(), root.winfo_screenheight()]
    families = set(tkfont.families())
    d["font_families_count"] = len(families)
    d["fonts_present"] = {f: f in families for f in (".AppleSystemUIFont", "SF Pro", "SF Pro Text", "SF Pro Display",
                                                       "SF Mono", "Helvetica Neue", "Helvetica", "Menlo", "Monaco")}
    d["default_font"] = tkfont.nametofont("TkDefaultFont").actual()
    d["mac_commands"] = {c: bool(root.tk.call("info", "commands", c))
                         for c in ("::tk::mac::ReopenApplication", "::tk::mac::Quit", "::tk::mac::ShowPreferences")}
    ticker = Ticker(root)
    start = time.monotonic()
    root.after(1500, lambda: d.update(screencapture=run_cmd(["screencapture", "-x", str(out / "tk.png")], 20)))
    root.after(2500, root.quit)
    root.mainloop()
    d["loop_seconds"], d["ticks"] = round(time.monotonic() - start, 2), ticker.count
    root.destroy()
    ok = d["windowingsystem"] == "aqua" and d["mapped"] and ticker.count > 10
    return {"status": "ok" if ok else "fail",
            "summary": f"Tk {d['patchlevel']} {d['windowingsystem']}, mapped={d['mapped']}, ticks={ticker.count}, "
                       f"SF Pro={d['fonts_present']['SF Pro']}, .AppleSystemUIFont={d['fonts_present']['.AppleSystemUIFont']}",
            "details": d}


def check_ctk(out: Path) -> dict:
    import customtkinter as ctk

    d: dict = {"version": ctk.__version__}
    app = ctk.CTk()
    app.title("Ucho spike: customtkinter")
    app.geometry("360x200+520+120")
    ctk.CTkButton(app, text="Record").pack(padx=20, pady=20)
    ctk.CTkEntry(app, placeholder_text="Search").pack(padx=20)
    app.update()
    d["mapped"] = bool(app.winfo_ismapped())
    d["appearance"] = ctk.get_appearance_mode()
    ticker = Ticker(app)
    app.after(1500, lambda: d.update(screencapture=run_cmd(["screencapture", "-x", str(out / "ctk.png")], 20)))
    app.after(2500, app.quit)
    app.mainloop()
    d["ticks"] = ticker.count
    app.destroy()
    return {"status": "ok" if d["mapped"] and ticker.count > 10 else "fail",
            "summary": f"customtkinter {d['version']}, mapped={d['mapped']}, appearance={d['appearance']}, ticks={ticker.count}",
            "details": d}


def _tray_image(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=(*color, 255))
    return image


def _status_item_state(item) -> dict:
    state = {"isVisible": bool(item.isVisible())}
    button = item.button()
    if button is not None:
        state["button_hidden"] = bool(button.isHidden())
        window = button.window()
        if window is not None:
            frame = window.frame()
            state["button_window_frame"] = [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height]
            state["button_window_visible"] = bool(window.isVisible())
    menu = item.menu()
    state["menu_items"] = int(menu.numberOfItems()) if menu is not None else None
    return state


def tray_pystray(out: Path, mode: str) -> dict:
    """pystray next to Tk, the way ucho/tray.py starts it (run_detached on the Tk thread)."""
    import AppKit
    import pystray

    d: dict = {"mode": mode}
    root = tk_root(f"tray {mode}")
    d["nsapp_class"] = str(AppKit.NSApplication.sharedApplication().className())
    calls: list = []

    def action(name):
        return lambda icon, item: calls.append({"item": name, "main_thread": threading.current_thread()
                                                is threading.main_thread()})

    menu = pystray.Menu(pystray.MenuItem("Record", action("record"), default=True),
                        pystray.MenuItem("Dictate", action("dictate")),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem("Quit", action("quit")))
    icon = pystray.Icon("ucho-spike", _tray_image((40, 120, 220)), "Ucho spike", menu)
    if mode == "default":
        icon.run_detached()  # setup thread sets visible=True off the main thread (pystray default)
    else:
        icon.run_detached(setup=lambda _icon: None)
        root.after(100, lambda: setattr(icon, "visible", True))  # visible set on the Tk thread
    ticker = Ticker(root)
    steps: dict = {}

    def at(ms, key, func):
        root.after(ms, lambda: guard(steps, key, func))

    at(1000, "state_after_start", lambda: _status_item_state(icon._status_item))
    at(2000, "menu_action", lambda: icon._status_item.menu().performActionForItemAtIndex_(0) or True)

    def update():
        icon.icon = _tray_image((220, 40, 40))
        icon.title = "Ucho spike: recording"
        icon.update_menu()
        return _status_item_state(icon._status_item)

    at(3000, "update_from_tk_thread", update)
    at(4000, "stop", lambda: icon.stop() or True)
    at(4500, "state_after_stop", lambda: _status_item_state(icon._status_item))
    at(5000, "screencapture", lambda: run_cmd(["screencapture", "-x", str(out / f"tray_{mode}.png")], 20))
    start = time.monotonic()
    root.after(10000, root.quit)
    root.mainloop()
    d.update(steps)
    d["loop_seconds"], d["ticks"], d["calls"] = round(time.monotonic() - start, 2), ticker.count, calls
    root.destroy()
    ok = bool(calls) and ticker.count > 60 and d["loop_seconds"] < 14
    return {"status": "ok" if ok else "fail",
            "summary": f"NSApp={d['nsapp_class']}, menu callback={calls[:1]}, ticks={ticker.count} in "
                       f"{d['loop_seconds']} s, visible after stop={(d.get('state_after_stop') or {}).get('isVisible')}",
            "details": d}


def check_tray_pystray_default(out: Path) -> dict:
    return tray_pystray(out, "default")


def check_tray_pystray_main(out: Path) -> dict:
    return tray_pystray(out, "main")


def check_tray_thread(out: Path) -> dict:
    """Expected to fail: pystray's own loop (icon.run) in a worker thread next to Tk."""
    import pystray

    d: dict = {}
    root = tk_root("tray thread")
    icon = pystray.Icon("ucho-spike-thread", _tray_image((40, 160, 80)), "Ucho spike thread",
                        pystray.Menu(pystray.MenuItem("Record", lambda: None)))

    def target():
        try:
            icon.run()
            d["thread_result"] = "returned"
        except BaseException as error:
            d["thread_result"] = f"{type(error).__name__}: {error}"

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    ticker = Ticker(root)
    start = time.monotonic()
    root.after(6000, root.quit)
    root.mainloop()
    d["loop_seconds"], d["ticks"], d["thread_alive"] = round(time.monotonic() - start, 2), ticker.count, thread.is_alive()
    return {"status": "info",
            "summary": f"thread result={d.get('thread_result', 'still running')}, ticks={ticker.count}",
            "details": d}


def check_tray_nsstatusitem(out: Path) -> dict:
    """Plan B: own NSStatusItem through PyObjC on the Tk thread, no pystray."""
    import AppKit
    import Foundation
    import objc

    d: dict = {}
    calls: list = []
    root = tk_root("NSStatusItem")

    class SpikeTarget(Foundation.NSObject):
        @objc.typedSelector(b"v@:@")
        def fire_(self, sender):
            calls.append({"title": str(sender.title()),
                          "main_thread": threading.current_thread() is threading.main_thread()})

    target = SpikeTarget.alloc().init()
    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    item.button().setTitle_("U")
    item.button().setToolTip_("Ucho spike")
    menu = AppKit.NSMenu.alloc().init()
    for title in ("Record", "Dictate"):
        entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "fire:", "")
        entry.setTarget_(target)
        menu.addItem_(entry)
    item.setMenu_(menu)
    ticker = Ticker(root)
    steps: dict = {}
    root.after(1000, lambda: guard(steps, "state", lambda: _status_item_state(item)))
    root.after(2000, lambda: guard(steps, "menu_action", lambda: menu.performActionForItemAtIndex_(1) or True))
    root.after(3000, lambda: guard(steps, "remove", lambda: AppKit.NSStatusBar.systemStatusBar()
                                   .removeStatusItem_(item) or True))
    root.after(5000, root.quit)
    root.mainloop()
    d.update(steps)
    d["calls"], d["ticks"] = calls, ticker.count
    root.destroy()
    return {"status": "ok" if calls and ticker.count > 30 else "fail",
            "summary": f"menu callback={calls[:1]}, ticks={ticker.count}, state={steps.get('state')}",
            "details": d}


class Carbon:
    """Minimal ctypes binding of Carbon global hotkeys (RegisterEventHotKey)."""

    def __init__(self):
        import ctypes

        self.ct = ctypes
        lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

        class EventHotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        self.EventHotKeyID, self.EventTypeSpec = EventHotKeyID, EventTypeSpec
        self.HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        lib.GetApplicationEventTarget.restype = ctypes.c_void_p
        lib.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID, ctypes.c_void_p,
                                            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        lib.RegisterEventHotKey.restype = ctypes.c_int32
        lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        lib.UnregisterEventHotKey.restype = ctypes.c_int32
        lib.InstallEventHandler.argtypes = [ctypes.c_void_p, self.HANDLER, ctypes.c_uint32,
                                            ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        lib.InstallEventHandler.restype = ctypes.c_int32
        lib.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                          ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        lib.GetEventParameter.restype = ctypes.c_int32
        self.lib = lib
        self.target = lib.GetApplicationEventTarget()
        self._handler = None

    def register(self, key: int, modifiers: int, number: int, options: int) -> tuple[int, object]:
        ref = self.ct.c_void_p()
        status = self.lib.RegisterEventHotKey(key, modifiers, self.EventHotKeyID(fourcc("UCHO"), number),
                                              self.target, options, self.ct.byref(ref))
        return status, ref

    def unregister(self, ref) -> int:
        return self.lib.UnregisterEventHotKey(ref)

    def install(self, on_hotkey: Callable[[int], None]) -> int:
        ct = self.ct

        def handler(_call_ref, event, _user):
            hotkey = self.EventHotKeyID()
            self.lib.GetEventParameter(event, fourcc("----"), fourcc("hkid"), None,
                                       ct.sizeof(hotkey), None, ct.byref(hotkey))
            on_hotkey(hotkey.id)
            return 0

        self._handler = self.HANDLER(handler)  # keep a reference for the process lifetime
        spec = self.EventTypeSpec(fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        return self.lib.InstallEventHandler(self.target, self._handler, 1, ct.byref(spec), None,
                                            ct.byref(ct.c_void_p()))


def _err(status: int) -> str:
    return f"{status} {HOTKEY_ERRORS.get(status, '')}".strip()


def check_hotkey(out: Path) -> dict:
    d: dict = {"permissions": permission_snapshot()}
    root = tk_root("hotkey")
    carbon = Carbon()
    fired: list = []
    d["install_handler"] = _err(carbon.install(lambda number: fired.append(
        {"id": number, "main_thread": threading.current_thread() is threading.main_thread(),
         "t": round(time.monotonic(), 2)})))
    regs: dict = {}

    def reg(name, key, mods, number, options=HOTKEY_EXCLUSIVE):
        status, ref = carbon.register(key, mods, number, options)
        regs[name] = {"status": _err(status), "ref": ref}
        return status

    reg("ctrl+alt+r (Ucho record default)", KVK_ANSI_R, CONTROL_KEY | OPTION_KEY, 1)
    reg("ctrl+alt+r again, same process, exclusive", KVK_ANSI_R, CONTROL_KEY | OPTION_KEY, 2)
    reg("ctrl+alt+r again, same process, no options", KVK_ANSI_R, CONTROL_KEY | OPTION_KEY, 3, 0)
    reg("ctrl+shift+space (Ucho dictation default)", KVK_SPACE, CONTROL_KEY | SHIFT_KEY, 4)
    reg("cmd+space (Spotlight)", KVK_SPACE, CMD_KEY, 5)
    reg("ctrl+space (input source)", KVK_SPACE, CONTROL_KEY, 6)
    reg("esc (dictation cancel)", KVK_ESCAPE, 0, 7)
    regs["esc (dictation cancel)"]["unregister"] = _err(carbon.unregister(regs["esc (dictation cancel)"]["ref"]))

    # Another process holds ctrl+alt+J exclusively: does our registration see it?
    holder = subprocess.Popen([sys.executable, str(Path(__file__)), "hotkey-holder"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = ""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = holder.stdout.readline().strip() if holder.stdout else ""
        if line.startswith("holder") or not line:
            break
    d["holder_line"] = line
    reg("ctrl+alt+j held by another process, exclusive", KVK_ANSI_J, CONTROL_KEY | OPTION_KEY, 8)
    reg("ctrl+alt+j held by another process, no options", KVK_ANSI_J, CONTROL_KEY | OPTION_KEY, 9, 0)

    def post_keys():
        import Quartz

        result = {}
        for name, tap in (("hid", Quartz.kCGHIDEventTap), ("session", Quartz.kCGSessionEventTap)):
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, KVK_ANSI_R, down)
                Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate)
                Quartz.CGEventPost(tap, event)
            result[name] = round(time.monotonic(), 2)
        return result

    steps: dict = {}
    root.after(1000, lambda: guard(steps, "synthetic_post", post_keys))
    ticker = Ticker(root)
    root.after(4000, root.quit)
    root.mainloop()
    holder.wait(timeout=20)
    d["holder_output"] = holder.stdout.read()[-1000:] if holder.stdout else ""
    for value in regs.values():
        value.pop("ref", None)
    d["registrations"] = regs
    d.update(steps)
    d["fired"], d["ticks"] = fired, ticker.count
    root.destroy()
    base_ok = regs["ctrl+alt+r (Ucho record default)"]["status"].startswith("0 ")
    cross = regs["ctrl+alt+j held by another process, exclusive"]["status"]
    return {"status": "ok" if base_ok else "fail",
            "summary": f"register={regs['ctrl+alt+r (Ucho record default)']['status']}, "
                       f"dup same process={regs['ctrl+alt+r again, same process, exclusive']['status']}, "
                       f"held by other process={cross}, cmd+space={regs['cmd+space (Spotlight)']['status']}, "
                       f"synthetic event fired={len(fired)}",
            "details": d}


def hotkey_holder() -> None:
    root = tk_root("hotkey holder")
    carbon = Carbon()
    carbon.install(lambda number: None)
    status, _ref = carbon.register(KVK_ANSI_J, CONTROL_KEY | OPTION_KEY, 1, HOTKEY_EXCLUSIVE)
    print(f"holder registered ctrl+alt+j: {_err(status)}", flush=True)
    root.after(6000, root.quit)
    root.mainloop()


def _parse_voices(text: str) -> list[dict]:
    voices = []
    for line in text.splitlines():
        match = re.match(r"^(.*?)\s+([a-z]{2,3}_[A-Z0-9]{2,3})\s+#", line)
        if match:
            voices.append({"name": match.group(1).strip(), "locale": match.group(2)})
    return voices


def check_tts(out: Path) -> dict:
    import soundfile as sf

    listing = run_cmd(["say", "-v", "?"])
    voices = _parse_voices(listing.get("out", ""))
    d: dict = {"voice_count": len(voices),
               "pl_voices": [v["name"] for v in voices if v["locale"] == "pl_PL"],
               "en_voices": [v["name"] for v in voices if v["locale"] in ("en_US", "en_GB")][:10]}
    made = {}
    for lang, text, names in (("en", TEXT_EN, d["en_voices"]), ("pl", TEXT_PL, d["pl_voices"])):
        path = out / f"tts_{lang}.wav"
        args = ["say", "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@16000", text]
        if names:
            args[1:1] = ["-v", names[0]]
        result = run_cmd(args, timeout=60)
        entry = {"voice": names[0] if names else "(default)", "rc": result.get("rc"), "seconds": result.get("seconds"),
                 "err": result.get("err", "")[-300:]}
        if path.exists():
            info = sf.info(str(path))
            entry.update(samplerate=info.samplerate, channels=info.channels, duration=round(info.duration, 2),
                         bytes=path.stat().st_size)
            made[lang] = entry["samplerate"] == 16000 and entry["duration"] > 2
        d[lang] = entry
    return {"status": "ok" if made.get("en") else "fail",
            "summary": f"voices={len(voices)}, pl_PL={d['pl_voices'] or 'none'}, EN wav={d.get('en', {}).get('duration')} s, "
                       f"PL wav={d.get('pl', {}).get('duration')} s",
            "details": d}


def _hits(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if w in low]


def check_whisper(out: Path) -> dict:
    import numpy as np
    import soundfile as sf
    from huggingface_hub import hf_hub_download

    d: dict = {"revision": WHISPER_REVISION, "models": {}}
    audio = {}
    for lang in ("en", "pl"):
        path = out / f"tts_{lang}.wav"
        if path.exists():
            data, rate = sf.read(str(path), dtype="float32")
            audio[lang] = np.ascontiguousarray(data if data.ndim == 1 else data.mean(axis=1), dtype=np.float32)
            d[f"audio_{lang}"] = {"rate": rate, "seconds": round(len(data) / rate, 2)}
    if "en" not in audio:
        return {"status": "fail", "summary": "no EN WAV from the tts check", "details": d}
    from pywhispercpp.model import Model

    for filename in WHISPER_FILES:
        m: dict = {}
        d["models"][filename] = m
        start = time.monotonic()
        try:
            path = hf_hub_download(WHISPER_REPO, filename, revision=WHISPER_REVISION)
        except BaseException as error:
            m["download_error"] = f"{type(error).__name__}: {error}"
            continue
        m["download_seconds"] = round(time.monotonic() - start, 2)
        m["mb"] = round(Path(path).stat().st_size / 2**20, 1)
        m["path"] = path
        log = out / f"whisper_{filename}.log"
        start = time.monotonic()
        model = Model(path, redirect_whispercpp_logs_to=str(log), n_threads=os.cpu_count() or 3,
                      print_progress=False, print_realtime=False)
        m["load_seconds"] = round(time.monotonic() - start, 2)
        for lang, words in (("en", KEYWORDS_EN), ("pl", KEYWORDS_PL)):
            if lang not in audio:
                continue
            r: dict = {}
            m[lang] = r

            def detect(samples=audio[lang], engine=model):
                found = engine.auto_detect_language(samples)
                top = found[0]
                probs = sorted(found[1].items(), key=lambda kv: -float(kv[1]))[:5]
                return {"type": type(found).__name__, "top": [top[0], round(float(top[1]), 3)],
                        "top5": [[k, round(float(v), 3)] for k, v in probs]}

            guard(r, "auto_detect", detect)
            start = time.monotonic()
            try:
                segments = model.transcribe(audio[lang], language=lang)
                r["seconds"] = round(time.monotonic() - start, 2)
                text = " ".join(s.text.strip() for s in segments)
                r["text"] = text
                r["segments"] = [[s.t0, s.t1] for s in segments][:5]
                r["hits"] = _hits(text, words)
            except BaseException as error:
                r["error"] = f"{type(error).__name__}: {error}"
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            m["log_backend_lines"] = [ln for ln in lines if re.search(r"metal|gpu|backend|system_info|BLAS|CPU",
                                                                       ln, re.I)][:25]
        del model
    base = d["models"].get("ggml-base-q5_1.bin", {})
    en_hits = len((base.get("en") or {}).get("hits", []))
    if base.get("path"):
        (out / "model_path.txt").write_text(base["path"], encoding="utf-8")
    tiny = d["models"].get("ggml-tiny-q5_1.bin", {})
    return {"status": "ok" if en_hits >= 3 else "fail",
            "summary": f"base: load {base.get('load_seconds')} s, EN {(base.get('en') or {}).get('seconds')} s "
                       f"hits {en_hits}/{len(KEYWORDS_EN)}, PL hits {len((base.get('pl') or {}).get('hits', []))}/"
                       f"{len(KEYWORDS_PL)}; tiny EN hits {len((tiny.get('en') or {}).get('hits', []))}",
            "details": d}


def check_tap(out: Path) -> dict:
    import ctypes

    import numpy as np

    d: dict = {"permissions_before": permission_snapshot()}
    guard(d, "AudioHardwareCreateProcessTap_symbol", lambda: hasattr(
        ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio"), "AudioHardwareCreateProcessTap"))

    def lookup():
        import objc
        return str(objc.lookUpClass("CATapDescription"))

    guard(d, "CATapDescription_class", lookup)
    import catap

    d["catap_version"] = catap.__version__
    guard(d, "bundle_id_taps_supported", lambda: bool(catap.bundle_id_taps_supported()))
    guard(d, "audio_processes", lambda: [str(getattr(p, "name", p)) for p in catap.list_audio_processes()][:20])
    guard(d, "audio_devices", lambda: [str(getattr(x, "name", x)) for x in catap.list_audio_devices()][:20])
    guard(d, "own_process", lambda: str(catap.find_process_by_pid(os.getpid())))
    native = Path(catap.__file__).parent / "native" / "libcatap_coreaudio.dylib"
    d["native_lib"] = {"exists": native.exists(), "kb": round(native.stat().st_size / 1024) if native.exists() else None}

    stats = {"buffers": 0, "frames": 0, "nonzero": 0, "peak": 0.0, "format": None}

    def on_buffer(buffer):
        stats["buffers"] += 1
        stats["frames"] += buffer.frame_count
        fmt = buffer.format
        stats["format"] = f"{fmt.sample_rate} Hz, {fmt.num_channels} ch, {fmt.bits_per_sample} bit, {fmt.sample_type}"
        is_float = "float" in str(fmt.sample_type).lower()
        dtype = np.float32 if is_float and fmt.bits_per_sample == 32 else np.int16 if fmt.bits_per_sample == 16 else None
        if dtype is not None:
            samples = np.frombuffer(buffer.data, dtype=dtype)
            stats["nonzero"] += int(np.count_nonzero(samples))
            if samples.size:
                stats["peak"] = max(stats["peak"], float(np.abs(samples.astype(np.float64)).max()))

    session = None
    try:
        session = catap.RecordingSession.from_system_audio(on_buffer=on_buffer)
        start = time.monotonic()
        session.start()
        d["start_seconds"] = round(time.monotonic() - start, 2)
        wav = out / "tts_en.wav"
        if wav.exists():
            d["afplay"] = run_cmd(["afplay", str(wav)], timeout=20)
        else:
            time.sleep(3)
        time.sleep(0.5)
        d["capture_failed"] = bool(session.capture_failed)
        session.stop()
        d["captured_only_silence"] = guard(d, "captured_only_silence", lambda: bool(session.captured_only_silence))
        d["frames_recorded"] = guard(d, "frames_recorded", lambda: int(session.frames_recorded))
        d["result"] = "started"
    except BaseException as error:
        d["result"] = f"{type(error).__name__}: {error}"
        d["traceback"] = traceback.format_exc()[-2000:]
        if session is not None:
            with contextlib.suppress(BaseException):
                session.stop()
    d["stats"] = stats
    d["permissions_after"] = permission_snapshot()
    return {"status": "info",
            "summary": f"start: {d['result']}; buffers={stats['buffers']}, frames={stats['frames']}, "
                       f"nonzero={stats['nonzero']}, peak={round(stats['peak'], 4)}, format={stats['format']}",
            "details": d}


def check_microphone(out: Path) -> dict:
    import numpy as np
    import sounddevice as sd

    d: dict = {}
    try:
        info = sd.query_devices(kind="input")
        d["default_input"] = info["name"]
    except BaseException as error:
        d["default_input"] = f"{type(error).__name__}: {error}"
        return {"status": "info", "summary": f"no input device: {d['default_input']}", "details": d}
    chunks: list[bytes] = []

    def callback(indata, frames, _time, status):
        chunks.append(bytes(indata))

    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1600, callback=callback):
            time.sleep(1.5)
        data = np.frombuffer(b"".join(chunks), dtype=np.int16)
        d.update(blocks=len(chunks), samples=int(data.size), nonzero=int(np.count_nonzero(data)))
    except BaseException as error:
        d["error"] = f"{type(error).__name__}: {error}"
    d["permissions_after"] = permission_snapshot()
    return {"status": "info", "summary": f"input={d['default_input']}, samples={d.get('samples')}, "
                                         f"nonzero={d.get('nonzero')}, error={d.get('error')}", "details": d}


def check_pyinstaller(out: Path) -> dict:
    d: dict = {}
    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "pyi"
    shutil.rmtree(temp, ignore_errors=True)
    build = run_cmd([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "WhisperProbe",
                     "--osx-bundle-identifier", "app.ucho.spike", "--distpath", str(temp / "dist"),
                     "--workpath", str(temp / "build"), "--specpath", str(temp), str(HERE / "pyi_probe.py")],
                    timeout=900, tail=6000)
    (out / "pyinstaller-build.log").write_text(build.get("err", "") + build.get("out", ""), encoding="utf-8")
    d["build"] = {"rc": build.get("rc"), "seconds": build.get("seconds")}
    app = temp / "dist" / "WhisperProbe.app"
    if not app.exists():
        return {"status": "fail", "summary": f"build failed rc={build.get('rc')}", "details": d}
    d["app_mb"] = round(sum(p.stat().st_size for p in app.rglob("*") if p.is_file() and not p.is_symlink()) / 2**20, 1)
    d["dylibs"] = sorted(str(p.relative_to(app)) for p in app.rglob("*.dylib"))[:80]
    d["codesign_verify"] = run_cmd(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    d["codesign_display"] = run_cmd(["codesign", "-dv", str(app)]).get("err", "")[-800:]
    d["spctl"] = run_cmd(["spctl", "-a", "-vv", "-t", "exec", str(app)])
    model_file = out / "model_path.txt"
    if model_file.exists():
        probe_out = out / "pyi_probe.json"
        run = run_cmd([str(app / "Contents" / "MacOS" / "WhisperProbe"), model_file.read_text(encoding="utf-8"),
                       str(out / "tts_en.wav"), str(probe_out)], timeout=300)
        d["run"] = {k: run.get(k) for k in ("rc", "seconds", "err")}
        if probe_out.exists():
            d["probe"] = json.loads(probe_out.read_text(encoding="utf-8"))
    dmg = temp / "WhisperProbe.dmg"
    for attempt in range(3):
        made = run_cmd(["hdiutil", "create", "-volname", "WhisperProbe", "-srcfolder", str(app), "-format", "ULFO",
                        "-ov", str(dmg)], timeout=300)
        d[f"hdiutil_attempt_{attempt + 1}"] = {k: made.get(k) for k in ("rc", "seconds", "err")}
        if made.get("rc") == 0:
            d["dmg_mb"] = round(dmg.stat().st_size / 2**20, 1)
            break
        time.sleep(10)
    probe = d.get("probe", {})
    ok = d["codesign_verify"].get("rc") == 0 and len(_hits(probe.get("text", ""), KEYWORDS_EN)) >= 3
    return {"status": "ok" if ok else "fail",
            "summary": f"build {d['build']['seconds']} s, .app {d['app_mb']} MB, dmg {d.get('dmg_mb')} MB, "
                       f"codesign rc={d['codesign_verify'].get('rc')}, frozen run rc={d.get('run', {}).get('rc')}, "
                       f"text hits={len(_hits(probe.get('text', ''), KEYWORDS_EN))}, tk={probe.get('tk')}",
            "details": d}


CHECKS: dict[str, tuple[Callable[[Path], dict], int]] = {
    "install": (check_install, 30),
    "env": (check_env, 120),
    "imports": (check_imports, 300),
    "tk": (check_tk, 60),
    "ctk": (check_ctk, 60),
    "tray_pystray_default": (check_tray_pystray_default, 60),
    "tray_pystray_main": (check_tray_pystray_main, 60),
    "tray_thread": (check_tray_thread, 40),
    "tray_nsstatusitem": (check_tray_nsstatusitem, 60),
    "hotkey": (check_hotkey, 90),
    "tts": (check_tts, 120),
    "whisper": (check_whisper, 900),
    "microphone": (check_microphone, 60),
    "tap": (check_tap, 120),
    "pyinstaller": (check_pyinstaller, 1500),
}


def run_one(name: str, out: Path) -> None:
    func, _timeout = CHECKS[name]
    start = time.monotonic()
    try:
        result = func(out)
    except BaseException as error:
        result = {"status": "error", "summary": f"{type(error).__name__}: {error}",
                  "details": {"traceback": traceback.format_exc()[-3000:]}}
    result["seconds"] = round(time.monotonic() - start, 2)
    (out / f"{name}.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def run_all(out: Path, only: list[str] | None) -> None:
    for name, (_func, timeout) in CHECKS.items():
        if only and name not in only:
            continue
        log = out / f"{name}.log"
        target = out / f"{name}.json"
        target.unlink(missing_ok=True)
        start = time.monotonic()
        with open(log, "w", encoding="utf-8") as handle:
            try:
                done = subprocess.run([sys.executable, str(Path(__file__)), "run", name, "--out", str(out)],
                                      stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
                code = done.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
        seconds = round(time.monotonic() - start, 2)
        if not target.exists():
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
            target.write_text(json.dumps({"status": "crash", "summary": f"process ended with {code} after {seconds} s",
                                          "seconds": seconds, "details": {"log_tail": tail}}, indent=2),
                              encoding="utf-8")
        status = json.loads(target.read_text(encoding="utf-8"))["status"]
        print(f"{name}: {status} ({seconds} s, exit {code})", flush=True)


def summary(out: Path) -> str:
    runner = os.environ.get("SPIKE_RUNNER", platform.mac_ver()[0])
    lines = [f"## macOS spike (M3a): {runner}", "", "| Check | Status | Time | Result |", "|---|---|---|---|"]
    for name in CHECKS:
        path = out / f"{name}.json"
        if not path.exists():
            lines.append(f"| {name} | missing | | |")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        text = str(data.get("summary", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {data.get('status')} | {data.get('seconds', '')} s | {text} |")
    lines += ["", "Details: artifact `spike-<runner>` (JSON per check, logs, screenshots)."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["all", "run", "summary", "hotkey-holder"])
    parser.add_argument("name", nargs="*")
    parser.add_argument("--out", type=Path, default=Path("spike-out"))
    args = parser.parse_args()
    if args.command == "hotkey-holder":
        hotkey_holder()
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    if args.command == "run":
        run_one(args.name[0], args.out)
    elif args.command == "all":
        run_all(args.out, args.name or None)
    else:
        text = summary(args.out)
        (args.out / "summary.md").write_text(text, encoding="utf-8")
        target = os.environ.get("GITHUB_STEP_SUMMARY")
        if target:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(text)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
