"""Entry points for ``python -m claude_squared``.

Default subcommand: run the FastMCP server (no args).

Subcommands:
    wait <task_id|prefix|pair_name> [--timeout <s>] [--poll <s>]
        Block until the async task reaches a terminal state. Resolution ladder
        (matches ``pair_poll`` and ``wait.py``): exact task id, then pair name
        (-> that pair's latest task), then unique task-id prefix.

        Exit codes (kept in sync with ``_wait_script.py``):
            0  done · 1  failed (work error) · 2  not-found or ambiguous
            3  timeout · 4  orphaned (MCP server died — NOT a work error)
            5  stopped (pair_stop) · 6  crashed (claude.exe died mid-turn)
            64 usage error
        Silent by default — the agent's follow-up ``pair_poll(task_id)`` is
        the canonical "read result" step. Designed to be invoked from
        ``Bash(run_in_background=True, ...)`` so the harness's task-completion
        notification fires when the wait exits.
"""

from __future__ import annotations

import sys
import time

from claude_squared import async_tasks


def _cmd_wait(argv: list[str]) -> int:
    """Block until the named async task finishes. Used by the documented background
    polling pattern in ``pair_send_async``.

    Kept in feature parity with ``_wait_script.WAIT_SCRIPT_SOURCE`` (the
    standalone stdlib-only watcher installed to ``~/.claude/pairs/wait.py``).
    The standalone version is the one most users invoke; this fallback is
    hit when ``python -m claude_squared`` is on the agent's PATH but the
    install of wait.py at startup failed for any reason.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python -m claude_squared wait <task_id|prefix|pair_name> "
            "[--timeout SECS] [--poll SECS]\n"
            "  Resolution: exact id, then pair name, then unique prefix.\n"
            "  Exit codes: 0=done, 1=failed, 2=not-found-or-ambiguous,\n"
            "              3=timeout, 4=orphaned (MCP server died),\n"
            "              5=stopped (pair_stop), 6=crashed (claude.exe died),\n"
            "              64=usage",
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
    arg = task_id  # original, for error messages
    # Resolution ladder matched to pair_poll / wait.py (v0.9.9): exact task id
    # → pair name → unique task-id prefix. Two attempts tolerate the filesystem
    # race right after task creation. Ambiguous prefix → exit 2 with a clear
    # message (don't retry, more files won't help).
    ambiguous_msg: str | None = None

    def _resolve() -> bool:
        nonlocal task_id, ambiguous_msg
        if async_tasks.load_task(task_id) is not None:
            return True
        latest = async_tasks.latest_task_id_for_pair(task_id)
        if latest:
            print(f"resolved pair '{arg}' -> latest task {latest}", file=sys.stderr)
            task_id = latest
            return async_tasks.load_task(task_id) is not None
        # v0.9.9: prefix resolution — copying an 8-char prefix from `pair_status`
        # output into this command should Just Work the same way it does in
        # pair_poll.
        matches = async_tasks.find_task_by_prefix(task_id)
        if len(matches) == 1:
            resolved = matches[0]
            print(f"resolved prefix '{arg}' -> task {resolved}", file=sys.stderr)
            task_id = resolved
            return async_tasks.load_task(task_id) is not None
        elif len(matches) > 1:
            shown = ", ".join(t[:12] for t in matches[:5])
            more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
            ambiguous_msg = (
                f"ambiguous: prefix '{arg}' matches {len(matches)} tasks: "
                f"{shown}{more}. Use a longer prefix or the full task id."
            )
        return False

    if not _resolve():
        # Don't retry on ambiguous prefix — adding files in 1s won't change
        # multi-match to single-match.
        if ambiguous_msg:
            print(ambiguous_msg, file=sys.stderr)
            return 2
        # Give it one tick — async_tasks writes the state file synchronously
        # before returning task_id, so this should be vanishingly rare. But
        # filesystem racing during background-Bash startup is real.
        time.sleep(min(poll_s, 1.0))
        if not _resolve():
            if ambiguous_msg:
                print(ambiguous_msg, file=sys.stderr)
            else:
                print(
                    f"not found: '{arg}' is not a task id, prefix, or pair name",
                    file=sys.stderr,
                )
            return 2

    while True:
        state = async_tasks.load_task(task_id)
        if state is None:
            # Task file was deleted under us (cleanup, manual rm). Treat as not-found.
            print(f"task disappeared: {task_id}", file=sys.stderr)
            return 2
        if state.status == "done":
            return 0
        if state.status == "stopped":
            # v0.9.8 parity: deliberate cancel via pair_stop is NOT a work error.
            print(state.error or "stopped by pair_stop", file=sys.stderr)
            return 5
        if state.status == "failed":
            err = state.error or "(no error message)"
            print(err, file=sys.stderr)
            # v0.9.8 parity: supervision-class errors map to distinct codes so
            # the caller can dispatch without parsing stderr.
            if err.startswith(async_tasks.ORPHAN_ERROR_PREFIX):
                return 4
            if err.startswith(async_tasks.CRASHED_ERROR_PREFIX):
                return 6
            return 1
        # v0.9.5 parity: detect a dead-owner orphan within one poll cycle
        # instead of waiting for a future server's startup sweep.
        if (state.status == "running"
                and state.owner_pid is not None
                and state.owner_pid > 0
                and not async_tasks._is_pid_alive(int(state.owner_pid))):
            print(
                f"orphaned: owner MCP server (pid {state.owner_pid}) is no longer "
                f"alive; the task was running but its supervisor died mid-turn. The "
                f"work may have completed (check pair_poll / your git or file "
                f"state); pair_send to resume.",
                file=sys.stderr,
            )
            return 4
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
