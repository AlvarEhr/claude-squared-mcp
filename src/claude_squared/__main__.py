"""Entry points for ``python -m claude_squared``.

Default subcommand: run the FastMCP server (no args).

Subcommands:
    wait <task_id> [--timeout <s>] [--poll <s>]
        Block until the async task is ``done`` or ``failed``. Exit 0 on done,
        1 on failed, 2 if the task isn't found, 3 on timeout. Silent by default —
        the agent's follow-up ``pair_poll(task_id)`` is the canonical "read result"
        step. Designed to be invoked from ``Bash(run_in_background=True, ...)`` so
        the harness's task-completion notification fires when the wait exits.
"""

from __future__ import annotations

import sys
import time

from claude_squared import async_tasks


def _cmd_wait(argv: list[str]) -> int:
    """Block until the named async task finishes. Used by the documented background
    polling pattern in ``pair_send_async``."""
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python -m claude_squared wait <task_id> [--timeout SECS] [--poll SECS]\n"
            "  Exit codes: 0=done, 1=failed, 2=not-found, 3=timeout, 64=usage error",
            file=sys.stderr,
        )
        return 64
    task_id = argv[0]
    timeout_s = 1800.0   # 30 min default; the underlying pair_send_async default is 600s
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

    deadline = time.monotonic() + timeout_s
    # First check: existence. If the task file never showed up in the first poll
    # window, treat as not-found (catches typos faster than letting timeout fire).
    initial = async_tasks.load_task(task_id)
    if initial is None:
        # Give it one tick — async_tasks writes the state file synchronously
        # before returning task_id, so this should be vanishingly rare. But
        # filesystem racing during background-Bash startup is real.
        time.sleep(min(poll_s, 1.0))
        if async_tasks.load_task(task_id) is None:
            print(f"task not found: {task_id}", file=sys.stderr)
            return 2

    while True:
        state = async_tasks.load_task(task_id)
        if state is None:
            # Task file was deleted under us (cleanup, manual rm). Treat as not-found.
            print(f"task disappeared: {task_id}", file=sys.stderr)
            return 2
        if state.status == "done":
            return 0
        if state.status == "failed":
            print(state.error or "(no error message)", file=sys.stderr)
            return 1
        if time.monotonic() >= deadline:
            print(f"timeout after {timeout_s}s; task still {state.status}", file=sys.stderr)
            return 3
        time.sleep(poll_s)


def _cmd_serve() -> int:
    from claude_squared.server import mcp
    mcp.run()
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "wait":
        sys.exit(_cmd_wait(argv[1:]))
    # Default: run the MCP server (preserves the no-args invocation that all
    # MCP host configs use).
    sys.exit(_cmd_serve())


if __name__ == "__main__":
    main()
