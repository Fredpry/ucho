# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Settings screen: appearance and windows, hardware, transcription, dictation, audio, summary.

Every change is saved to settings.json at once (the transcription re-reads
the file before it starts, see meeting.py).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import customtkinter as ctk

from . import config, hardware, i18n, retention, theme, transcribe, ui, winshell
from .hotkeys import label as hotkey_label
from .i18n import t
from .screens import PAD, Screen, header, open_path
from .theme import C

log = logging.getLogger(__name__)

THEME_LABELS = config.THEME_LABELS
LAYOUT_LABELS = config.LAYOUT_LABELS
MODE_LABELS = config.DICTATION_MODE_LABELS


def model_label(repo: str) -> str:
    info = config.MODELS.get(repo, {})
    return f"{info.get('name', repo.split('/')[-1])}  ({info.get('size', '?')})"


def short_model(repo: str) -> str:
    return repo.split("/")[-1].removeprefix("whisper-").removesuffix("-ov")


class Section(ctk.CTkFrame):
    """A card with a title and rows of label + control."""

    def __init__(self, master, title: str, description: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        ui.label(self, title, theme.LG, "bold", anchor="w").grid(row=0, column=0, sticky="ew")
        if description:
            ui.muted(self, description, anchor="w", justify="left", wraplength=640).grid(
                row=1, column=0, sticky="ew", pady=(2, 0))
        self.body = ui.card(self)
        self.body.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.body.grid_columnconfigure(1, weight=1)
        self._row = 0

    def row(self, label: str, widget_factory, hint: str | None = None):
        """Adds "label | control" and returns the control."""
        pad_top = 14 if self._row == 0 else 6
        ui.label(self.body, label, theme.SM - 1, anchor="w", width=210, justify="left",
                 wraplength=200).grid(row=self._row, column=0, sticky="nw", padx=(18, 12),
                                      pady=(pad_top + 6, 6))
        holder = ctk.CTkFrame(self.body, fg_color="transparent")
        holder.grid(row=self._row, column=1, sticky="ew", padx=(0, 18), pady=(pad_top, 6))
        widget = widget_factory(holder)
        widget.pack(anchor="w", fill="x" if isinstance(widget, ctk.CTkEntry) else None)
        if hint:
            ui.muted(holder, hint, theme.XS, anchor="w", justify="left", wraplength=440).pack(
                anchor="w", pady=(4, 0))
        self._row += 1
        return widget

    def full(self, widget_factory, pady=(6, 6)):
        """A control over the full width of the card."""
        holder = ctk.CTkFrame(self.body, fg_color="transparent")
        holder.grid(row=self._row, column=0, columnspan=2, sticky="ew", padx=18, pady=pady)
        widget_factory(holder)
        self._row += 1
        return holder

    def end(self) -> None:
        ctk.CTkFrame(self.body, fg_color="transparent", height=10).grid(row=self._row, column=0)
        self._row += 1


class SettingsScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.settings = app.settings
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        top, _slot = header(self, t("Settings"))
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD - 4, 10))
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=(PAD - 8, 8), pady=(0, 8))
        self.scroll.grid_columnconfigure(0, weight=1)
        self._sections = 0
        self._build_appearance()
        self._build_hardware()
        self._build_transcription()
        self._build_dictation()
        self._build_audio()
        self._build_summary()
        self._build_storage()
        self._build_about()
        self._profile_loaded = False

    def _section(self, title: str, description: str | None = None) -> Section:
        section = Section(self.scroll, title, description)
        section.grid(row=self._sections, column=0, sticky="ew", padx=(8, 12), pady=(8, 18))
        self._sections += 1
        return section

    def _save(self, key: str, value) -> None:
        self.settings[key] = value
        self.settings.save()

    # --- appearance and windows ----------------------------------------------------

    def _build_appearance(self) -> None:
        s = self._section(t("Appearance and windows"))
        themes = {t(v): k for k, v in THEME_LABELS.items()}
        s.row(t("Theme"), lambda m: ui.segmented(
            m, list(themes), t(THEME_LABELS.get(self.settings["theme"], "Same as Windows")),
            lambda v: self.app.set_theme(themes[v])))
        layouts = {t(v): k for k, v in LAYOUT_LABELS.items()}
        s.row(t("Window layout"), lambda m: ui.option_menu(
            m, list(layouts), t(LAYOUT_LABELS.get(self.settings["layout"], LAYOUT_LABELS["single"])),
            lambda v: self.app.set_layout(layouts[v]), width=320),
            t("One window switches between Meeting and Dictation. With separate windows Ucho lives "
              "in the tray and dictation has its own small window."))
        languages = {t(v): k for k, v in config.UI_LANGUAGE_LABELS.items()}
        s.row(t("Interface language"), lambda m: ui.option_menu(
            m, list(languages), t(config.UI_LANGUAGE_LABELS.get(self.settings["ui_language"],
                                                                 config.UI_LANGUAGE_LABELS["auto"])),
            lambda v: self.app.set_ui_language(languages[v])))
        s.row(t("Start with Windows"), lambda m: ui.switch(
            m, t("Start Ucho in the tray when I sign in"), self.settings["start_with_windows"],
            self.app.set_autostart))
        s.row(t("Closing the window"), lambda m: ui.switch(
            m, t("Keep Ucho running in the tray"), self.settings["tray"],
            lambda v: self._save("tray", v)),
            t("The shortcuts work only while Ucho runs."))
        s.end()

    # --- hardware ---------------------------------------------------------------------

    def _build_hardware(self) -> None:
        s = self._section(t("Hardware"), t("Ucho picks the model and the device from your hardware. "
                                           "Nothing here uses the network."))
        self.hw_profile = s.full(lambda m: None)
        self.hw_profile_text = ui.label(self.hw_profile, t("Reading the hardware profile…"),
                                        theme.SM - 1, anchor="w", justify="left", wraplength=620)
        self.hw_profile_text.pack(anchor="w", fill="x", pady=(8, 0))
        self.hw_rec = s.full(lambda m: None, pady=(4, 4))
        self.hw_rec_title = ui.label(self.hw_rec, "", theme.SM, "bold", anchor="w")
        self.hw_rec_title.pack(anchor="w", fill="x")
        self.hw_reasons = ui.muted(self.hw_rec, "", theme.XS + 1, anchor="w", justify="left",
                                   wraplength=620)
        self.hw_reasons.pack(anchor="w", fill="x", pady=(2, 0))
        self.hw_warnings = ui.label(self.hw_rec, "", theme.XS + 1, color=C.warning, anchor="w",
                                    justify="left", wraplength=620)

        def buttons(master):
            self.hw_apply = ui.primary(master, t("Use the recommendation"), self._apply_recommendation,
                                       "check", height=34, width=10)
            self.hw_measure = ui.secondary(master, t("Measure again"), self._measure, "speed", width=10)
            self.hw_measure.pack(side="left", padx=(0, 8))
            self.hw_result = ui.muted(master, "", theme.XS + 1, anchor="w", justify="left",
                                      wraplength=360)
            self.hw_result.pack(side="left", padx=(4, 0))
        self.hw_buttons = s.full(buttons, pady=(4, 10))

        devices = list(config.DEVICES)
        current = self.settings["device"].split(".")[0]
        self.device = s.row(t("Device"), lambda m: ui.segmented(
            m, devices, current if current in devices else "GPU", self._device),
            t("NPU is experimental: the first start compiles the model for about 3 minutes. "
              "AUTO lets OpenVINO choose."))
        model_labels = {model_label(k): k for k in transcribe.offered_models()}
        self.model_labels = model_labels
        def model_row(master):
            box = ctk.CTkFrame(master, fg_color="transparent")
            self.model = ui.option_menu(box, list(model_labels), model_label(self.settings["model"]),
                                        self._model, width=340)
            self.model.pack(anchor="w")
            status = ctk.CTkFrame(box, fg_color="transparent")
            status.pack(anchor="w", pady=(6, 0))
            self.model_state = ui.muted(status, "", theme.XS + 1, anchor="w")
            self.model_state.pack(side="left")
            self.download = ui.secondary(status, t("Download model"), self._download, width=10,
                                         height=30)
            return box
        s.row(t("Transcription model"), model_row)
        s.end()

    def on_show(self, **kwargs) -> None:
        self._refresh_model()
        if not self._profile_loaded:
            self._profile_loaded = True
            self.app.load_hardware(lambda: self._show_hardware())
        else:
            self._show_hardware()
        if kwargs.get("section") == "hardware":
            self.scroll._parent_canvas.yview_moveto(0.12)  # noqa: SLF001

    def _show_hardware(self) -> None:
        p, rec = self.app.hardware_profile, self.app.hardware_rec
        if p is None or rec is None:
            return
        gpu = p.usable_gpu()
        npu = p.npu()
        lines = [
            t("Processor: {cpu} ({cores} cores{avx})", cpu=p.cpu_name or "?", cores=p.cores,
              avx=", AVX-512" if p.avx512 else ""),
            t("Memory: {ram} GB", ram=f"{p.ram_gb:.0f}"),
            t("Graphics for Whisper: {gpu}", gpu=(gpu.name or gpu.id) if gpu else t("none usable")),
        ]
        if npu is not None:
            lines.append(t("NPU: {npu}", npu=npu.name or npu.id))
        lines.append(t("OpenVINO devices: {list}", list=", ".join(d.id for d in p.devices) or "-"))
        self.hw_profile_text.configure(text="\n".join(lines))
        dictation = t("same model") if rec.dictation_model == rec.model else short_model(rec.dictation_model)
        self.hw_rec_title.configure(text=t("Recommended: {model} on {device}, dictation: {dictation}",
                                           model=short_model(rec.model), device=rec.device,
                                           dictation=dictation))
        self.hw_reasons.configure(text="\n".join(f"•  {r}" for r in rec.reasons))
        self.hw_warnings.configure(text="\n".join(f"!  {w}" for w in rec.warnings))
        if rec.warnings:
            self.hw_warnings.pack(anchor="w", fill="x", pady=(4, 0))
        else:
            self.hw_warnings.pack_forget()
        matches = (self.settings["model"], self.settings["device"]) == (rec.model, rec.device)
        self.hw_apply.pack_forget()
        if not matches:
            self.hw_apply.pack(side="left", padx=(0, 8), before=self.hw_measure)
        measured = (self.settings["hardware_profile"] or {}).get("benchmark") if isinstance(
            self.settings["hardware_profile"], dict) else None
        if measured and not self.hw_result.cget("text"):
            self.hw_result.configure(text=t("Last measurement: {x}× faster than real time on {device}.",
                                            x=measured.get("speed"), device=measured.get("device")))

    def _apply_recommendation(self) -> None:
        rec = self.app.hardware_rec
        if rec is None:
            return
        hardware.save(self.settings, self.app.hardware_profile, rec)
        self.device.set(rec.device.split(".")[0])
        self.model.set(model_label(rec.model))
        self.app.settings_changed()
        self._refresh_model()
        self._show_hardware()

    def _measure(self) -> None:
        rec = self.app.hardware_rec
        if rec is None:
            return
        if not transcribe.model_downloaded(rec.model):
            self.hw_result.configure(text=t("Measurement skipped: model {model} is not downloaded.",
                                            model=short_model(rec.model)))
            return
        self.hw_measure.configure(state="disabled")
        self.hw_result.configure(text=t("Measuring {model} on {device}…", model=short_model(rec.model),
                                        device=rec.device))

        def done(result) -> None:
            measured, new_rec = result
            self.app.hardware_rec = new_rec
            hardware.save(self.settings, self.app.hardware_profile, new_rec, measured, keep_choice=True)
            self.hw_measure.configure(state="normal")
            self.hw_result.configure(text=t("Measured: {x}× faster than real time on {device}.",
                                            x=f"{measured.speed:.1f}", device=measured.device))
            self.app.settings_changed()
            self._show_hardware()

        def failed(error) -> None:
            self.hw_measure.configure(state="normal")
            self.hw_result.configure(text=t("Measurement failed: {error}", error=error))

        self.app.run_benchmark(rec, done, failed)

    def _device(self, value: str) -> None:
        if value == "NPU" and not ui.confirm(
                self, t("Use the NPU?"),
                t("The NPU is experimental in Ucho. The first transcription compiles the model "
                  "for the NPU, which took over 3 minutes on a Core Ultra 7. Later starts are fast."),
                t("Use the NPU")):
            self.device.set(self.settings["device"].split(".")[0])
            return
        rec = self.app.hardware_rec
        if value == "GPU" and rec is not None and rec.device.startswith("GPU"):
            value = rec.device  # GPU.0 / GPU.1 as recommended
        self._save("device", value)
        self.app.settings_changed()

    def _model(self, value: str) -> None:
        self._save("model", self.model_labels.get(value, config.DEFAULT_MODEL))
        self._refresh_model()
        self.app.settings_changed()

    def _refresh_model(self) -> None:
        model = self.settings["model"]
        size = config.MODELS.get(model, {}).get("size", "")
        self.download.pack_forget()
        # Parakeet (optional backend) ignores the device setting.
        cpu_only = config.MODELS.get(model, {}).get("backend") == "parakeet"
        note = " " + t("Runs on the CPU only.") if cpu_only else ""
        if transcribe.model_downloaded(model):
            self.model_state.configure(text=t("Model downloaded ({size})", size=size) + note,
                                       text_color=C.success)
        else:
            self.model_state.configure(text=t("Model not downloaded ({size})", size=size) + note,
                                       text_color=C.warning)
            self.download.configure(text=t("Download model ({size})", size=size), state="normal")
            self.download.pack(side="left", padx=(12, 0))

    def _download(self) -> None:
        model = self.settings["model"]
        self.download.configure(state="disabled", text=t("Downloading..."))

        def job():
            try:
                transcribe.model_path(model, download=True)
            except Exception as error:
                message = str(error)
                log.exception("Model download failed")
                self.app.post(lambda: self.app.toast(t("Model download failed: {error}", error=message),
                                                     "warning"))
            self.app.post(self._refresh_model)
            self.app.post(self.app.settings_changed)

        threading.Thread(target=job, daemon=True).start()

    # --- transcription ----------------------------------------------------------------

    def _build_transcription(self) -> None:
        s = self._section(t("Transcription"))
        s.row(t("After stopping"), lambda m: ui.switch(
            m, t("Transcribe automatically after stopping"), self.settings["transcribe_after_stop"],
            lambda v: self._save("transcribe_after_stop", v)))
        s.end()

    # --- dictation --------------------------------------------------------------------

    def _build_dictation(self) -> None:
        s = self._section(t("Dictation"), t("{shortcut} starts and ends a dictation, Esc cancels it. "
                                            "The text goes where the cursor is.",
                                            shortcut=hotkey_label(self.settings["dictation_hotkey"])))
        options = {t("Same as transcription (one model in memory)"): config.DICTATION_SHARED}
        options.update({model_label(k): k for k in transcribe.offered_models()})
        current = self.settings["dictation_model"]
        if current == self.settings["model"] or current == config.DICTATION_SHARED:
            current_label = next(iter(options))
        else:
            current_label = model_label(current) if current in config.MODELS else next(iter(options))
        self.dictation_options = options
        s.row(t("Dictation model"), lambda m: ui.option_menu(
            m, list(options), current_label, self._dictation_model, width=340),
            t("A separate FP16 model is a little more precise for short dictations but needs "
              "about 3 GB more memory."))
        s.row(t("Model in memory"), lambda m: ui.switch(
            m, t("Keep the dictation model loaded"), self.settings["dictation_preload"],
            self._preload), t("Off: the model loads at the first dictation and frees memory "
                              "after two idle minutes."))
        modes = {t(v): k for k, v in MODE_LABELS.items()}
        s.row(t("Insert text by"), lambda m: ui.option_menu(
            m, list(modes), t(MODE_LABELS.get(self.settings["dictation_mode"], MODE_LABELS["paste"])),
            lambda v: self._save("dictation_mode", modes[v]), width=320))
        s.row(t("Sounds"), lambda m: ui.switch(
            m, t("Short beeps at start and end"), self.settings["dictation_sounds"],
            lambda v: self._save("dictation_sounds", v)))
        s.end()

    def _dictation_model(self, value: str) -> None:
        self._save("dictation_model", self.dictation_options.get(value, config.DICTATION_SHARED))
        self.app.settings_changed()

    def _preload(self, value: bool) -> None:
        self._save("dictation_preload", value)
        self.app.settings_changed()

    # --- audio --------------------------------------------------------------------------

    def _build_audio(self) -> None:
        s = self._section(t("Audio devices"))
        self.microphone = s.row(t("Microphone"), lambda m: ui.option_menu(
            m, [t("(default)")], t("(default)"), lambda _v: self._save_device("microphone"), width=340))
        self.system = s.row(t("System audio (loopback)"), lambda m: ui.option_menu(
            m, [t("(default)")], t("(default)"), lambda _v: self._save_device("system"), width=340))
        tracks = {t(v): k for k, v in config.TRACK_LABELS.items()}
        s.row(t("What to record"), lambda m: ui.option_menu(
            m, list(tracks), t(config.TRACK_LABELS.get(self.settings["tracks"], "Microphone + system audio")),
            lambda v: self._save("tracks", tracks[v]), width=340))
        self.audio_note = s.full(lambda m: None, pady=(0, 6))
        self.audio_label = ui.muted(self.audio_note, t("Looking for audio devices…"), theme.XS + 1,
                                    anchor="w")
        self.audio_label.pack(anchor="w")
        s.end()
        self.app.on_devices(self._fill_devices)

    def _fill_devices(self, devices: dict) -> None:
        from . import audio

        self._device_labels = {}
        for key, menu, list_key in (("microphone", self.microphone, "microphones"),
                                    ("system", self.system, "system")):
            labels, selected = audio.menu_choices(
                devices.get(list_key, []), self.settings[f"{key}_name"],
                self.settings[f"{key}_index"], devices.get("names_by_index"))
            menu.configure(values=list(labels))
            menu.set(selected)
            if not devices.get("error"):
                # After a failed listing the menu shows only "(default)": choosing it
                # must not erase a choice we could not show.
                self._device_labels[key] = labels
        if devices.get("error"):
            self.audio_label.configure(text=t("Audio device error: {error}", error=devices["error"]),
                                       text_color=C.warning)
        elif not devices.get("microphones") and not devices.get("system"):
            self.audio_label.configure(text=t("No audio devices found."), text_color=C.warning)
        elif not devices.get("system"):
            self.audio_label.configure(text=t("Warning: no loopback device — system audio unavailable"),
                                       text_color=C.warning)
        else:
            self.audio_label.configure(text=t("{n} microphones, {m} outputs for system audio.",
                                              n=len(devices["microphones"]), m=len(devices["system"])),
                                       text_color=C.text_muted)

    def _save_device(self, key: str) -> None:
        """Saves the menu the user changed ("microphone" or "system") by device name."""
        labels = getattr(self, "_device_labels", {}).get(key)
        if labels is None:  # the device list has not arrived, or listing failed
            return
        menu = self.microphone if key == "microphone" else self.system
        self.settings[f"{key}_name"] = labels.get(menu.get())
        # A name replaces the index saved by an earlier version.
        self.settings[f"{key}_index"] = None
        self.settings.save()

    # --- summary ------------------------------------------------------------------------

    def _build_summary(self) -> None:
        s = self._section(t("AI summary (optional)"),
                          t("The model starts only after transcription and is released right after it. "
                            "Runs locally, offline."))
        s.row(t("Summary"), lambda m: ui.switch(
            m, t("Summarize after transcription"), self.settings["summary_enabled"],
            lambda v: self._save("summary_enabled", v)))
        s.row(t("Folder name"), lambda m: ui.switch(
            m, t("Name the folder after the topic and names"), self.settings["rename_folder"],
            lambda v: self._save("rename_folder", v)))
        llm = {f"{k.split('/')[-1]}  ({v['size']})": k for k, v in config.LLM_MODELS.items()}
        current = next((label for label, k in llm.items() if k == self.settings["summary_model"]),
                       next(iter(llm)))
        self.llm_labels = llm
        s.row(t("Language model"), lambda m: ui.option_menu(
            m, list(llm), current, self._llm, width=340))
        self.llm_note = s.full(lambda m: None, pady=(0, 8))
        self.llm_state = ui.muted(self.llm_note, "", theme.XS + 1, anchor="w")
        self.llm_state.pack(side="left")
        self.llm_download = ui.secondary(self.llm_note, t("Download model"), self._download_llm,
                                         width=10, height=30)
        s.end()
        self.after(400, self._refresh_llm)

    def _llm(self, value: str) -> None:
        self._save("summary_model", self.llm_labels.get(value, config.DEFAULT_LLM_MODEL))
        self._refresh_llm()

    def _refresh_llm(self) -> None:
        model = self.settings["summary_model"]
        size = config.LLM_MODELS.get(model, {}).get("size", "")
        self.llm_download.pack_forget()
        if transcribe.model_downloaded(model):
            self.llm_state.configure(text=t("Model downloaded ({size})", size=size), text_color=C.success)
        else:
            self.llm_state.configure(text=t("Not downloaded ({size}). Transcription works without it.",
                                            size=size), text_color=C.text_muted)
            self.llm_download.configure(text=t("Download model ({size})", size=size), state="normal")
            self.llm_download.pack(side="left", padx=(12, 0))

    def _download_llm(self) -> None:
        model = self.settings["summary_model"]
        self.llm_download.configure(state="disabled", text=t("Downloading..."))

        def job():
            try:
                transcribe.model_path(model, download=True)
            except Exception as error:
                message = str(error)
                log.exception("Language model download failed")
                self.app.post(lambda: self.app.toast(
                    t("Language model download failed: {error}", error=message), "warning"))
            self.app.post(self._refresh_llm)

        threading.Thread(target=job, daemon=True).start()

    # --- storage ------------------------------------------------------------------------

    def _build_storage(self) -> None:
        s = self._section(t("Storing recordings"))

        def folder(master):
            row = ctk.CTkFrame(master, fg_color="transparent")
            row.grid_columnconfigure(0, weight=1)
            self.folder = ui.entry(row)
            self.folder.insert(0, str(self.settings.recordings_dir))
            self.folder.grid(row=0, column=0, sticky="ew")
            ui.secondary(row, t("Browse…"), self._browse, "folder", width=10).grid(
                row=0, column=1, padx=(8, 0))
            ui.primary(row, t("Save"), self._save_folder, height=34, width=10).grid(
                row=0, column=2, padx=(8, 0))
            return row

        holder = s.full(lambda m: folder(m).pack(fill="x"), pady=(14, 4))
        self.folder_note = ui.muted(holder, "", theme.XS + 1, anchor="w", justify="left", wraplength=620)
        self.folder_note.pack(anchor="w", fill="x", pady=(6, 0))
        self._folder_warning()

        current = retention.retention_days(self.settings["audio_retention_days"])
        choices = list(retention.RETENTION_CHOICES)
        if current not in choices:
            choices.append(current)  # a value set by hand in settings.json
        self.retention_labels = {(t("{n} days", n=d) if d else t("Never")): d for d in choices}
        self.retention_current = current
        self.retention = s.row(t("Delete audio after"), lambda m: ui.option_menu(
            m, list(self.retention_labels), self._retention_label(current), self._retention, width=200),
            t("Only the audio files of recordings with a complete transcript (not stopped halfway). "
              "Transcripts and summaries stay. Ucho checks this at start and once a day."))
        self._retention_request: object | None = None
        s.end()

    def _retention_label(self, days: int) -> str:
        return next(label for label, d in self.retention_labels.items() if d == days)

    def _retention(self, value: str) -> None:
        days = self.retention_labels.get(value, 0)
        if days == self.retention_current:
            self._retention_request = None  # back to the saved value: drop a pending count
            return
        if not days:
            self._retention_request = None
            self._set_retention(days)
            return
        # Shortening the period deletes at once: say how much, and let the user
        # back out. Counting reads every meta.json, which can take long on a big
        # archive or a network drive, so it runs on a worker thread like
        # App.run_retention; the newest choice wins.
        request = self._retention_request = object()
        retention.select_expired_in_background(
            self.settings.recordings_dir, days,
            lambda expired: self.app.post(lambda: self._retention_counted(request, days, expired)))

    def _retention_counted(self, request: object, days: int, expired: list[Path] | None) -> None:
        if request is not self._retention_request or not self.winfo_exists():
            return  # another choice was made meanwhile, or the screen was rebuilt
        self._retention_request = None
        if expired is None:
            self.retention.set(self._retention_label(self.retention_current))
            self.app.toast(t("The recordings folder could not be read. The setting was not changed."),
                           "warning", 6)
            return
        # Sessions recorded, transcribed or queued are skipped by retention anyway.
        expired = [folder for folder in expired if not self.app.meeting.in_use(folder)]
        if expired and not ui.confirm(
                self, t("Delete old audio?"),
                t("Recordings with a complete transcript whose audio is older than {days} days: {n}. "
                  "Their audio files will be deleted now. Transcripts and summaries stay. "
                  "This cannot be undone.", days=days, n=len(expired)),
                t("Delete audio"), destructive=True):
            self.retention.set(self._retention_label(self.retention_current))
            return
        self._set_retention(days)

    def _set_retention(self, days: int) -> None:
        self.retention_current = days
        self._save("audio_retention_days", days)
        self.app.run_retention()

    def _browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(parent=self, initialdir=str(self.settings.recordings_dir))
        if chosen:
            self.folder.delete(0, "end")
            self.folder.insert(0, str(Path(chosen)))
            self._folder_warning()

    def _folder_warning(self) -> None:
        path = Path(self.folder.get().strip() or ".")
        if config.onedrive_folder(path):
            self.folder_note.configure(text=t("This folder is synced by OneDrive: recordings and transcripts "
                                              "would go to the cloud. Choose a folder outside OneDrive."),
                                       text_color=C.warning)
        else:
            self.folder_note.configure(text=t("Recordings and transcripts stay on this computer."),
                                       text_color=C.text_muted)

    def _save_folder(self) -> None:
        path = Path(self.folder.get().strip())
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.folder_note.configure(text=t("Invalid folder: {error}", error=error), text_color=C.warning)
            return
        self._save("recordings_dir", str(path))
        self.app.recordings_dir_changed()
        self._folder_warning()
        self.app.toast(t("Recordings folder saved"))

    # --- about --------------------------------------------------------------------------

    def _build_about(self) -> None:
        s = self._section(t("About"))
        shortcuts = "\n".join([
            t("Record a meeting: {shortcut}", shortcut=hotkey_label(self.settings["hotkey"])),
            t("Dictate: {shortcut}", shortcut=hotkey_label(self.settings["dictation_hotkey"])),
            t("Cancel a dictation: Esc"),
        ])
        s.full(lambda m: ui.label(m, shortcuts, theme.SM - 1, anchor="w", justify="left").pack(anchor="w"),
               pady=(14, 4))
        versions = f"{config.APP_NAME} {config.VERSION}  ·  OpenVINO GenAI {transcribe.genai_version()}" \
                   f"  ·  {t('interface: {language}', language=i18n.language().upper())}"
        s.full(lambda m: ui.muted(m, versions, theme.XS + 1, anchor="w").pack(anchor="w"), pady=(0, 4))

        def buttons(master):
            ui.secondary(master, t("Open the log folder"), lambda: open_path(config.LOG_FILE.parent),
                         "folder", width=10).pack(side="left", padx=(0, 8))
            ui.secondary(master, t("Run the first-start setup again"), self.app.show_wizard,
                         width=10).pack(side="left")
        s.full(buttons, pady=(6, 8))
        s.end()


def autostart_command() -> str:
    from .app import start_command

    return start_command() + " --minimized"


def sync_autostart(settings: config.Settings) -> None:
    """Makes the Run entry match the setting (also fixes a moved installation)."""
    enabled = bool(settings["start_with_windows"])
    current = winshell.autostart_command()
    wanted = autostart_command()
    if enabled and current != wanted:
        winshell.set_autostart(True, wanted)
    elif not enabled and current is not None and "ucho.pyw" in current.lower():
        # Only an entry this app wrote; a "Ucho" entry made by hand stays.
        winshell.set_autostart(False, wanted)
