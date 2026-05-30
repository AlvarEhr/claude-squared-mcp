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
    1  task failed (work error — message printed to stderr)
    2  task not found (typo, or already cleaned up)
    3  timeout (default 1800s; task still running)
    4  orphaned (owner MCP server died mid-turn — supervision event, NOT a work
       error; the work may well have completed — check pair_poll / git / transcript)
   64  usage error
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Kept in sync with claude_squared.async_tasks.ORPHAN_ERROR_PREFIX.
_ORPHAN_ERROR_PREFIX = "ORPHANED: "


def _claude_home() -> Path:
    """Mirror claude_squared.registry.claude_home() — stdlib-only."""
    h = os.environ.get("CLAUDE_HOME")
    return Path(h) if h else Path.home() / ".claude"


def _async_dir() -> Path:
    return _claude_home() / "pairs" / "async"


def _state_path(task_id: str) -> Path:
    return _async_dir() / f"{task_id}.json"


def _latest_task_for_pair(pair_name: str) -> str | None:
    """If any task on disk has pair_name == this arg, return that pair's
    most-recently-STARTED task id (max started_at). Lets the watcher be fired by
    PAIR NAME, like pair_poll — agents know the name, not the UUID. Stdlib-only;
    mirrors async_tasks.latest_task_id_for_pair. ISO-8601 started_at sorts
    lexicographically, so a string max gives the latest without date parsing."""
    d = _async_dir()
    if not d.is_dir():
        return None
    best_id, best_started = None, ""
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("pair_name") != pair_name:
            continue
        started = data.get("started_at") or ""
        if best_id is None or started > best_started:
            best_id, best_started = data.get("task_id"), started
    return best_id


def _pid_alive(pid: int) -> bool:
    """Stdlib cross-platform PID liveness. Biased toward 'alive' on uncertainty
    so we never falsely abandon a healthy long-running task — only return False
    when the OS definitively says the process is gone."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python wait.py <task_id|pair_name> [--timeout SECS] [--poll SECS]\\n"
            "  A pair name resolves to that pair's latest task.\\n"
            "  Exit codes: 0=done, 1=failed, 2=not-found, 3=timeout, 4=orphaned, 64=usage",
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

    arg = task_id  # remember the original for error messages
    state_file = _state_path(task_id)
    deadline = time.monotonic() + timeout_s

    # Accept a PAIR NAME as well as an exact task id (parity with pair_poll): if
    # there's no task file by this name, treat the arg as a pair name and resolve
    # to that pair's latest task. Two attempts tolerate the filesystem race right
    # after the task is created.
    for _attempt in (0, 1):
        if state_file.exists():
            break
        latest = _latest_task_for_pair(task_id)
        if latest:
            print(f"resolved pair '{arg}' -> latest task {latest}", file=sys.stderr)
            task_id = latest
            state_file = _state_path(task_id)
            break
        if _attempt == 0:
            time.sleep(min(poll_s, 1.0))
    if not state_file.exists():
        print(f"not found: no task id or pair named '{arg}'", file=sys.stderr)
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
            # Orphaned (owner server died) is a supervision event, not a work
            # error — distinct exit code so callers can tell them apart.
            return 4 if err.startswith(_ORPHAN_ERROR_PREFIX) else 1
        # Detect an orphan BEFORE any server sweeps it: a "running" task whose
        # owner MCP server is no longer alive would otherwise sit here until the
        # timeout (the bug that caused a 47-min silent wait). Surface it within
        # one poll cycle instead.
        owner = data.get("owner_pid")
        if status == "running" and isinstance(owner, int) and owner > 0 and not _pid_alive(owner):
            print(
                f"orphaned: owner MCP server (pid {owner}) is no longer alive; the task was "
                f"running but its supervisor died mid-turn. The work may have completed "
                f"(check pair_poll / your git or file state); pair_send to resume.",
                file=sys.stderr,
            )
            return 4
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
