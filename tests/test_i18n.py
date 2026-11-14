# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Every UI text has a Polish translation with the same {fields}."""
import ast
import json
import string
from pathlib import Path

from ucho import config, i18n, merge

PACKAGE = Path(__file__).resolve().parent.parent / "ucho"
PL = json.loads((PACKAGE / "locale" / "pl.json").read_text(encoding="utf-8"))


def _keys_from_code() -> set[str]:
    keys = set()
    for file in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    return keys


def _keys_from_config() -> set[str]:
    keys = set(merge.SPEAKER_LABELS.values())
    keys |= set(config.TRACK_LABELS.values())
    keys |= {config.UI_LANGUAGE_LABELS["auto"]}
    keys |= set(config.THEME_LABELS.values())
    keys |= set(config.LAYOUT_LABELS.values())
    keys |= set(config.DICTATION_MODE_LABELS.values())
    keys |= {m["description"] for m in config.MODELS.values()}
    keys |= {m["description"] for m in config.LLM_MODELS.values()}
    return keys


def _fields(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_every_text_has_a_polish_translation():
    missing = sorted((_keys_from_code() | _keys_from_config()) - set(PL))
    assert not missing, f"Missing in pl.json: {missing}"


def test_no_unused_translations():
    unused = sorted(set(PL) - _keys_from_code() - _keys_from_config()
                    - {k for k in PL if k.startswith("_")})
    assert not unused, f"Unused in pl.json: {unused}"


def test_translations_have_the_same_fields():
    for key, value in PL.items():
        if key.startswith("_"):
            continue
        assert _fields(key) == _fields(value), key


def test_language_switching():
    assert i18n.set_language("pl") == "pl"
    assert i18n.t("Settings") == "Ustawienia"
    assert i18n.t("Model downloaded ({size})", size="1 GB") == "Model pobrany (1 GB)"
    assert i18n.set_language("en") == "en"
    assert i18n.t("Settings") == "Settings"
    assert i18n.t("Text without a translation") == "Text without a translation"


def test_speaker_labels_follow_the_language():
    assert merge.speaker_label("me") == "Me"
    i18n.set_language("pl")
    assert merge.speaker_label("me") == "Ja"
    assert merge.speaker_label("meeting") == "Spotkanie"
    assert merge.speaker_label("Anna") == "Anna"
