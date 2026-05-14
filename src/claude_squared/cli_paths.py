"""Single source of truth for path-encoding rules shared with the ``claude`` CLI.

Background
----------
Claude Code stores per-session JSONL transcripts under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. The encoding step
mirrors the CLI's own logic: any character that isn't ``[a-zA-Z0-9]`` is
replaced with ``-``. The MCP needs to compute this encoding in three
unrelated code paths:

  1. ``adapters/claude.py`` — building ``--resume`` lookups
  2. ``runtime.py`` — discovering sub-agent JSONLs spawned during a turn
  3. ``server.py`` — match-parent detection + pair_clear archive paths

Before this module existed, the regex was duplicated across all three sites
(with subtle signature variations: ``Path`` vs ``str``). A drift bug already
landed once — silent because the JSONL just goes "missing" if encodings
disagree across writers and readers. Centralizing here eliminates the drift
risk: when the CLI changes its encoding (which has happened across CLI
versions), this is the single file to update.

Public API
----------
``encode_cwd_for_project(cwd)`` — accepts ``str | Path``, returns the encoded
project-dir name suitable for joining under ``~/.claude/projects/``. Idempotent
(running it twice gives the same result).
"""

from __future__ import annotations

import re
from pathlib import Path

# Regex extracted from CLI binary inspection. Any non-alphanumeric becomes '-'.
# Stable across CLI 2.1.x at time of writing (2026-05). If Anthropic changes
# this, update HERE only — all three call sites import from this module.
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def encode_cwd_for_project(cwd: str | Path) -> str:
    """Encode a working directory path the same way the ``claude`` CLI does
    when laying out ``~/.claude/projects/<encoded>/<session>.jsonl``.

    Accepts either a string or a Path; both produce the same output. Pure
    function — no I/O, no validation that the input is an actual directory.
    """
    return _NON_ALNUM.sub("-", str(cwd))
