# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""UI translations: English (source text) and Polish.

The key is the English text written in the code, and `locale/pl.json` maps it
to Polish. A missing translation falls back to English, so nothing breaks, and
`tests/test_i18n.py` checks that every `t("...")` has an entry in pl.json.

The language comes from the `ui_language` setting ("auto", "pl", "en"). "auto"
follows the system display language: Polish for a Polish system, English
for everything else.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import platforms

log = logging.getLogger(__name__)

SOURCE_LANGUAGE = "en"
_LOCALE_DIR = Path(__file__).resolve().parent / "locale"

_language = SOURCE_LANGUAGE
_translations: dict[str, str] = {}


def system_language() -> str:
    """'pl' for a Polish system display language, 'en' for any other."""
    try:
        code = platforms.sysinfo().ui_language()
    except Exception:
        log.debug("Could not read the system language", exc_info=True)
        code = None
    if code:
        return "pl" if code == "pl" else "en"
    import locale

    code = (locale.getlocale()[0] or "").lower()
    return "pl" if code.startswith(("pl", "polish")) else "en"


def load_translations(language: str) -> dict[str, str]:
    if language == SOURCE_LANGUAGE:
        return {}
    try:
        data = json.loads((_LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("No translations for language %s", language)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}


def set_language(language: str | None) -> str:
    """Sets the UI language for the whole process. Returns the code actually used."""
    global _language, _translations
    if language not in ("pl", "en"):
        language = system_language()
    _language = language
    _translations = load_translations(language)
    return _language


def language() -> str:
    return _language


def t(text: str, **fields) -> str:
    """Text in the current language; fields are inserted with str.format."""
    result = _translations.get(text, text)
    return result.format(**fields) if fields else result
