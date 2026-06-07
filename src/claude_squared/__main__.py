"""Entry points for ``python -m claude_squared``.

Default (no args): run the FastMCP server — this is what every MCP host config
invokes, so it MUST stay the no-arg behavior.

Subcommands:
    list
        Print all registered pairs (name, model, turns, last active, purpose).
        Pure disk read of ``~/.claude/pairs/registry.json`` — zero inference,
        no model, no agent. (v0.9.11)
    info <pair_name>
        Full config + stats for one pair, including a zero-inference context
        fill % computed from the session JSONL's last turn. (v0.9.11)
    context <pair_name>
        Just the context fill % for one pair (zero inference, from JSONL).
        The full categorized /context breakdown is only via the MCP
        ``pair_context`` tool (that one costs a small pair inference). (v0.9.11)
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

The ``list`` / ``info`` / ``context`` commands are designed for YOU to run in a
terminal — they read on-disk state directly and never involve the agent or cost
an inference. ``--help`` (or ``help``) lists them all.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone


def _fmt_local(dt) -> str:
    """Render a stored naive-UTC datetime (or ISO string) as local time.

    All PairSpec datetimes are stored naive-UTC (``datetime.utcnow()``); attach
    UTC then convert for display. ASCII-only output to avoid Windows cp1252
    stdout crashes when the terminal isn't UTF-8.
    """
    if dt is None:
        return "?"
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _context_fill(spec) -> "tuple[int, int, float] | None":
    """Return ``(used_tokens, window, percent)`` for a pair from its session
    JSONL, or None if there are no turns yet.

    ZERO inference — reuses the adapter's ``_read_last_turn_context_fill`` which
    reads the last assistant message's usage block straight from disk. The
    context window is inferred from the model name (1M for ``1m`` variants, else
    200k), matching the adapter's own fallback.
    """
    try:
        from claude_squared.adapters.claude import ClaudeAdapter
        used = ClaudeAdapter()._read_last_turn_context_fill(spec)  # noqa: SLF001
        if used is None:
            return None
        window = 1_000_000 if "1m" in (spec.model or "").lower() else 200_000
        pct = (used / window * 100) if window else 0.0
        return (used, window, pct)
    except Exception:
        return None


def _transcript_path(spec):
    try:
        from claude_squared.adapters.claude import ClaudeAdapter
        return ClaudeAdapter().transcript_path(spec)
    except Exception:
        return "(unknown)"


def _cmd_list(argv: list[str]) -> int:
    """Print all registered pairs. Pure disk read — no agent, no inference."""
    if argv and argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared list", file=sys.stderr)
        return 64
    from claude_squared import registry as reg_mod
    reg = reg_mod.load()
    pairs = reg.pairs
    if not pairs:
        print("No pairs registered.")
        return 0
    print(f"{len(pairs)} pair(s):")
    for name in sorted(pairs):
        spec = pairs[name]
        purpose = (spec.purpose or "").strip().replace("\n", " ")
        if len(purpose) > 64:
            purpose = purpose[:61] + "..."
        tail = f"  - {purpose}" if purpose else ""
        print(
            f"  {name:<16} {spec.model:<24} {spec.turn_count:>4} turns  "
            f"last {_fmt_local(spec.last_active_at)}{tail}"
        )
    return 0


def _cmd_info(argv: list[str]) -> int:
    """Full config + stats for one pair, with zero-inference context fill %."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared info <pair_name>", file=sys.stderr)
        return 64
    name = argv[0]
    from claude_squared import registry as reg_mod
    from claude_squared.errors import PairNotFound
    try:
        spec = reg_mod.get_pair(name)
    except PairNotFound as e:
        print(str(e), file=sys.stderr)
        return 2
    eff = spec.effort if spec.effort is not None else "none"
    uc = "    ultracode: on" if getattr(spec, "ultracode", False) else ""
    print(f"Pair '{name}':")
    print(f"  session:     {spec.session_id}")
    print(f"  model:       {spec.model}    effort: {eff}    "
          f"permissions: {spec.permission_mode}{uc}")
    print(f"  turns:       {spec.turn_count}    last active: {_fmt_local(spec.last_active_at)}")
    print(f"  cwd:         {spec.cwd or '(server cwd)'}")
    fill = _context_fill(spec)
    if fill:
        used, window, pct = fill
        print(f"  context:     {pct:.0f}% ({used:,} / {window:,} tokens)   "
              f"[zero-inference, from JSONL]")
    else:
        print("  context:     (no turns yet)")
    if spec.persistent:
        print("  persistent:  yes (runtime never idle-evicted)")
    purpose = (spec.purpose or "").strip()
    if purpose:
        print(f"  purpose:     {purpose}")
    print(f"  transcript:  {_transcript_path(spec)}")
    return 0


def _cmd_context(argv: list[str]) -> int:
    """Context fill % for one pair (zero inference, from the session JSONL)."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared context <pair_name>", file=sys.stderr)
        return 64
    name = argv[0]
    from claude_squared import registry as reg_mod
    from claude_squared.errors import PairNotFound
    try:
        spec = reg_mod.get_pair(name)
    except PairNotFound as e:
        print(str(e), file=sys.stderr)
        return 2
    fill = _context_fill(spec)
    if not fill:
        print(f"Pair '{name}': no turns yet (context ~0%).")
        return 0
    used, window, pct = fill
    print(f"Pair '{name}' context: {pct:.0f}% ({used:,} / {window:,} tokens)")
    print("  Source: last assistant turn in the session JSONL "
          "(zero inference, no agent).")
    if pct >= 85:
        print("  [!] Near limit - strongly consider pair_compact.")
    elif pct >= 60:
        print("  Consider pair_compact soon to free context.")
    return 0


def _print_top_usage(to_stderr: bool = False) -> None:
    out = sys.stderr if to_stderr else sys.stdout
    print(
        "claude-squared terminal commands (read-only; no agent, no inference):\n"
        "  python -m claude_squared list             List all pairs\n"
        "  python -m claude_squared info <pair>      Full config + context% for one pair\n"
        "  python -m claude_squared context <pair>   Context fill % for one pair\n"
        "  python -m claude_squared wait <task|pair> Block until an async task finishes\n"
        "\n"
        "  python -m claude_squared                  (no args) Run the MCP server\n"
        "\n"
        "list / info / context read ~/.claude/pairs/ directly - zero model involvement.",
        file=out,
    )


def _cmd_wait(argv: list[str]) -> int:
    """Block until the named async task finishes. Used by the documented background
    polling pattern in ``pair_send_async``.

    Kept in feature parity with ``_wait_script.WAIT_SCRIPT_SOURCE`` (the
    standalone stdlib-only watcher installed to ``~/.claude/pairs/wait.py``).
    The standalone version is the one most users invoke; this fallback is
    hit when ``python -m claude_squared`` is on the agent's PATH but the
    install of wait.py at startup failed for any reason.

    NOTE: the ``async_tasks`` import lives inside this function (not at module
    top) on purpose — importing it runs a module-level dead-owner orphan sweep,
    which is a write side effect we don't want the read-only ``list`` / ``info``
    / ``context`` commands to trigger.
    """
    from claude_squared import async_tasks
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


# Re-export for tests / external callers that want the command table.
_SUBCOMMANDS = {
    "list": _cmd_list,
    "info": _cmd_info,
    "context": _cmd_context,
    "wait": _cmd_wait,
}


def main() -> None:
    argv = sys.argv[1:]
    # Default: no args → run the MCP server. EVERY MCP host config invokes
    # `python -m claude_squared` with no arguments, so this path must stay.
    # IMPORTANT: do NOT touch stdout before serving — FastMCP's stdio transport
    # owns stdout for JSON-RPC framing; reconfiguring it would corrupt the
    # protocol. The encoding hardening below is gated AFTER this early return.
    if not argv:
        sys.exit(_cmd_serve())

    # Read-only subcommands print human-facing text (pair purposes can contain
    # em-dashes etc.). On a Windows cp1252 console that raises UnicodeEncodeError
    # mid-print. Harden stdout/stderr to UTF-8 (so it renders) with replace as a
    # fallback (so it never crashes). Safe here because we've already returned
    # for the serve path — these subcommands own their streams and then exit.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            try:
                _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass

    cmd = argv[0]
    rest = argv[1:]
    if cmd == "wait":
        sys.exit(_cmd_wait(rest))
    if cmd == "list":
        sys.exit(_cmd_list(rest))
    if cmd == "info":
        sys.exit(_cmd_info(rest))
    if cmd == "context":
        sys.exit(_cmd_context(rest))
    if cmd in ("help", "-h", "--help"):
        _print_top_usage(to_stderr=False)
        sys.exit(0)
    # Unknown subcommand → usage to stderr, non-zero. (We do NOT fall through to
    # serve here: an unrecognized arg is a user typo, not a host server launch.)
    print(f"unknown command: {cmd!r}\n", file=sys.stderr)
    _print_top_usage(to_stderr=True)
    sys.exit(64)


if __name__ == "__main__":
    main()
