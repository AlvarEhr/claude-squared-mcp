"""MCPB entry point — invoked by Claude Desktop's host with type='python'.

Adds bundled `server/lib/` (vendored deps) and `src/` (our package) to sys.path,
then runs the FastMCP server over stdio. PYTHONPATH from the manifest is the
primary mechanism; this is belt-and-suspenders in case the env var doesn't carry.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
SRC = HERE.parent / "src"
for p in (LIB, SRC):
    s = str(p)
    if p.exists() and s not in sys.path:
        sys.path.insert(0, s)

from claude_squared.server import mcp  # noqa: E402


if __name__ == "__main__":
    mcp.run()
