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
from pathlib import Path


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


# ---- async-task reading (read-only; we deliberately do NOT import async_tasks,
#      whose module load runs a dead-owner orphan SWEEP — a write side effect.
#      These helpers only ever READ task json, so the terminal read commands
#      can never interrupt or mutate an in-flight pair). ----

def _async_dir() -> Path:
    from claude_squared import registry as reg_mod
    return reg_mod.async_dir()


def _read_task(task_id: str) -> "dict | None":
    import json
    p = _async_dir() / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_task_for_pair(pair_name: str) -> "str | None":
    import json
    d = _async_dir()
    if not d.is_dir():
        return None
    best_id, best_started = None, ""
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("pair_name") != pair_name:
            continue
        started = data.get("started_at") or ""
        if best_id is None or started > best_started:
            best_id, best_started = data.get("task_id"), started
    return best_id


def _find_task_prefix(prefix: str) -> "list[str]":
    d = _async_dir()
    if not prefix or not d.is_dir():
        return []
    return [p.stem for p in d.glob(f"{prefix}*.json") if p.stem.startswith(prefix)]


def _resolve_task_ref(ref: str) -> "tuple[str | None, str | None]":
    """Resolve exact task id -> pair name (latest) -> unique prefix.
    Returns (task_id_or_None, note_or_None). Mirrors pair_poll's ladder."""
    if _read_task(ref) is not None:
        return ref, None
    latest = _latest_task_for_pair(ref)
    if latest:
        return latest, f"resolved pair '{ref}' -> latest task {latest}"
    matches = _find_task_prefix(ref)
    if len(matches) == 1:
        return matches[0], f"resolved prefix '{ref}' -> {matches[0]}"
    if len(matches) > 1:
        shown = ", ".join(m[:12] for m in matches[:5])
        return None, f"ambiguous prefix '{ref}' matches {len(matches)}: {shown}"
    return None, None


def _cmd_poll(argv: list[str]) -> int:
    """Show an async task's status (read-only). Resolve by task id / pair / prefix."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared poll <task_id|pair_name|prefix>", file=sys.stderr)
        return 64
    tid, note = _resolve_task_ref(argv[0])
    if note:
        print(f"  ({note})", file=sys.stderr)
    if tid is None:
        print(f"not found: '{argv[0]}' is not a task id, pair name, or unique prefix",
              file=sys.stderr)
        return 2
    data = _read_task(tid)
    if data is None:
        print(f"task disappeared: {tid}", file=sys.stderr)
        return 2
    status = data.get("status", "?")
    print(f"task {tid[:8]} (pair '{data.get('pair_name')}'): {status}   "
          f"started {_fmt_local(data.get('started_at'))}")
    err = data.get("error")
    if status in ("failed", "stopped") and err:
        print(f"  {err[:400]}")
    res = data.get("result")
    if status == "done" and isinstance(res, dict):
        if "response" in res:  # SendResult
            print(f"  response: {(res.get('response') or '').strip()[:400]}")
        elif "pre_tokens" in res:  # CompactResult
            print(f"  compacted: {res.get('pre_tokens', 0):,} -> "
                  f"{res.get('post_tokens', 0):,} tokens")
    return 0


def _cmd_transcript(argv: list[str]) -> int:
    """Tail the last N conversation turns for a pair (read-only)."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared transcript <pair_name> [N]", file=sys.stderr)
        return 64
    name = argv[0]
    n = 10
    if len(argv) > 1:
        try:
            n = max(1, int(argv[1]))
        except ValueError:
            print(f"invalid N: {argv[1]}", file=sys.stderr)
            return 64
    from claude_squared import registry as reg_mod
    from claude_squared.errors import PairNotFound
    from claude_squared.transcript import tail_turns
    try:
        spec = reg_mod.get_pair(name)
    except PairNotFound as e:
        print(str(e), file=sys.stderr)
        return 2
    tp = _transcript_path(spec)
    turns = tail_turns(tp, last_n=n) if isinstance(tp, Path) and tp.exists() else []
    if not turns:
        print(f"Pair '{name}': no conversation turns yet.")
        return 0
    print(f"Pair '{name}' - last {len(turns)} turn(s):")
    for t in turns:
        content = (t.get("content") or "").strip().replace("\n", " ")
        if len(content) > 300:
            content = content[:297] + "..."
        print(f"  [{t.get('role', '?')} @ {_fmt_local(t.get('timestamp'))}] {content}")
        for tu in t.get("tool_uses") or []:
            print(f"      -> {tu.get('name')}")
    return 0


def _cmd_status(argv: list[str]) -> int:
    """Liveness for a pair from disk (read-only). A terminal can't see the
    in-process runtime (that lives in the MCP server process); this is derived
    from on-disk task files + main.log mtime."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared status <pair_name>", file=sys.stderr)
        return 64
    name = argv[0]
    import json
    from claude_squared import registry as reg_mod
    from claude_squared.errors import PairNotFound
    try:
        reg_mod.get_pair(name)
    except PairNotFound as e:
        print(str(e), file=sys.stderr)
        return 2
    d = _async_dir()
    inflight: list[str] = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("pair_name") == name and data.get("status") == "running":
                inflight.append(data.get("task_id"))
    print(f"Pair '{name}' status (terminal view - from task files + main.log):")
    flight = f" ({inflight[0][:8]}...)" if inflight else ""
    print(f"  in-flight async tasks: {len(inflight)}{flight}")
    logp = reg_mod.logs_dir() / name / "main.log"
    if logp.exists():
        idle = time.time() - logp.stat().st_mtime
        print(f"  last log activity: {idle:.0f}s ago")
        try:
            lines = logp.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
            print("  last log line(s):")
            for l in lines:
                print(f"    {l}")
        except Exception:
            pass
    else:
        print("  main.log: none yet")
    print("  (note: live in-process runtime state needs the MCP pair_status tool)")
    return 0


def _cmd_log(argv: list[str]) -> int:
    """Tail the last N lines of a pair's main.log activity log (read-only)."""
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared log <pair_name> [N]", file=sys.stderr)
        return 64
    name = argv[0]
    n = 30
    if len(argv) > 1:
        try:
            n = max(1, int(argv[1]))
        except ValueError:
            print(f"invalid N: {argv[1]}", file=sys.stderr)
            return 64
    from claude_squared import registry as reg_mod
    logp = reg_mod.logs_dir() / name / "main.log"
    if not logp.exists():
        print(f"Pair '{name}': no main.log yet.")
        return 0
    lines = logp.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    print(f"Pair '{name}' - last {len(lines)} main.log line(s):")
    for l in lines:
        print(f"  {l}")
    return 0


def _stop_requests_dir() -> Path:
    from claude_squared import registry as reg_mod
    return reg_mod.pairs_dir() / "stop-requests"


def _cmd_stop(argv: list[str]) -> int:
    """Request that a pair interrupt its current turn. This is the ONLY mutating
    terminal command — it confirms with Y/N unless ``-y`` / ``--yes`` is passed.

    Cross-process design: a terminal can't reach the MCP server's in-process
    runtime, so this writes a timestamped stop-request marker at
    ``~/.claude/pairs/stop-requests/<pair>.json``. The server's runtime read
    loop honors it on its next ~1s tick with a graceful in-band interrupt (the
    pair stays alive and can be re-sent). Requires the MCP server on v0.10.0+.
    The read-only commands above never touch this path, so they can never
    interrupt an in-flight pair."""
    import json
    import os
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python -m claude_squared stop <pair_name> [-y|--yes]", file=sys.stderr)
        return 64
    name = argv[0]
    assume_yes = any(a in ("-y", "--yes") for a in argv[1:])
    from claude_squared import registry as reg_mod
    from claude_squared.errors import PairNotFound
    try:
        reg_mod.get_pair(name)
    except PairNotFound as e:
        print(str(e), file=sys.stderr)
        return 2
    # Only stop if something is actually in flight (a running task for this pair).
    d = _async_dir()
    running: list[str] = []
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("pair_name") == name and data.get("status") == "running":
                running.append(data.get("task_id"))
    if not running:
        print(f"Pair '{name}': nothing in flight to stop (no running task).")
        return 0
    # Confirmation gate (the "are you sure?" the user asked for).
    if not assume_yes:
        if not (sys.stdin and sys.stdin.isatty()):
            print(f"Refusing to stop '{name}' without confirmation (stdin is not a "
                  f"TTY). Re-run with -y to force.", file=sys.stderr)
            return 64
        queued = "" if len(running) == 1 else f"; {len(running) - 1} more queued behind it keep running"
        try:
            ans = input(f"Stop the current turn on '{name}'{queued}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\naborted.", file=sys.stderr)
            return 0
        if ans not in ("y", "yes"):
            print("aborted.")
            return 0

    # Re-check after the (possibly slow) confirmation — the turn may have
    # finished while the user was reading the prompt. Don't write a marker the
    # next turn would clear, and don't claim a stop that didn't happen.
    still_running = [
        json.loads(p.read_text(encoding="utf-8")).get("task_id")
        for p in (_async_dir().glob("*.json") if _async_dir().is_dir() else [])
        if _safe_running(p, name)
    ]
    if not still_running:
        print(f"Pair '{name}': the turn already completed - nothing to interrupt.")
        return 0

    sd = _stop_requests_dir()
    sd.mkdir(parents=True, exist_ok=True)
    marker = sd / f"{name}.json"
    payload = {
        "pair_name": name,
        # Epoch float on BOTH sides (terminal + runtime) — no ISO-parse ambiguity.
        "requested_at": time.time(),
        "requested_by_pid": os.getpid(),
    }
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(marker)
    print(f"Stop requested for '{name}'. The running turn will be interrupted within "
          f"~1s (graceful in-band interrupt; the pair stays alive and can be re-sent). "
          f"Only the current turn is stopped; any queued sends still run.")
    return 0


def _safe_running(p, name) -> bool:
    import json
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("pair_name") == name and data.get("status") == "running"


def _print_top_usage(to_stderr: bool = False) -> None:
    out = sys.stderr if to_stderr else sys.stdout
    print(
        "claude-squared terminal commands (read-only unless noted; no agent, no inference):\n"
        "  python -m claude_squared list                List all pairs\n"
        "  python -m claude_squared info <pair>         Full config + context% for one pair\n"
        "  python -m claude_squared context <pair>      Context fill % for one pair\n"
        "  python -m claude_squared poll <task|pair>    Async task status\n"
        "  python -m claude_squared transcript <pair> [N]  Tail last N conversation turns\n"
        "  python -m claude_squared status <pair>       Liveness (in-flight tasks + log recency)\n"
        "  python -m claude_squared log <pair> [N]      Tail last N main.log activity lines\n"
        "  python -m claude_squared wait <task|pair>    Block until an async task finishes\n"
        "  python -m claude_squared stop <pair> [-y]    Interrupt a pair's current turn (asks Y/N)\n"
        "\n"
        "  python -m claude_squared                     (no args) Run the MCP server\n"
        "\n"
        "All read commands read ~/.claude/pairs/ directly - zero model involvement and they\n"
        "never interrupt an in-flight pair. Only 'stop' mutates (and it confirms first).",
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
    "poll": _cmd_poll,
    "transcript": _cmd_transcript,
    "status": _cmd_status,
    "log": _cmd_log,
    "wait": _cmd_wait,
    "stop": _cmd_stop,
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
    if cmd == "poll":
        sys.exit(_cmd_poll(rest))
    if cmd == "transcript":
        sys.exit(_cmd_transcript(rest))
    if cmd == "status":
        sys.exit(_cmd_status(rest))
    if cmd == "log":
        sys.exit(_cmd_log(rest))
    if cmd == "stop":
        sys.exit(_cmd_stop(rest))
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
