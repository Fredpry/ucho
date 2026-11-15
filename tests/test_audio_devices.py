# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Listing and choosing audio devices, on a fake PortAudio (no real devices are opened)."""
import sys
import types

try:
    import pyaudiowpatch  # noqa: F401
except ImportError:  # the CI test job does not install the audio stack
    stub = types.ModuleType("pyaudiowpatch")
    stub.paWASAPI, stub.paInt16, stub.paContinue = 13, 8, 0
    stub.PyAudio = object
    sys.modules["pyaudiowpatch"] = stub

from ucho import audio, dictation  # noqa: E402

MME, DS, WASAPI = 0, 1, 2
HEADSET = "Zestaw słuchawkowy (Logitech G PRO X 2)"
ARRAY = "Zestaw mikrofonów (2 — Technologia Intel® Smart Sound dla mikrofonów cyfrowych)"

# The device list of a laptop with a built-in microphone array and a USB headset:
# each microphone once per API, MME names cut to 31 characters.
DEVICES = [
    {"name": "Mapowanie dźwięku Microsoft - Input", "hostApi": MME, "maxInputChannels": 2},
    {"name": ARRAY[:31], "hostApi": MME, "maxInputChannels": 2},
    {"name": HEADSET[:31], "hostApi": MME, "maxInputChannels": 2},
    {"name": "Speaker (Realtek(R) Audio)", "hostApi": MME, "maxInputChannels": 0},
    {"name": "Podstawowy sterownik przechwytywania dźwięku", "hostApi": DS, "maxInputChannels": 2},
    {"name": ARRAY, "hostApi": DS, "maxInputChannels": 2},
    {"name": HEADSET, "hostApi": DS, "maxInputChannels": 2},
    {"name": HEADSET, "hostApi": WASAPI, "maxInputChannels": 2, "defaultSampleRate": 48000.0},
    {"name": ARRAY, "hostApi": WASAPI, "maxInputChannels": 4, "defaultSampleRate": 48000.0},
    {"name": "Speaker (Realtek(R) Audio) [Loopback]", "hostApi": WASAPI, "maxInputChannels": 2,
     "isLoopbackDevice": True},
]
WASAPI_DEFAULT_INPUT = 8   # the array


class FakePyAudio:
    def __init__(self, devices=DEVICES, wasapi=True, wasapi_default=WASAPI_DEFAULT_INPUT):
        self.devices = [dict(d, index=i, defaultSampleRate=d.get("defaultSampleRate", 44100.0))
                        for i, d in enumerate(devices)]
        self.wasapi = wasapi
        self.wasapi_default = wasapi_default

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        return self.devices[i]

    def get_host_api_info_by_type(self, kind):
        if not self.wasapi or kind != audio.pyaudio.paWASAPI:
            raise OSError("no such host API")
        return {"index": WASAPI, "name": "Windows WASAPI"}

    def get_host_api_info_by_index(self, api):
        return {"index": api, "defaultInputDevice": self.wasapi_default if api == WASAPI else 1}

    def get_default_host_api_info(self):
        return {"index": MME, "defaultInputDevice": 1}

    def get_default_input_device_info(self):
        return self.devices[1]


def test_each_microphone_is_listed_once_with_its_full_name():
    mics, default, names = audio.microphones(FakePyAudio())
    assert [d.name for d in mics] == [HEADSET, ARRAY]
    assert default.name == ARRAY and default.index == WASAPI_DEFAULT_INPUT
    assert default.rate == 48000 and default.channels == 4
    assert names[2] == HEADSET[:31] and 3 not in names  # outputs are not inputs


def test_without_wasapi_the_default_api_is_listed():
    mics, default, _ = audio.microphones(FakePyAudio(wasapi=False))
    assert [d.name for d in mics] == ["Mapowanie dźwięku Microsoft - Input", ARRAY[:31], HEADSET[:31]]
    assert default.index == 1


def test_no_wasapi_default_falls_back_to_the_portaudio_default():
    _, default, _ = audio.microphones(FakePyAudio(wasapi_default=-1))
    assert default.index == 1


def test_find_device_by_name_survives_shifted_indices():
    mics, default, _ = audio.microphones(FakePyAudio())
    # Another device was plugged in: every index moved, the name did not.
    shifted, shifted_default, _ = audio.microphones(
        FakePyAudio([{"name": "USB Mic", "hostApi": MME, "maxInputChannels": 1}] + DEVICES,
                    wasapi_default=WASAPI_DEFAULT_INPUT + 1))
    chosen = audio.find_device(HEADSET, shifted, shifted_default)
    assert chosen.name == HEADSET and chosen.index != audio.find_device(HEADSET, mics, default).index


def test_unknown_or_missing_choice_gives_the_default():
    mics, default, _ = audio.microphones(FakePyAudio())
    assert audio.find_device(None, mics, default) is default
    assert audio.find_device("Bluetooth headset", mics, default) is default


def test_legacy_index_of_a_cut_mme_name_finds_the_full_device():
    mics, default, names = audio.microphones(FakePyAudio())
    chosen = audio.find_device(None, mics, default, legacy_index=2, names_by_index=names)
    assert chosen.name == HEADSET and chosen.index == 7


def test_legacy_index_of_a_directsound_entry_and_of_the_mapper():
    mics, default, names = audio.microphones(FakePyAudio())
    assert audio.find_device(None, mics, default, 6, names).name == HEADSET
    assert audio.find_device(None, mics, default, 0, names) is default   # Sound Mapper
    assert audio.find_device(None, mics, default, 4, names) is default   # Primary Capture Driver
    assert audio.find_device(None, mics, default, 99, names) is default  # gone


def test_short_names_are_not_matched_by_prefix():
    mics, default, _ = audio.microphones(FakePyAudio())
    assert audio.find_device("Zestaw", mics, default) is default


def test_ambiguous_cut_name_gives_the_default():
    twins = [audio.DeviceInfo(1, "Mikrofon (USB Audio Device) nr 1", 1, 48000, False),
             audio.DeviceInfo(2, "Mikrofon (USB Audio Device) nr 2", 1, 48000, False)]
    assert audio.find_device("Mikrofon (USB Audio Device) nr ", twins, None) is None


def _devices():
    mics, default, names = audio.microphones(FakePyAudio())
    loop = audio.DeviceInfo(9, "Speaker (Realtek(R) Audio) [Loopback]", 2, 48000, True)
    return {"microphones": mics, "system": [loop], "default_microphone": default,
            "default_system": loop, "names_by_index": names}


def test_migration_turns_saved_indices_into_names():
    settings = {"microphone_name": None, "microphone_index": 2,
                "system_name": None, "system_index": 9}
    assert audio.migrate_device_choices(settings, _devices()) is True
    assert settings == {"microphone_name": HEADSET, "microphone_index": None,
                        "system_name": "Speaker (Realtek(R) Audio) [Loopback]", "system_index": None}
    assert audio.migrate_device_choices(settings, _devices()) is False


def test_migration_of_the_mapper_means_default_and_keeps_a_name():
    settings = {"microphone_name": None, "microphone_index": 0,
                "system_name": "Headphones [Loopback]", "system_index": 3}
    assert audio.migrate_device_choices(settings, _devices()) is True
    assert settings["microphone_name"] is None and settings["microphone_index"] is None
    assert settings["system_name"] == "Headphones [Loopback]" and settings["system_index"] is None


def test_migration_waits_for_a_real_device_list():
    settings = {"microphone_name": None, "microphone_index": 2,
                "system_name": None, "system_index": None}
    failed = {"microphones": [], "system": [], "error": "PortAudio failed"}
    assert audio.migrate_device_choices(settings, failed) is False
    assert settings["microphone_index"] == 2


def test_recorder_picks_by_name_then_legacy_index():
    devices = _devices()
    assert audio.Recorder(microphone=HEADSET)._pick("microphone", devices).index == 7
    assert audio.Recorder(microphone_index=2)._pick("microphone", devices).index == 7
    assert audio.Recorder()._pick("microphone", devices).name == ARRAY
    assert audio.Recorder(system="nope")._pick("system", devices).index == 9


def test_dictation_microphone_reports_a_missing_choice():
    p = FakePyAudio()
    device, found = dictation.Microphone(HEADSET)._device(p)
    assert (device.index, found) == (7, True)
    device, found = dictation.Microphone(None)._device(p)
    assert (device.name, found) == (ARRAY, True)
    device, found = dictation.Microphone("Bluetooth headset")._device(p)
    assert (device.name, found) == (ARRAY, False)
    device, found = dictation.Microphone(None, legacy_index=2)._device(p)
    assert (device.index, found) == (7, True)


def test_dictation_rereads_devices_when_the_choice_is_missing(monkeypatch):
    """A microphone plugged in after PortAudio started is found by a fresh PortAudio."""
    stale = FakePyAudio([d for d in DEVICES if d["name"] != HEADSET])
    fresh = FakePyAudio()
    opened = []

    class Stream:
        def start_stream(self):
            pass

    def make():
        return fresh

    def open_(**kwargs):
        opened.append(kwargs["input_device_index"])
        return Stream()

    fresh.open = open_
    monkeypatch.setattr(audio, "new_pyaudio", make)
    monkeypatch.setattr(audio, "close_pyaudio", lambda p: None)
    mic = dictation.Microphone(HEADSET)
    mic._pa = stale
    mic.start()
    assert opened == [7] and mic.name == HEADSET


def _start_with(monkeypatch, mic, stale, fresh):
    """Starts `mic` whose PortAudio is `stale`, while a new PortAudio would be `fresh`."""
    opened = []

    class Stream:
        def start_stream(self):
            pass

        stop_stream = close = start_stream

    def open_(**kwargs):
        opened.append(kwargs["input_device_index"])
        return Stream()

    fresh.open = open_
    monkeypatch.setattr(audio, "new_pyaudio", lambda: fresh)
    monkeypatch.setattr(audio, "close_pyaudio", lambda p: None)
    mic._pa = stale
    mic.start()
    return opened


def test_dictation_follows_a_default_changed_in_windows(monkeypatch):
    """No microphone chosen: the default of a fresh PortAudio, not of the one from startup."""
    opened = _start_with(monkeypatch, dictation.Microphone(None),
                         FakePyAudio(), FakePyAudio(wasapi_default=7))
    assert opened == [7]


def test_missing_choice_is_logged_once(monkeypatch, caplog):
    mic = dictation.Microphone("Bluetooth headset")
    for _ in range(3):
        _start_with(monkeypatch, mic, FakePyAudio(), FakePyAudio())
        mic.stop()
    assert sum("is not connected" in r.getMessage() for r in caplog.records) == 1


def test_migration_keeps_an_index_when_its_list_is_empty():
    devices = _devices()
    devices["system"] = []   # listing loopbacks failed
    settings = {"microphone_name": None, "microphone_index": None,
                "system_name": None, "system_index": 9}
    assert audio.migrate_device_choices(settings, devices) is False
    assert settings["system_index"] == 9


def test_menu_lists_default_devices_and_a_missing_choice():
    mics = _devices()["microphones"]
    labels, selected = audio.menu_choices(mics, None)
    assert list(labels.values()) == [None, HEADSET, ARRAY] and labels[selected] is None
    labels, selected = audio.menu_choices(mics, HEADSET)
    assert selected == HEADSET and labels[selected] == HEADSET
    labels, selected = audio.menu_choices(mics, "Bluetooth headset")
    assert "Bluetooth headset" in selected and labels[selected] == "Bluetooth headset"
    assert len(labels) == 4


def test_menu_shows_a_legacy_index_as_its_device():
    devices = _devices()
    labels, selected = audio.menu_choices(devices["microphones"], None, 2, devices["names_by_index"])
    assert labels[selected] == HEADSET
