# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""UI building blocks on the design system: buttons, pills, cards, the record button.

customtkinter widgets are not keyboard-accessible out of the box (buttons do
not take focus, nothing shows where the focus is). `focusable()` adds that:
Tab reaches the widget, Enter/Space activate it, and a 2 px ring in the
focus color shows where you are (design/README.md, "Focus").
"""

from __future__ import annotations

import logging
import sys
import time
import tkinter
from pathlib import Path
from collections.abc import Callable

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from . import theme
from .merge import speaker_track
from .theme import C, font

log = logging.getLogger(__name__)

# --- icons (Segoe Fluent Icons / Segoe MDL2 Assets, part of Windows) ----------

_ICON_FONTS = ("SegoeIcons.ttf", "segmdl2.ttf")
GLYPHS = {
    "meeting": "\uE7C8",     # Record
    "mic": "\uE720",         # Microphone
    "list": "\uE8FD",        # BulletedList
    "settings": "\uE713",
    "search": "\uE721",
    "copy": "\uE8C8",
    "folder": "\uE838",      # OpenLocal
    "back": "\uE72B",
    "refresh": "\uE72C",
    "stop": "\uE71A",
    "info": "\uE946",
    "warning": "\uE7BA",
    "check": "\uE73E",
    "window": "\uE8A7",      # OpenInNewWindow
    "summary": "\uE8BD",     # Message
    "speed": "\uEC4A",       # Speed high
    "close": "\uE711",
    "delete": "\uE74D",
    "edit": "\uE70F",
}
_icon_font_path: str | None = None
_icon_cache: dict = {}


def _font_file() -> str | None:
    global _icon_font_path
    if _icon_font_path is None:
        windir = Path(__import__("os").environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        _icon_font_path = next((str(windir / f) for f in _ICON_FONTS if (windir / f).exists()), "")
    return _icon_font_path or None


def _glyph_image(glyph: str, size: int, color: str) -> Image.Image:
    path = _font_file()
    scale = 4
    image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    if path:
        face = ImageFont.truetype(path, int(size * scale * 0.92))
        pen = ImageDraw.Draw(image)
        box = pen.textbbox((0, 0), glyph, font=face)
        x = (image.width - (box[2] - box[0])) / 2 - box[0]
        y = (image.height - (box[3] - box[1])) / 2 - box[1]
        pen.text((x, y), glyph, font=face, fill=color)
    return image


def icon(name: str, size: int = 18, color: theme.Pair | None = None) -> ctk.CTkImage | None:
    """Icon as a CTkImage with a light and a dark variant; None when the icon font is missing."""
    if name not in GLYPHS or _font_file() is None:
        return None
    color = color or C.text
    key = (name, size, color)
    if key not in _icon_cache:
        glyph = GLYPHS[name]
        _icon_cache[key] = ctk.CTkImage(_glyph_image(glyph, size, color[0]),
                                        _glyph_image(glyph, size, color[1]), (size, size))
    return _icon_cache[key]


def mark(size: int = 28) -> ctk.CTkImage:
    """The ear logo, transparent background, for the sidebar."""
    from . import icon as icon_mod

    key = ("mark", size)
    if key not in _icon_cache:
        light = icon_mod.draw_mark(size * 4, background=None, with_waves=True)
        dark = icon_mod.draw_mark(size * 4, outer=icon_mod.BLUSH_300, inner=icon_mod.BLUSH_400,
                                  waves=icon_mod.BLUSH_400, background=None, with_waves=True)
        _icon_cache[key] = ctk.CTkImage(light, dark, (size, size))
    return _icon_cache[key]


# --- keyboard focus ---------------------------------------------------------------


def focusable(widget, activate: Callable[[], None] | None = None, ring: str = "border",
              ring_color: theme.Pair | None = None) -> None:
    """Tab reaches `widget`, Enter/Space call `activate`, focus shows a ring.

    ring: "border" (buttons, switches, checkboxes: their border turns the focus
    color), "fill" (option menus: the fill turns the soft brand color).
    """
    try:
        tkinter.Frame.configure(widget, takefocus=1)
    except tkinter.TclError:
        return
    state: dict = {}

    def on_in(_event=None):
        try:
            if ring == "border":
                state["border"] = (widget.cget("border_width"), widget.cget("border_color"))
                widget.configure(border_width=2, border_color=ring_color or C.focus)
            elif ring == "fill":
                state["fill"] = (widget.cget("fg_color"), widget.cget("button_color"))
                widget.configure(fg_color=C.brand_soft, button_color=C.brand_soft)
        except (ValueError, tkinter.TclError):
            pass

    def on_out(_event=None):
        try:
            if "border" in state:
                width, color = state.pop("border")
                widget.configure(border_width=width, border_color=color)
            if "fill" in state:
                fg, button = state.pop("fill")
                widget.configure(fg_color=fg, button_color=button)
        except (ValueError, tkinter.TclError):
            pass

    tkinter.Misc.bind(widget, "<FocusIn>", on_in, add="+")
    tkinter.Misc.bind(widget, "<FocusOut>", on_out, add="+")
    if activate is not None:
        def run(_event=None):
            if not _has_state(widget) or str(widget.cget("state")) != "disabled":
                activate()
            return "break"

        tkinter.Misc.bind(widget, "<Return>", run, add="+")
        tkinter.Misc.bind(widget, "<space>", run, add="+")


def _has_state(widget) -> bool:
    try:
        widget.cget("state")
        return True
    except (ValueError, tkinter.TclError):
        return False


def focus(widget) -> None:
    """Moves the keyboard focus to a customtkinter widget itself (not its inner label)."""
    try:
        if widget.winfo_exists():
            tkinter.Misc.focus_set(widget)
    except tkinter.TclError:
        pass


def entry_focus(entry: ctk.CTkEntry) -> None:
    """Visible focus for an entry: thicker border in the focus color."""
    entry.bind("<FocusIn>", lambda _e: entry.configure(border_color=C.focus, border_width=2), add=True)
    entry.bind("<FocusOut>", lambda _e: entry.configure(border_color=C.border, border_width=1), add=True)


def segmented_focus(segmented: ctk.CTkSegmentedButton, command: Callable[[str], None] | None) -> None:
    """Every segment is reachable with Tab and selectable with Enter/Space."""
    for value, button in segmented._buttons_dict.items():  # no public access
        def choose(v=value):
            segmented.set(v)
            if command:
                command(v)
        focusable(button, choose)


# --- basic widgets ---------------------------------------------------------------


def logical(widget, pixels: float) -> int:
    """Physical pixels (e.g. from a <Configure> event) -> customtkinter's logical pixels."""
    return int(pixels / ctk.ScalingTracker.get_widget_scaling(widget))


# customtkinter scrolls by `delta / 6` units on Windows with a 1 px unit, so
# one wheel notch (delta 120) moved a page by 20 px, under one line of
# settings. Windows apps move about three lines (~100 px at 100 %); 5 px per
# unit gives that, scaled with the DPI like the rest of the window.
_WHEEL_UNIT_PX = 5


def scrollable(master, **kwargs) -> ctk.CTkScrollableFrame:
    """A transparent `CTkScrollableFrame` that scrolls at the usual wheel speed."""
    frame = ctk.CTkScrollableFrame(master, fg_color="transparent", corner_radius=0, **kwargs)
    if sys.platform == "win32":
        unit = max(1, round(_WHEEL_UNIT_PX * ctk.ScalingTracker.get_widget_scaling(frame)))
        frame._parent_canvas.configure(yscrollincrement=unit)
    return frame


def label(master, text: str = "", size: int = theme.SM, weight: str = "normal",
          color: theme.Pair | None = None, family: str = "sans", **kwargs) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=font(size, weight, family),
                        text_color=color or C.text, **kwargs)


def heading(master, text: str, size: int = theme.XXL) -> ctk.CTkLabel:
    return label(master, text, size, "bold", family="display", anchor="w")


def muted(master, text: str = "", size: int = theme.XS + 1, **kwargs) -> ctk.CTkLabel:
    return label(master, text, size, color=C.text_muted, **kwargs)


def card(master, **kwargs) -> ctk.CTkFrame:
    options = {"fg_color": C.surface, "corner_radius": theme.RADIUS_MD, "border_width": 1,
               "border_color": C.border}
    options.update(kwargs)
    return ctk.CTkFrame(master, **options)


def primary(master, text: str, command: Callable, icon_name: str | None = None,
            height: int = 38, **kwargs) -> ctk.CTkButton:
    button = ctk.CTkButton(master, text=text, command=command, height=height,
                           corner_radius=theme.RADIUS_MD, font=font(theme.SM, "bold"),
                           fg_color=C.action, hover_color=C.action_hover, text_color=C.on_action,
                           border_color=C.focus, border_width=0,
                           image=icon(icon_name, 16, C.on_action) if icon_name else None,
                           **kwargs)
    # The focus color is close to the fill of a primary button, so its ring is
    # drawn in the button's text color instead (an inset ring, visible in both themes).
    focusable(button, button.invoke, ring_color=C.on_action)
    return button


def _outline(master, text: str, command: Callable, icon_name: str | None, height: int,
             hover: theme.Pair, color: theme.Pair, **kwargs) -> ctk.CTkButton:
    """Outlined button on the surface color; `hover` and `color` (text and icon) set its tone."""
    button = ctk.CTkButton(master, text=text, command=command, height=height,
                           corner_radius=theme.RADIUS_SM + 2, font=font(theme.SM - 1),
                           fg_color=C.surface, hover_color=hover, text_color=color,
                           border_color=C.border, border_width=1,
                           image=icon(icon_name, 15, color) if icon_name else None, **kwargs)
    focusable(button, button.invoke)
    return button


def secondary(master, text: str, command: Callable, icon_name: str | None = None,
              height: int = 34, **kwargs) -> ctk.CTkButton:
    return _outline(master, text, command, icon_name, height, C.hover, C.text, **kwargs)


def danger(master, text: str, command: Callable, icon_name: str | None = None,
           height: int = 34, **kwargs) -> ctk.CTkButton:
    """Secondary button for a destructive action: text and icon in the recording (clay) color."""
    return _outline(master, text, command, icon_name, height, C.recording_soft, C.recording,
                    **kwargs)


def ghost(master, text: str, command: Callable, icon_name: str | None = None,
          color: theme.Pair | None = None, height: int = 30, **kwargs) -> ctk.CTkButton:
    """Text button: no fill, action-colored text (links, "All recordings")."""
    color = color or C.action
    button = ctk.CTkButton(master, text=text, command=command, height=height,
                           corner_radius=theme.RADIUS_SM, font=font(theme.SM - 1, "bold"),
                           fg_color="transparent", hover_color=C.hover, text_color=color,
                           border_color=C.focus, border_width=0,
                           image=icon(icon_name, 15, color) if icon_name else None, **kwargs)
    focusable(button, button.invoke)
    return button


def switch(master, text: str, value: bool, command: Callable[[bool], None]) -> ctk.CTkSwitch:
    var = ctk.BooleanVar(value=bool(value))
    widget = ctk.CTkSwitch(master, text=text, variable=var, font=font(theme.SM - 1),
                           command=lambda: command(bool(var.get())), switch_width=40,
                           switch_height=22, border_width=2, border_color="transparent")
    focusable(widget, widget.toggle)
    return widget


def option_menu(master, values: list[str], current: str, command: Callable[[str], None],
                width: int = 260) -> ctk.CTkOptionMenu:
    widget = ctk.CTkOptionMenu(master, values=values, command=command, width=width, height=34,
                               dynamic_resizing=False, font=font(theme.SM - 1),
                               dropdown_font=font(theme.SM - 1), anchor="w")
    widget.set(current)
    focusable(widget, widget._open_dropdown_menu, ring="fill")
    return widget


def segmented(master, values: list[str], current: str, command: Callable[[str], None],
              **kwargs) -> ctk.CTkSegmentedButton:
    widget = ctk.CTkSegmentedButton(master, values=values, command=command,
                                    font=font(theme.SM - 1, "bold"), height=32, **kwargs)
    widget.set(current)
    segmented_focus(widget, command)
    return widget


def entry(master, placeholder: str = "", **kwargs) -> ctk.CTkEntry:
    widget = ctk.CTkEntry(master, placeholder_text=placeholder, height=36,
                          font=font(theme.SM), **kwargs)
    entry_focus(widget)
    return widget


# --- status pill -------------------------------------------------------------------

PILL_STYLES = {
    "ready": (C.success_ink, C.success_soft),
    "recording": (C.recording_ink, C.recording_soft),
    "working": (C.info_ink, C.info_soft),
    "warning": (C.warning_ink, C.warning_soft),
    "idle": (C.text_muted, C.surface_muted),
}


class StatusPill(ctk.CTkLabel):
    """Rounded status label: Ready (sage), Recording (clay), Transcribing (dusk)."""

    def __init__(self, master, text: str = "", style: str = "ready", **kwargs):
        super().__init__(master, text="", corner_radius=999, height=28,
                         font=font(theme.SM - 1, "bold"), padx=12, **kwargs)
        self._shown: tuple | None = None
        self.show(text, style)

    def show(self, text: str, style: str = "ready") -> None:
        if self._shown == (text, style):
            return
        self._shown = (text, style)
        color, background = PILL_STYLES.get(style, PILL_STYLES["idle"])
        self.configure(text=f"\u25CF  {text}", text_color=color, fg_color=background)


# --- level meter ----------------------------------------------------------------------


class LevelMeter(ctk.CTkFrame):
    """Label + thin bar for the audio level of one track."""

    def __init__(self, master, text: str, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(1, weight=1)
        muted(self, text, theme.XS + 1, width=110, anchor="w").grid(row=0, column=0, sticky="w")
        self.bar = ctk.CTkProgressBar(self, height=8, progress_color=C.success)
        self.bar.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.bar.set(0)
        self._value = 1.0
        self.set(0.0)

    def set(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        if abs(value - self._value) > 0.01:
            # At zero the bar would still draw a round dot; hide it instead.
            if (value < 0.02) != (self._value < 0.02):
                self.bar.configure(progress_color=C.surface_muted if value < 0.02 else C.success)
            self._value = value
            self.bar.set(value)


# --- record button ---------------------------------------------------------------------


class RecordButton(ctk.CTkLabel):
    """Big round button, drawn with PIL so the circle and the pulse ring are smooth.

    Idle: action color. Active: recording color with a slow pulse ring
    (2.4 s, as `--duration-slow`), static when Windows animations are off.
    Keyboard: Tab reaches it, Enter/Space press it, a focus ring shows it.
    """

    PERIOD_S = 2.4
    FRAMES = 36

    def __init__(self, master, command: Callable[[], None], diameter: int = 136,
                 idle_text: str = "REC", active_text: str = "STOP", glyph: str | None = None):
        self._geometry(diameter)
        super().__init__(master, text="", width=self._size, height=self._size, fg_color="transparent")
        self._command = command
        self._idle_text, self._active_text, self._glyph = idle_text, active_text, glyph
        self.active = False
        self.enabled = True
        self._hover = self._focus = self._pressed = False
        self._images: dict = {}
        self._shown = None
        self._still = theme.reduced_motion()
        self.bind("<Enter>", lambda _e: self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda _e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        try:
            tkinter.Frame.configure(self, takefocus=1)
        except tkinter.TclError:
            pass
        tkinter.Misc.bind(self, "<FocusIn>", lambda _e: self._set(focus=True), add="+")
        tkinter.Misc.bind(self, "<FocusOut>", lambda _e: self._set(focus=False), add="+")
        tkinter.Misc.bind(self, "<Return>", lambda _e: self._click(), add="+")
        tkinter.Misc.bind(self, "<space>", lambda _e: self._click(), add="+")
        self.configure(cursor="hand2")
        self.render()

    def _geometry(self, diameter: int) -> None:
        self._d = diameter
        self._pad = int(diameter * 0.16) + 6
        self._size = diameter + 2 * self._pad

    def set_diameter(self, diameter: int) -> None:
        if diameter == self._d:
            return
        self._geometry(diameter)
        self._images.clear()
        self._shown = None
        self.configure(width=self._size, height=self._size)
        self.render()

    def _release(self, event) -> None:
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._set(pressed=False)
        if inside:
            self._click()

    def _click(self) -> None:
        if self.enabled:
            self._command()

    def _set(self, **flags) -> None:
        for key, value in flags.items():
            setattr(self, "_" + key, value)
        self.render()

    def set_state(self, active: bool, enabled: bool = True) -> None:
        if (active, enabled) != (self.active, self.enabled):
            self.active, self.enabled = active, enabled
            self.render()

    def render(self) -> None:
        """Shows the current frame; call regularly while active to animate the pulse."""
        frame = 0
        if self.active and not self._still:
            frame = int((time.monotonic() % self.PERIOD_S) / self.PERIOD_S * self.FRAMES)
        key = (self.active, self.enabled, self._hover, self._focus, self._pressed, frame)
        if key == self._shown:
            return
        self._shown = key
        image = self._images.get(key)
        if image is None:
            image = ctk.CTkImage(self._paint("light", *key), self._paint("dark", *key),
                                 (self._size, self._size))
            if len(self._images) > 400:
                self._images.clear()
            self._images[key] = image
        self.configure(image=image)

    def _paint(self, mode: str, active: bool, enabled: bool, hover: bool, focus: bool,
              pressed: bool, frame: int) -> Image.Image:
        i = 1 if mode == "dark" else 0
        scale = 3
        size = self._size * scale
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        pen = ImageDraw.Draw(image)
        center = size / 2
        r = self._d * scale / 2
        if not enabled:
            fill = C.border[i]
        elif active:
            fill = C.recording_hover[i] if hover else C.recording[i]
        else:
            fill = C.action_hover[i] if hover else C.action[i]
        if pressed and enabled:
            r *= 0.97

        if active:
            if self._still:
                progress, alpha = 0.55, 0.35
            else:
                t = frame / self.FRAMES
                progress = 1 - (1 - t) ** 3  # ease-out
                alpha = 0.6 * (1 - t)
            ring_r = r * (0.96 + 0.20 * progress) + 6 * scale
            color = _rgba(C.recording[i], alpha)
            width = 3 * scale
            pen.ellipse([center - ring_r, center - ring_r, center + ring_r, center + ring_r],
                        outline=color, width=width)
            # A soft halo right around the button.
            halo = r + 5 * scale
            pen.ellipse([center - halo, center - halo, center + halo, center + halo],
                        fill=_rgba(C.recording[i], 0.12))
        if focus:
            ring = r + 5 * scale
            pen.ellipse([center - ring, center - ring, center + ring, center + ring],
                        outline=C.focus[i], width=2 * scale)
        pen.ellipse([center - r, center - r, center + r, center + r], fill=fill)

        text_color = C.on_action[i]
        if active and i == 0:
            text_color = "#FFFFFF"
        if self._glyph and not active:
            path = _font_file()
            face = ImageFont.truetype(path, int(r * 0.62)) if path else ImageFont.load_default()
            text = GLYPHS.get(self._glyph, "") if path else self._idle_text
        else:
            face = _ui_font(int(r * (0.34 if len(self._active_text if active else self._idle_text) <= 4
                                    else 0.26)))
            text = self._active_text if active else self._idle_text
        if active and self._glyph:
            # Stop square for the dictation button.
            side = r * 0.5
            pen.rounded_rectangle([center - side / 2, center - side / 2, center + side / 2,
                                   center + side / 2], radius=side * 0.18, fill=text_color)
        else:
            box = pen.textbbox((0, 0), text, font=face)
            pen.text((center - (box[0] + box[2]) / 2, center - (box[1] + box[3]) / 2), text,
                     font=face, fill=text_color)
        return image


def _rgba(color: str, alpha: float) -> tuple[int, int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), int(255 * alpha))


_ui_fonts: dict = {}


def _ui_font(size: int):
    if size not in _ui_fonts:
        windir = Path(__import__("os").environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"):
            if (windir / name).exists():
                _ui_fonts[size] = ImageFont.truetype(str(windir / name), size)
                break
        else:
            _ui_fonts[size] = ImageFont.load_default(size)
    return _ui_fonts[size]


# --- transcript text ---------------------------------------------------------------------


def _tk_column(line: str, offset: int) -> int:
    """Python string offset -> Tk text column. Tcl 8.6 counts a character above
    U+FFFF (an emoji) as two."""
    if tkinter.TkVersion >= 9:
        return offset
    return offset + sum(1 for char in line[:offset] if ord(char) > 0xFFFF)


class TranscriptText(ctk.CTkTextbox):
    """Read-only transcript: mono timestamps, colored speakers, highlighted search hits."""

    def __init__(self, master, **kwargs):
        super().__init__(master, wrap="word", font=font(theme.SM + 1), border_width=1,
                         border_color=C.border, corner_radius=theme.RADIUS_MD, **kwargs)
        self._text = self._textbox
        self._text.configure(padx=18, pady=14, spacing1=2, spacing3=8, cursor="arrow")
        self.configure(state="disabled")
        self._speaker_tags: dict[str, str] = {}
        self._mode = None
        self.retheme()

    def retheme(self) -> None:
        mode = ctk.get_appearance_mode().lower()
        if mode == self._mode:
            return
        self._mode = mode
        i = 1 if mode == "dark" else 0
        mono = theme.Fonts.mono
        scaling = ctk.ScalingTracker.get_widget_scaling(self)

        def px(size: int) -> int:
            return -round(size * scaling)

        def tk_font(size: int, family: str = "sans", weight: str = "normal") -> tuple:
            name, weight = theme.Fonts.family(family, weight)
            return (name, px(size), weight) if weight == "bold" else (name, px(size))

        strong = tk_font(theme.SM + 1, weight="bold")
        self._text.tag_configure("ts", font=(mono, px(theme.XS + 1)), foreground=C.text_muted[i])
        self._text.tag_configure("me", font=strong, foreground=C.action[i])
        self._text.tag_configure("meeting", font=strong, foreground=C.info[i])
        self._text.tag_configure("other", font=strong, foreground=C.text_muted[i])
        self._text.tag_configure("body", foreground=C.text[i])
        self._text.tag_configure("title", font=tk_font(theme.LG, "display", "bold"),
                                 foreground=C.text[i], spacing1=6, spacing3=6)
        self._text.tag_configure("summary", foreground=C.text[i], lmargin1=0, lmargin2=16)
        self._text.tag_configure("muted", foreground=C.text_muted[i])
        self._text.tag_configure("hit", background=C.brand_soft[i] if i == 0 else C.brand_soft[1],
                                 foreground=C.text[i])

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        for tag in self._speaker_tags.values():
            self._text.tag_delete(tag)
        self._speaker_tags.clear()
        self._text.configure(cursor="arrow")
        self.configure(state="disabled")

    def write(self, *parts: tuple[str, str | tuple[str, ...]]) -> None:
        """Appends (text, tag) pairs."""
        self.retheme()
        self.configure(state="normal")
        for text, tag in parts:
            self._text.insert("end", text, tag)
        self.configure(state="disabled")

    def _speaker_tag(self, code: str, on_click: Callable[[str], None]) -> str:
        tag = self._speaker_tags.get(code)
        if tag is None:
            tag = f"speaker-{len(self._speaker_tags)}"
            self._speaker_tags[code] = tag
            self._text.tag_bind(tag, "<Button-1>", lambda _e, c=code: on_click(c))
            self._text.tag_bind(tag, "<Enter>", lambda _e: self._text.configure(cursor="hand2"))
            self._text.tag_bind(tag, "<Leave>", lambda _e: self._text.configure(cursor="arrow"))
        return tag

    def line(self, timestamp: str, speaker_code: str, speaker: str, text: str,
             on_click: Callable[[str], None] | None = None) -> None:
        track = speaker_track(speaker_code)
        tag = track if track in ("me", "meeting") else "other"
        speaker_tags: str | tuple[str, str] = (
            (tag, self._speaker_tag(speaker_code, on_click)) if on_click is not None else tag
        )
        self.write((f"{timestamp}  ", "ts"), (f"{speaker}  ", speaker_tags), (text + "\n", "body"))

    def highlight(self, words: list[str]) -> str | None:
        """Marks words the way search matches them and returns the index of the first hit.

        Prefix of a word, case- and diacritics-insensitive, "ł" included
        ("lodz" marks "Łódź"), so every search hit is also highlighted.
        """
        from .archive import match_spans

        self._text.tag_remove("hit", "1.0", "end")
        words = [w for w in words if len(w) >= 2]
        first = None
        if not words:
            return None
        lines = self._text.get("1.0", "end-1c").split("\n")
        for number, line in enumerate(lines, start=1):
            for start, end in match_spans(line, words):
                position = f"{number}.{_tk_column(line, start)}"
                self._text.tag_add("hit", position, f"{number}.{_tk_column(line, end)}")
                first = first or position
        if first:
            self._text.see(first)
        return first

    def all_text(self) -> str:
        return self._text.get("1.0", "end").strip()



# --- dialogs --------------------------------------------------------------------------------


def confirm(parent, title: str, message: str, ok_text: str, destructive: bool = False) -> bool:
    """Small modal dialog in the app style. True when the user confirms.

    destructive: the confirm button is drawn in the recording (clay) color.
    Cancel has the focus either way, so Enter does not confirm by accident.
    """
    from .i18n import t

    root = parent.winfo_toplevel()
    dialog = ctk.CTkToplevel(root)
    dialog.title(title)
    dialog.configure(fg_color=C.surface)
    dialog.resizable(False, False)
    dialog.transient(root)
    result = {"ok": False}
    ui_label = label(dialog, title, theme.LG, "bold", anchor="w")
    ui_label.pack(fill="x", padx=24, pady=(20, 6))
    label(dialog, message, theme.SM - 1, anchor="w", justify="left", wraplength=380).pack(
        fill="x", padx=24)
    buttons = ctk.CTkFrame(dialog, fg_color="transparent")
    buttons.pack(fill="x", padx=24, pady=(18, 20))

    def close(ok: bool) -> None:
        result["ok"] = ok
        dialog.grab_release()
        dialog.destroy()

    ok = primary(buttons, ok_text, lambda: close(True), width=10)
    if destructive:
        ok.configure(fg_color=C.recording, hover_color=C.recording_hover)
    ok.pack(side="right")
    cancel = secondary(buttons, t("Cancel"), lambda: close(False), width=10, height=38)
    cancel.pack(side="right", padx=(0, 8))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
    dialog.bind("<Escape>", lambda _e: close(False))
    dialog.after(50, lambda: (dialog.grab_set(), cancel.focus_set()))
    root.wait_window(dialog)
    return result["ok"]


def ask_text(parent, title: str, message: str, initial: str, ok_text: str) -> str | None:
    """Modal dialog with a text entry. Returns the entered text, or None when cancelled."""
    from .i18n import t

    root = parent.winfo_toplevel()
    dialog = ctk.CTkToplevel(root)
    dialog.title(title)
    dialog.configure(fg_color=C.surface)
    dialog.resizable(False, False)
    dialog.transient(root)
    result: dict[str, str | None] = {"value": None}
    ui_label = label(dialog, title, theme.LG, "bold", anchor="w")
    ui_label.pack(fill="x", padx=24, pady=(20, 6))
    label(dialog, message, theme.SM - 1, anchor="w", justify="left", wraplength=380).pack(
        fill="x", padx=24)
    field = entry(dialog)
    if initial:
        field.insert(0, initial)
        field.select_range(0, "end")
    field.pack(fill="x", padx=24, pady=(12, 0))
    buttons = ctk.CTkFrame(dialog, fg_color="transparent")
    buttons.pack(fill="x", padx=24, pady=(18, 20))

    def close(save: bool) -> None:
        if save:
            result["value"] = field.get()
        dialog.grab_release()
        dialog.destroy()

    ok = primary(buttons, ok_text, lambda: close(True), width=10)
    ok.pack(side="right")
    cancel = secondary(buttons, t("Cancel"), lambda: close(False), width=10, height=38)
    cancel.pack(side="right", padx=(0, 8))
    field.bind("<Return>", lambda _e: close(True), add=True)
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
    dialog.bind("<Escape>", lambda _e: close(False))
    dialog.after(50, lambda: (dialog.grab_set(), field.focus_set(), field.select_range(0, "end")))
    root.wait_window(dialog)
    return result["value"]
