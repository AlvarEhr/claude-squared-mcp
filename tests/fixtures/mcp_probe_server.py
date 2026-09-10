"""Tiny stdio MCP server used by the live connector tests.

Tools:
  probe_read()            -> a fixed token; annotated read-only
  probe_write(name, text) -> writes <PROBE_OUT_DIR>/<name>; annotated destructive

PROBE_OUT_DIR (env) should point OUTSIDE the pair's workspace: a successful
write proves the call was permitted, since MCP server processes are not
sandboxed by either backend.
"""
import os
from pathlib import Path

from fastmcp import FastMCP

OUT = Path(os.environ.get("PROBE_OUT_DIR") or (Path(__file__).parent / "probe-out"))
mcp = FastMCP("cs-probe")


@mcp.tool(annotations={"readOnlyHint": True})
def probe_read() -> str:
    """Return a fixed token. Harmless read."""
    return "PROBE-READ-OK"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def probe_write(name: str, text: str) -> str:
    """Write text to a file in the probe output directory."""
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(text, encoding="utf-8")
        return f"PROBE-WRITE-OK {OUT / name}"
    except OSError as exc:
        return f"PROBE-WRITE-FAILED {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
