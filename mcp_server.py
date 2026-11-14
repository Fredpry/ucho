# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Starts the MCP server independently of the working directory.

An MCP client (Claude Code, Claude Desktop, Cursor...) starts the server with
its own working directory, so we give it the full path to this file:

    claude mcp add ucho -- "C:\\Users\\<you>\\Ucho\\app\\.venv\\Scripts\\python.exe" "C:\\Users\\<you>\\Ucho\\app\\mcp_server.py"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ucho_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
