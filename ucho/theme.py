# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""customtkinter theme built from the design tokens (design/tokens.json).

Every color is a (light, dark) pair, so customtkinter switches the whole UI
when the appearance mode changes. "system" follows the Windows app theme
(Settings > Personalization > Colors), which customtkinter reads itself.

The tokens file is the source of truth. A copy of the semantic colors is kept
below as a fallback for a package without the `design/` folder;
tests/test_theme.py checks that both agree.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

TOKENS_FILE = config.PROJECT_DIR / "design" / "tokens.json"

Pair = tuple[str, str]

# Semantic colors from design/tokens.json (fallback only, see module docstring).
FALLBACK = {
    "light": {
        "bg": "#FDF7F4", "surface": "#FFFFFF", "surfaceMuted": "#F2EEEB", "border": "#E4DDD8",
        "text": "#2A211E", "textMuted": "#675C55", "brand": "#DF9C86", "brandSoft": "#FAEBE5",
        "action": "#8E4E3D", "actionHover": "#6B3B2F", "onAction": "#FFFFFF", "recording": "#B4452F",
    },
    "dark": {
        "bg": "#171312", "surface": "#221C1A", "surfaceMuted": "#2B2422", "border": "#3A322F",
        "text": "#F4EAE6", "textMuted": "#A99C93", "brand": "#EBBBAA", "brandSoft": "#3A2A25",
        "action": "#EBBBAA", "actionHover": "#F4D6CB", "onAction": "#171312", "recording": "#EE8A72",
    },
}
FALLBACK_STATUS = {
    "sage": {"light": "#4F7A5E", "dark": "#8CC0A0"},
    "amber": {"light": "#8A5F10", "dark": "#E3B45A"},
    "clay": {"light": "#B4452F", "dark": "#EE8A72"},
    "dusk": {"light": "#4E6E8E", "dark": "#93B3D3"},
}
# Focus ring: blush-600 in light mode, blush-300 in dark mode (tokens.css --color-focus).
FALLBACK_FOCUS = ("#B0644E", "#EBBBAA")


def load_tokens(file: Path = TOKENS_FILE) -> dict:
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.info("Design tokens not found at %s, using the built-in copy", file)
        return {}


def mix(color: str, other: str, amount: float) -> str:
    """`amount` of `color` over `other` (0..1), as #RRGGBB."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * amount + y * (1 - amount)):02X}" for x, y in zip(a, b, strict=True))


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colors."""
    def luminance(color: str) -> float:
        channels = []
        for i in (1, 3, 5):
            c = int(color[i:i + 2], 16) / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@dataclass(frozen=True)
class Palette:
    bg: Pair
    surface: Pair
    surface_muted: Pair
    border: Pair
    text: Pair
    text_muted: Pair
    brand: Pair
    brand_soft: Pair
    action: Pair
    action_hover: Pair
    on_action: Pair
    recording: Pair
    recording_hover: Pair
    recording_soft: Pair
    success: Pair
    success_soft: Pair
    info: Pair
    info_soft: Pair
    warning: Pair
    warning_soft: Pair
    focus: Pair
    hover: Pair  # quiet hover for list items and secondary buttons
    selected: Pair  # selected navigation item


def palette(tokens: dict | None = None) -> Palette:
    tokens = load_tokens() if tokens is None else tokens
    color = tokens.get("color", {})
    semantic = color.get("semantic") or FALLBACK
    status = color.get("status") or FALLBACK_STATUS
    blush = color.get("blush", {})
    light, dark = semantic["light"], semantic["dark"]

    def pair(key: str) -> Pair:
        return light[key], dark[key]

    def status_pair(name: str) -> Pair:
        return status[name]["light"], status[name]["dark"]

    def soft(p: Pair, amount: float = 0.14) -> Pair:
        # A tint of the status color on the surface: pill and banner backgrounds.
        return mix(p[0], light["surface"], amount), mix(p[1], dark["surface"], amount + 0.06)

    recording = pair("recording")
    focus = (blush.get("600", FALLBACK_FOCUS[0]), blush.get("300", FALLBACK_FOCUS[1]))
    return Palette(
        bg=pair("bg"), surface=pair("surface"), surface_muted=pair("surfaceMuted"),
        border=pair("border"), text=pair("text"), text_muted=pair("textMuted"),
        brand=pair("brand"), brand_soft=pair("brandSoft"), action=pair("action"),
        action_hover=pair("actionHover"), on_action=pair("onAction"),
        recording=recording,
        recording_hover=(mix(recording[0], "#000000", 0.85), mix(recording[1], "#FFFFFF", 0.85)),
        recording_soft=soft(recording),
        success=status_pair("sage"), success_soft=soft(status_pair("sage")),
        info=status_pair("dusk"), info_soft=soft(status_pair("dusk")),
        warning=status_pair("amber"), warning_soft=soft(status_pair("amber")),
        focus=focus,
        hover=(mix(light["surfaceMuted"], light["surface"], 0.7),
               mix(dark["surfaceMuted"], dark["surface"], 0.9)),
        selected=pair("brandSoft"),
    )


C = palette()


# --- fonts ---------------------------------------------------------------------

SANS = ("Segoe UI Variable Text", "Segoe UI", "Arial")
DISPLAY = ("Sitka Heading", "Georgia", "Times New Roman")  # Iowan/Charter are rare on Windows
MONO = ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")
# Large numbers (the timer): the display cut of Segoe UI, tabular digits.
NUMERIC = ("Segoe UI Variable Display", "Segoe UI Light", "Segoe UI", "Arial")
# Windows ships semibold cuts as separate families; they read calmer than bold.
SEMIBOLD = {
    "sans": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
    "display": ("Sitka Heading Semibold",),
    "numeric": ("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
}


class Fonts:
    """Font families available on this system, resolved once a Tk root exists."""

    sans = SANS[0]
    display = DISPLAY[0]
    mono = MONO[0]
    numeric = NUMERIC[0]
    semibold: dict[str, str | None] = {}

    @classmethod
    def resolve(cls) -> None:
        try:
            import tkinter.font

            available = set(tkinter.font.families())
        except Exception:
            return
        for attr, stack in (("sans", SANS), ("display", DISPLAY), ("mono", MONO),
                            ("numeric", NUMERIC)):
            setattr(cls, attr, next((f for f in stack if f in available), stack[-1]))
        cls.semibold = {key: next((f for f in stack if f in available), None)
                        for key, stack in SEMIBOLD.items()}

    @classmethod
    def family(cls, family: str, weight: str) -> tuple[str, str]:
        """(family, weight) to use; "bold" becomes the semibold cut when installed."""
        if weight == "bold" and cls.semibold.get(family):
            return cls.semibold[family], "normal"
        return getattr(cls, family), weight


def font(size: int = 14, weight: str = "normal", family: str = "sans"):
    """CTkFont from the token stack: family 'sans', 'display', 'mono' or 'numeric'."""
    import customtkinter as ctk

    name, weight = Fonts.family(family, weight)
    return ctk.CTkFont(family=name, size=size, weight=weight)


# Sizes (px) from tokens.css: xs 12, sm 14, base 16, lg 18, xl 22, 2xl 28.
XS, SM, BASE, LG, XL, XXL = 12, 14, 16, 18, 22, 28
RADIUS_SM, RADIUS_MD, RADIUS_LG = 8, 12, 20


# --- applying the theme ------------------------------------------------------


def appearance_mode(setting: str) -> str:
    return {"light": "light", "dark": "dark"}.get(setting, "system")


def apply(setting: str = "system") -> None:
    """Loads the theme colors into customtkinter. Call before creating the root window."""
    import customtkinter as ctk

    ctk.set_appearance_mode(appearance_mode(setting))
    ctk.ThemeManager.load_theme("blue")
    theme = ctk.ThemeManager.theme
    c = C

    def set_(widget: str, **values) -> None:
        theme.setdefault(widget, {}).update({k: list(v) if isinstance(v, tuple) else v
                                             for k, v in values.items()})

    set_("CTk", fg_color=c.bg)
    set_("CTkToplevel", fg_color=c.bg)
    set_("CTkFrame", corner_radius=RADIUS_MD, border_width=0, fg_color=c.surface,
         top_fg_color=c.surface_muted, border_color=c.border)
    set_("CTkButton", corner_radius=RADIUS_MD, border_width=0, fg_color=c.action,
         hover_color=c.action_hover, border_color=c.focus, text_color=c.on_action,
         text_color_disabled=c.text_muted)
    set_("CTkLabel", corner_radius=0, fg_color="transparent", text_color=c.text)
    set_("CTkEntry", corner_radius=RADIUS_SM, border_width=1, fg_color=c.surface,
         border_color=c.border, text_color=c.text, placeholder_text_color=c.text_muted)
    set_("CTkCheckBox", corner_radius=6, border_width=2, fg_color=c.action,
         border_color=c.text_muted, hover_color=c.action_hover, checkmark_color=c.on_action,
         text_color=c.text, text_color_disabled=c.text_muted)
    # One knob color serves both states: white on stone-300 / action in light mode,
    # the dark surface on stone-600 / blush-300 in dark mode.
    set_("CTkSwitch", corner_radius=1000, border_width=3, button_length=0,
         fg_color=("#CDC3BC", "#675C55"), progress_color=c.action,
         button_color=("#FFFFFF", c.surface[1]), button_hover_color=("#FFFFFF", c.surface[1]),
         text_color=c.text, text_color_disabled=c.text_muted)
    set_("CTkRadioButton", corner_radius=1000, border_width_checked=6, border_width_unchecked=2,
         fg_color=c.action, border_color=c.text_muted, hover_color=c.action_hover,
         text_color=c.text, text_color_disabled=c.text_muted)
    set_("CTkProgressBar", corner_radius=1000, border_width=0, fg_color=c.surface_muted,
         progress_color=c.action, border_color=c.border)
    set_("CTkSlider", corner_radius=1000, button_corner_radius=1000, border_width=6,
         button_length=0, fg_color=c.surface_muted, progress_color=c.action,
         button_color=c.action, button_hover_color=c.action_hover)
    set_("CTkOptionMenu", corner_radius=RADIUS_SM, fg_color=c.surface_muted,
         button_color=c.surface_muted, button_hover_color=c.border, text_color=c.text,
         text_color_disabled=c.text_muted)
    set_("CTkComboBox", corner_radius=RADIUS_SM, border_width=1, fg_color=c.surface,
         border_color=c.border, button_color=c.border, button_hover_color=c.text_muted,
         text_color=c.text, text_color_disabled=c.text_muted)
    set_("CTkScrollbar", corner_radius=1000, border_spacing=4, fg_color="transparent",
         button_color=c.border, button_hover_color=c.text_muted)
    set_("CTkSegmentedButton", corner_radius=RADIUS_SM, border_width=3, fg_color=c.surface_muted,
         selected_color=c.surface, selected_hover_color=c.surface, unselected_color=c.surface_muted,
         unselected_hover_color=c.hover, text_color=c.text, text_color_disabled=c.text_muted)
    set_("CTkTextbox", corner_radius=RADIUS_MD, border_width=0, fg_color=c.surface,
         border_color=c.border, text_color=c.text, scrollbar_button_color=c.border,
         scrollbar_button_hover_color=c.text_muted)
    set_("CTkScrollableFrame", label_fg_color=c.surface_muted)
    set_("DropdownMenu", fg_color=c.surface, hover_color=c.surface_muted, text_color=c.text)
    set_("CTkFont", family=Fonts.sans, size=SM, weight="normal")


def apply_fonts() -> None:
    """Picks installed font families. Call once the Tk root exists, before widgets."""
    import customtkinter as ctk

    Fonts.resolve()
    ctk.ThemeManager.theme.setdefault("CTkFont", {}).update(
        {"family": Fonts.sans, "size": SM, "weight": "normal"})


def windows_dark_mode() -> bool | None:
    """The Windows app theme (AppsUseLightTheme), None when unknown."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return None


def reduced_motion() -> bool:
    """Windows "Animation effects" turned off (Settings > Accessibility > Visual effects)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        spi_getclientareaanimation = 0x1042
        ctypes.windll.user32.SystemParametersInfoW(spi_getclientareaanimation, 0,
                                                   ctypes.byref(enabled), 0)
        return not enabled.value
    except Exception:
        return False


def current(pair: Pair) -> str:
    """The color of a pair for the current appearance mode."""
    import customtkinter as ctk

    return pair[1] if ctk.get_appearance_mode().lower() == "dark" else pair[0]
