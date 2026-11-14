# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Start serwera MCP niezalezny od katalogu roboczego.

Klient MCP (Claude Code, Claude Desktop, Cursor...) uruchamia serwer z wlasnym
katalogiem roboczym, wiec podajemy mu pelna sciezke do tego pliku:

    claude mcp add ucho -- "C:\\Users\\<Ty>\\Ucho\\app\\.venv\\Scripts\\python.exe" "C:\\Users\\<Ty>\\Ucho\\app\\mcp_serwer.py"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ucho_mcp.serwer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
