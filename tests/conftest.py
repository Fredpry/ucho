# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def jezyk_polski():
    """Testy porownuja teksty zrodlowe, wiec interfejs zawsze po polsku."""
    from ucho import i18n

    i18n.ustaw_jezyk("pl")
    yield
    i18n.ustaw_jezyk("pl")
