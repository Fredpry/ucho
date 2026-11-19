# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""First-run wizard, five steps: language, recordings folder, hardware, language model (optional), consent.

Shown in the main window until it is finished once (`setup_complete`). The
hardware step shows the recommendation of ucho.hardware and can measure the
recommended model on a synthetic signal (hardware.benchmark through the
shared engine); the measurement may move the choice one row down.
"""

from __future__ import annotations

import logging
from pathlib import Path

import customtkinter as ctk

from . import config, hardware, llm, theme, transcribe, ui
from .hotkeys import label as hotkey_label
from .i18n import t
from .settings_screen import LanguageModelPicker, short_model
from .theme import C

log = logging.getLogger(__name__)

STEPS = 5


class Wizard(ctk.CTkFrame):
    def __init__(self, master, app, step: int = 0, state: dict | None = None):
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self.app = app
        self.settings = app.settings
        self.step = step
        self.state = state if state is not None else {
            "folder": str(self.settings.recordings_dir),
            "use_recommendation": True,
            "measured": None,
            "analysis_model": llm.chosen(self.settings),
            "summary": bool(self.settings["summary_enabled"]),
        }
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Brand panel on the left.
        brand = ctk.CTkFrame(self, fg_color=C.brand_soft, corner_radius=0, width=250)
        brand.grid(row=0, column=0, rowspan=2, sticky="nsw")
        brand.grid_propagate(False)
        brand.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(brand, text="", image=ui.mark(96)).grid(row=0, column=0, pady=(64, 8))
        ui.label(brand, "Ucho", 34, "bold", family="display").grid(row=1, column=0)
        ui.muted(brand, t("Meeting transcripts and dictation that stay on this computer."),
                 theme.SM - 1, wraplength=190, justify="center").grid(row=2, column=0, padx=24, pady=(6, 0))
        dots = ctk.CTkFrame(brand, fg_color="transparent")
        dots.grid(row=3, column=0, pady=(28, 0))
        for i in range(STEPS):
            ctk.CTkFrame(dots, width=22 if i == step else 8, height=8, corner_radius=4,
                         fg_color=C.action if i <= step else C.border).pack(side="left", padx=3)

        # Content.
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=(40, 40), pady=(36, 0))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)
        ui.muted(content, t("Step {i} of {n}", i=step + 1, n=STEPS), anchor="w").grid(
            row=0, column=0, sticky="w")
        self.body = ctk.CTkFrame(content, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.body.grid_columnconfigure(0, weight=1)
        [self._language, self._folder, self._hardware, self._analysis, self._consent][step]()

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=1, sticky="ew", padx=40, pady=(8, 24))
        footer.grid_columnconfigure(0, weight=1)
        if step > 0:
            ui.secondary(footer, t("Back"), self.back, "back", width=10, height=38).grid(
                row=0, column=1, padx=(0, 10))
        self.next_button = ui.primary(footer, t("Start using Ucho") if step == STEPS - 1 else t("Next"),
                                      self.next, width=140)
        self.next_button.grid(row=0, column=2)
        self.after(50, lambda: ui.focus(self.next_button))

    # --- steps --------------------------------------------------------------------

    def _title(self, text: str, lead: str | None = None) -> None:
        ui.heading(self.body, text, theme.XXL + 2).grid(row=0, column=0, sticky="w")
        if lead:
            ui.muted(self.body, lead, theme.SM, anchor="w", justify="left", wraplength=560).grid(
                row=1, column=0, sticky="w", pady=(6, 18))

    def _language(self) -> None:
        self._title(t("Welcome to Ucho"),
                    t("Ucho records and transcribes your meetings and turns speech into text "
                      "in any window. Everything runs on this computer, without the cloud."))
        ui.label(self.body, t("Interface language"), theme.SM, "bold", anchor="w").grid(
            row=2, column=0, sticky="w", pady=(0, 6))
        current = self.settings["ui_language"]
        options = [("pl", "Polski"), ("en", "English"), ("auto", t("Same as Windows"))]
        box = ctk.CTkFrame(self.body, fg_color="transparent")
        box.grid(row=3, column=0, sticky="w")
        for i, (code, text) in enumerate(options):
            selected = current == code
            button = ctk.CTkButton(
                box, text=text, width=150, height=48, corner_radius=theme.RADIUS_MD,
                font=theme.font(theme.SM + 1, "bold" if selected else "normal"),
                fg_color=C.brand_soft if selected else C.surface, hover_color=C.hover,
                text_color=C.text, border_width=2 if selected else 1,
                border_color=C.action if selected else C.border,
                command=lambda c=code: self._set_language(c))
            button.grid(row=0, column=i, padx=(0, 10))
            ui.focusable(button, button.invoke)

    def _set_language(self, code: str) -> None:
        self.settings["ui_language"] = code
        self.settings.save()
        self.app.apply_language()
        self.app.show_wizard(self.step, self.state)

    def _folder(self) -> None:
        self._title(t("Where should recordings go?"),
                    t("Each meeting gets a folder with the audio, the transcript and, if you want, "
                      "a summary. Roughly 100 MB per hour of meeting."))
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.folder = ui.entry(row)
        self.folder.insert(0, self.state["folder"])
        self.folder.grid(row=0, column=0, sticky="ew")
        self.folder.bind("<KeyRelease>", lambda _e: self._folder_check(), add=True)
        ui.secondary(row, t("Browse…"), self._browse, "folder", width=10, height=36).grid(
            row=0, column=1, padx=(8, 0))
        self.folder_note = ui.label(self.body, "", theme.SM - 1, anchor="w", justify="left",
                                    wraplength=560)
        self.folder_note.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self._folder_check()

    def _browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(parent=self, initialdir=self.folder.get())
        if chosen:
            self.folder.delete(0, "end")
            self.folder.insert(0, str(Path(chosen)))
            self._folder_check()

    def _folder_check(self) -> None:
        path = Path(self.folder.get().strip() or ".")
        self.state["folder"] = str(path)
        if config.onedrive_folder(path):
            self.folder_note.configure(
                text=t("This folder is synced by OneDrive: recordings and transcripts would go to "
                       "the cloud. Choose a folder outside OneDrive, for example {folder}.",
                       folder=config.RECORDINGS_DIR), text_color=C.warning)
        else:
            self.folder_note.configure(text=t("Recordings and transcripts stay on this computer."),
                                       text_color=C.success)

    def _hardware(self) -> None:
        self._title(t("Your hardware"),
                    t("Ucho picks the Whisper model and the device for this computer. "
                      "You can change it later in Settings."))
        self.hw = ui.card(self.body)
        self.hw.grid(row=2, column=0, sticky="ew")
        self.hw.grid_columnconfigure(0, weight=1)
        self.hw_text = ui.label(self.hw, t("Reading the hardware profile…"), theme.SM - 1, anchor="w",
                                justify="left", wraplength=540)
        self.hw_text.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        self.hw_rec = ui.label(self.hw, "", theme.SM, "bold", anchor="w", justify="left", wraplength=540)
        self.hw_rec.grid(row=1, column=0, sticky="ew", padx=18)
        self.hw_reasons = ui.muted(self.hw, "", theme.XS + 1, anchor="w", justify="left", wraplength=540)
        self.hw_reasons.grid(row=2, column=0, sticky="ew", padx=18, pady=(2, 14))
        actions = ctk.CTkFrame(self.body, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.measure = ui.secondary(actions, t("Measure the speed (about 30 s)"), self._measure, "speed",
                                    width=10)
        self.measure.pack(side="left")
        self.download = ui.secondary(actions, t("Download model"), self._download, width=10)
        self.result = ui.muted(actions, "", theme.XS + 1, anchor="w", justify="left", wraplength=320)
        self.result.pack(side="left", padx=(12, 0))
        self.use = ui.switch(self.body, t("Use this recommendation"), self.state["use_recommendation"],
                             lambda v: self.state.update(use_recommendation=v))
        self.use.grid(row=4, column=0, sticky="w", pady=(14, 0))
        self._poll: str | None = None
        self.app.load_hardware(self._show_hardware)

    def _poll_download(self) -> None:
        self._poll = None
        self._show_hardware()

    def _show_hardware(self) -> None:
        if not self.winfo_exists():
            return
        p, rec = self.app.hardware_profile, self.app.hardware_rec
        if p is None or rec is None:
            self.hw_text.configure(text=t("The hardware profile could not be read."))
            return
        gpu = p.usable_gpu()
        self.hw_text.configure(text=t("{cpu}  ·  {ram} GB RAM  ·  graphics: {gpu}", cpu=p.cpu_name or "?",
                                      ram=f"{p.ram_gb:.0f}",
                                      gpu=(gpu.name or gpu.id) if gpu else t("none usable")))
        dictation = t("same model") if rec.dictation_model == rec.model else short_model(rec.dictation_model)
        self.hw_rec.configure(text=t("Recommended: {model} on {device}, dictation: {dictation}",
                                     model=short_model(rec.model), device=rec.device, dictation=dictation))
        self.hw_reasons.configure(text="\n".join([f"•  {r}" for r in rec.reasons]
                                                 + [f"!  {w}" for w in rec.warnings]))
        item = self.app.downloads.get(rec.model)
        running = item is not None and item.running
        downloaded = not running and transcribe.model_downloaded(rec.model)
        self.measure.configure(state="normal" if downloaded else "disabled")
        self.download.pack_forget()
        if running:
            self.result.configure(text=t("Downloading: {progress}", progress=item.progress.describe()))
            # The download goes on in the background when you go to the next step.
            if self._poll is None:
                self._poll = self.after(500, self._poll_download)
        elif not downloaded:
            size = config.MODELS.get(rec.model, {}).get("size", "")
            if item is not None and item.error:
                self.download.configure(text=t("Retry download"), state="normal")
                self.result.configure(text=t("Model download failed: {error}", error=item.error))
            else:
                self.download.configure(text=t("Download model ({size})", size=size), state="normal")
                self.result.configure(text=t("The model is not downloaded yet."))
            self.download.pack(side="left", padx=(8, 0), before=self.result)
        elif self.state["measured"]:
            self.result.configure(text=self.state["measured"])
        else:
            self.result.configure(text="")

    def _measure(self) -> None:
        rec = self.app.hardware_rec
        if rec is None:
            return
        self.measure.configure(state="disabled")
        self.result.configure(text=t("Measuring {model} on {device}…", model=short_model(rec.model),
                                     device=rec.device))

        def done(result) -> None:
            measured, new_rec = result
            self.app.hardware_rec = new_rec
            self.app.hardware_measured = measured
            self.state["measured"] = t("Measured: {x}× faster than real time on {device}.",
                                       x=f"{measured.speed:.1f}", device=measured.device)
            if self.winfo_exists():
                self.measure.configure(state="normal")
                self._show_hardware()
                self.result.configure(text=self.state["measured"])

        def failed(error) -> None:
            if self.winfo_exists():
                self.measure.configure(state="normal")
                self.result.configure(text=t("Measurement failed: {error}", error=error))

        self.app.run_benchmark(rec, done, failed)

    def _download(self) -> None:
        rec = self.app.hardware_rec
        if rec is None:
            return
        self.app.download_model(rec.model)
        self._show_hardware()

    def _analysis(self) -> None:
        self._title(
            t("Meeting names and summaries"),
            t("Optional: a local language model gives your meetings short names and can also "
              "summarize them. It runs on this computer after the transcription. Skip this step "
              "if you only need transcripts; you can choose later in Settings."),
        )
        holder = ctk.CTkFrame(self.body, fg_color="transparent")
        holder.grid(row=2, column=0, sticky="ew")
        ui.label(holder, t("Language model"), theme.SM, "bold", anchor="w").pack(
            anchor="w", pady=(0, 6))
        profile = self.app.hardware_profile
        advice = hardware.recommend_llm(profile) if profile is not None else None
        self.analysis_picker = LanguageModelPicker(
            holder, self.app, self.state["analysis_model"], self._set_analysis,
            width=360, wraplength=540, recommended=advice.model if advice else None,
        )
        self.analysis_picker.pack(anchor="w")
        if advice is not None:
            # A suggestion only: the step stays optional and nothing is downloaded.
            ui.muted(
                holder,
                t("Recommended for this computer: {model}. {reason}",
                  model=advice.model.split("/")[-1], reason=advice.reasons[0]),
                theme.XS + 1, anchor="w", justify="left", wraplength=540,
            ).pack(anchor="w", pady=(8, 0))
        self.summary_switch = ui.switch(
            self.body, t("Also summarize every meeting"), bool(self.state["summary"]),
            lambda v: self.state.update(summary=bool(v)),
        )
        self.summary_switch.grid(row=3, column=0, sticky="w", pady=(14, 0))
        if self.state["analysis_model"] == llm.NONE:
            self.summary_switch.configure(state="disabled")
        ui.muted(
            self.body,
            t("Built-in models are downloaded once from Hugging Face. "
              "Your own model stays where it is; Ucho only reads it."),
            theme.XS + 1, anchor="w", justify="left", wraplength=560,
        ).grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _set_analysis(self, value: str) -> None:
        self.state["analysis_model"] = value
        self.summary_switch.configure(state="normal" if value != llm.NONE else "disabled")

    def _consent(self) -> None:
        self._title(t("Before your first recording"),
                    t("Four things worth knowing before you press REC."))
        points = [
            ("info", t("Tell the other participants that you record the meeting and ask for their "
                       "consent. In many countries this is required by law.")),
            ("mic", t("Use headphones. On speakers the microphone also picks up the others, and the "
                      "transcript mixes the voices.")),
            ("check", t("Audio, transcripts and settings stay on this computer. Ucho sends nothing "
                        "anywhere.")),
            ("meeting", t("Record a meeting: {record}. Dictate into any window: {dictate}.",
                          record=hotkey_label(self.settings["hotkey"]),
                          dictate=hotkey_label(self.settings["dictation_hotkey"]))),
        ]
        box = ui.card(self.body)
        box.grid(row=2, column=0, sticky="ew")
        box.grid_columnconfigure(1, weight=1)
        for i, (glyph, text) in enumerate(points):
            ctk.CTkLabel(box, text="", image=ui.icon(glyph, 18, C.action)).grid(
                row=i, column=0, sticky="n", padx=(18, 10), pady=(14 if i == 0 else 6, 6))
            ui.label(box, text, theme.SM - 1, anchor="w", justify="left", wraplength=500).grid(
                row=i, column=1, sticky="ew", padx=(0, 18), pady=(14 if i == 0 else 6, 6 if i < 3 else 14))

    # --- navigation -----------------------------------------------------------------

    def back(self) -> None:
        self.app.show_wizard(max(0, self.step - 1), self.state)

    def next(self) -> None:
        if self.step == 1:
            path = Path(self.state["folder"])
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.folder_note.configure(text=t("Invalid folder: {error}", error=error),
                                           text_color=C.warning)
                return
        if self.step < STEPS - 1:
            self.app.show_wizard(self.step + 1, self.state)
            return
        self.finish()

    def finish(self) -> None:
        self.settings["recordings_dir"] = self.state["folder"]
        p, rec = self.app.hardware_profile, self.app.hardware_rec
        if self.state["use_recommendation"] and p is not None and rec is not None:
            hardware.save(self.settings, p, rec, getattr(self.app, "hardware_measured", None))
        chosen = self.state.get("analysis_model", llm.chosen(self.settings))
        llm.set_choice(self.settings, chosen)
        self.settings["summary_enabled"] = bool(self.state.get("summary")) and chosen != llm.NONE
        self.settings["setup_complete"] = True
        self.settings.save()
        log.info("First-start setup finished")
        self.app.finish_wizard()
