"""Source of the standalone ``wait.py`` script that gets installed to
``~/.claude/pairs/wait.py`` on MCP server startup.

Why standalone: the agent's ``Bash(command="python ...")`` invokes whatever
``python`` is on its shell PATH. That Python may NOT have ``claude_squared``
importable — Desktop install bundles the package via PYTHONPATH inside the MCP
server's own subprocess, which the agent's bash spawn doesn't inherit. So a
``python -m claude_squared wait`` command fails for Desktop-only users with
``No module named claude_squared``.

This script imports nothing from our package — only stdlib — so it works on any
Python ≥3.6 the agent's bash happens to find. It just polls the JSON state file
that ``async_tasks.start_task`` writes atomically.

Kept in sync with ``__main__.py``'s ``_cmd_wait`` (which is the in-package
equivalent for users who DO have the module importable). Behavioral parity is a
soft promise; any divergence should favor this standalone version since it's
the one users actually invoke.
"""

WAIT_SCRIPT_SOURCE = '''#!/usr/bin/env python3
"""Standalone async-task waiter for claude-squared.

Polls ~/.claude/pairs/async/<task_id>.json (or $CLAUDE_HOME/pairs/async/...) and
exits when the task transitions to a terminal state. Stdlib-only — no package
imports — so it works regardless of whether claude_squared is installed in the
calling Python's site-packages.

Installed automatically by the claude-squared MCP server on startup. Invoked by
the agent's Bash tool after pair_send/pair_send_async returns an async handle.

Exit codes:
    0  task done
    1  task failed (error message printed to stderr)
    2  task not found (typo, or already cleaned up)
    3  timeout (default 1800s; task still running)
   64  usage error
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _claude_home() -> Path:
    """Mirror claude_squared.registry.claude_home() — stdlib-only."""
    h = os.environ.get("CLAUDE_HOME")
    return Path(h) if h else Path.home() / ".claude"


def _state_path(task_id: str) -> Path:
    return _claude_home() / "pairs" / "async" / f"{task_id}.json"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python wait.py <task_id> [--timeout SECS] [--poll SECS]\\n"
            "  Exit codes: 0=done, 1=failed, 2=not-found, 3=timeout, 64=usage",
            file=sys.stderr,
        )
        return 64

    task_id = argv[0]
    timeout_s = 1800.0   # 30 min default — long enough for deep Opus runs
    poll_s = 2.0
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--timeout" and i + 1 < len(argv):
            try:
                timeout_s = float(argv[i + 1])
            except ValueError:
                print(f"invalid --timeout value: {argv[i + 1]}", file=sys.stderr)
                return 64
            i += 2
        elif a == "--poll" and i + 1 < len(argv):
            try:
                poll_s = max(0.5, float(argv[i + 1]))
            except ValueError:
                print(f"invalid --poll value: {argv[i + 1]}", file=sys.stderr)
                return 64
            i += 2
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            return 64

    state_file = _state_path(task_id)
    deadline = time.monotonic() + timeout_s

    # Initial existence check (one-tick race tolerance for filesystem startup)
    if not state_file.exists():
        time.sleep(min(poll_s, 1.0))
        if not state_file.exists():
            print(f"task not found: {task_id}", file=sys.stderr)
            return 2

    while True:
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Mid-write race or transient I/O error; just retry
            if time.monotonic() >= deadline:
                print(f"timeout after {timeout_s}s; couldnt read state file", file=sys.stderr)
                return 3
            time.sleep(poll_s)
            continue
        status = data.get("status")
        if status == "done":
            return 0
        if status == "failed":
            err = data.get("error") or "(no error message)"
            print(err, file=sys.stderr)
            return 1
        if time.monotonic() >= deadline:
            print(f"timeout after {timeout_s}s; task still {status}", file=sys.stderr)
            return 3
        time.sleep(poll_s)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def install_wait_script(target: "Path | None" = None) -> "Path":
    """Write the standalone wait.py to ``~/.claude/pairs/wait.py`` (or the
    overridden path) if missing or stale. Returns the absolute target path.

    Idempotent: if the on-disk content matches the embedded source, no-op.
    """
    from pathlib import Path
    from claude_squared.registry import pairs_dir
    if target is None:
        target = pairs_dir() / "wait.py"
    try:
        existing = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = None
    if existing != WAIT_SCRIPT_SOURCE:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write to avoid agent reading half-written content
        tmp = target.with_suffix(".tmp")
        tmp.write_text(WAIT_SCRIPT_SOURCE, encoding="utf-8")
        tmp.replace(target)
    return target.resolve()
