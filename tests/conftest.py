# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def english_ui():
    """Tests compare source texts, so the UI is always in English."""
    from ucho import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
