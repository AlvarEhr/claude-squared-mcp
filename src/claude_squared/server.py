"""FastMCP server exposing pair_* tools.

All tools return concise human-readable strings by default. Pass `verbose=True`
on tools that support it for the full structured payload.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastmcp import Context, FastMCP
from filelock import FileLock, Timeout as FileLockTimeout

from claude_squared import agents as agents_mod
from claude_squared import async_tasks
from claude_squared import registry as reg_mod
from claude_squared import transcript as transcript_mod
from claude_squared import runtime as runtime_mod
from claude_squared import settings as settings_mod
from claude_squared.adapters import ClaudeAdapter, PairAdapter
from claude_squared.cli_paths import encode_cwd_for_project as _encode_cwd_for_project
from claude_squared.errors import PairAlreadyExists, PairError, PairNotFound
from claude_squared.models import (
    ActionInfo,
    AsyncTaskState,
    CompactResult,
    ContextReport,
    CreateResult,
    PairInfo,
    PairListItem,
    PairSpec,
    SendResult,
    coerce_effort_for_model,
    default_effort_for_model,
)


mcp = FastMCP("claude-squared")


# Install the standalone wait.py to ~/.claude/pairs/wait.py on import. The
# agent's Bash spawn calls this script via its own ``python`` (which only needs
# stdlib), bypassing the "module not importable from agent's PATH-resolved
# python" failure mode that breaks ``python -m claude_squared wait <id>`` on
# Desktop installs (where the package lives under PYTHONPATH set in the MCP's
# own subprocess env, NOT in any pip-installed site-packages).
from claude_squared._wait_script import install_wait_script as _install_wait_script
try:
    _WAIT_SCRIPT_PATH = _install_wait_script()
except OSError:
    # Non-fatal: the runtime hint will fall back to the in-package form.
    # User can still poll manually via pair_poll.
    _WAIT_SCRIPT_PATH = None


# Server-side cap on how long pair_send will hold the JSON-RPC call before
# returning an async handle. Bounded by the MCP host's RPC timeout (typically
# ~60s on Claude Desktop); below that ceiling so our graceful "still running"
# handle always reaches the agent BEFORE the host kills the RPC.
#
# Conceptually separate from the agent's ``timeout_seconds`` (their stated
# patience). When the agent's patience exceeds this cap, we hold the RPC for
# the cap duration, then return the async handle while preserving the agent's
# patience as context ("you stated 300s; we held for 45s; X seconds remain
# of your patience — use pair_wait or pair_poll").
#
# Tunable via env var CLAUDE_PAIR_SYNC_CAP_SECONDS for hosts with different
# RPC timeouts (CLI vs Desktop, future hosts). Default 45s.
def _sync_cap_seconds() -> int:
    try:
        v = int(os.environ.get("CLAUDE_PAIR_SYNC_CAP_SECONDS", "45"))
        return max(5, v)  # clamp to a sane minimum
    except (ValueError, TypeError):
        return 45


def _short(uuid_str: str, n: int = 8) -> str:
    return uuid_str.split("-")[0] if "-" in uuid_str else uuid_str[:n]


def _coerce_to_str_list(value: Any, preserve_empty: bool = False) -> list[str] | None:
    """Coerce a flexible MCP-supplied value into ``list[str] | None``.

    Some MCP host transports JSON-encode list parameters into a *string* before
    sending them to the MCP server (observed: Claude Desktop / CCD MCP bridge
    sending ``'["a","b"]'`` for a declared ``list[str]``). The Pydantic-driven
    FastMCP validator then rejects the call as ``type=list_type`` mismatch.

    To stay robust, every list-typed parameter at the MCP boundary runs through
    this coercer. Accepted shapes:

    - ``None`` -> ``None``
    - ``""`` or ``"[]"`` -> ``None`` (or ``[]`` if ``preserve_empty=True``)
    - ``list[Any]`` -> ``[str(v) for v in value]`` (whitespace stripped, empties
      dropped); empty result -> ``None`` (or ``[]`` if ``preserve_empty=True``)
    - ``str`` looking like a JSON array (starts with ``[``) -> ``json.loads`` then recurse
    - ``str`` containing ``\\n`` -> split on newlines (multi-line agent input)
    - ``str`` containing ``;`` -> split on ``;`` (Windows PATH-style; safe for
      paths that may legitimately contain ``,``)
    - any other ``str`` -> single-element list ``[s.strip()]``
    - any other type -> ``[str(value)]`` (best effort)

    ``preserve_empty=True`` is REQUIRED for fields where ``[]`` semantically
    differs from ``None`` — e.g. ``allowed_invocations`` where ``None`` = "allow
    all" and ``[]`` = "deny all" (explicit lockdown). Without this flag, the
    default ``out or None`` collapse silently destroys lockdown intent. For
    most other fields (``allowed_tools``, ``extra_dirs``, ``mcp_whitelist``)
    ``[]`` and ``None`` are equivalent so the default ``False`` is correct.

    Note: comma-splitting is intentionally NOT supported because OneDrive-Business
    folder names like ``OneDrive - Acme, Inc`` would be silently broken in half.
    Use JSON, semicolons, or newlines for multi-value input.
    """
    if value is None:
        return None
    empty_result: list[str] | None = [] if preserve_empty else None
    if isinstance(value, list):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out if out else empty_result
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return empty_result
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return _coerce_to_str_list(parsed, preserve_empty=preserve_empty)
            except json.JSONDecodeError:
                pass  # fall through to other separators
        if "\n" in s:
            parts = [p.strip() for p in s.split("\n") if p.strip()]
            return parts if parts else empty_result
        if ";" in s:
            parts = [p.strip() for p in s.split(";") if p.strip()]
            return parts if parts else empty_result
        return [s]
    return [str(value)]


def _normalize_path(p: str | None) -> str | None:
    """Lightweight path normalization: strip surrounding quotes/whitespace,
    expand ``~`` and ``$VAR`` / ``%VAR%`` references, then canonicalize separators
    via ``os.path.normpath``. Does NOT call ``resolve()`` or check existence —
    leaves that to the consuming CLI for clean error reporting.

    Helpful for OneDrive paths that the user might pass as ``~/OneDrive`` or
    ``$OneDrive`` rather than the fully-expanded absolute form.

    Caveat: ``os.path.normpath`` does not understand the Windows ``\\\\?\\``
    extended-length prefix and may strip it. For the rare paths that need that
    prefix to bypass MAX_PATH, pass them through ``cwd`` directly without ``~``
    expansion needed and skip ``extra_dirs``-style listing.
    """
    if p is None:
        return None
    s = p.strip()
    # Strip a single matching pair of surrounding quotes (agents sometimes wrap)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if not s:
        return None
    s = os.path.expanduser(s)
    s = os.path.expandvars(s)
    return os.path.normpath(s)


def _normalize_path_list(paths: list[str] | None) -> list[str] | None:
    """Apply ``_normalize_path`` to each entry; drop empties; preserve order."""
    if not paths:
        return None
    out: list[str] = []
    for p in paths:
        n = _normalize_path(p)
        if n:
            out.append(n)
    return out or None


def _fmt_send_result(r: SendResult) -> str:
    # Visual separator so the agent can clearly see where the pair's response
    # starts/ends in the UI (otherwise the response text bleeds into the agent's
    # surrounding message). Bottom marker doubles as the footer (in brackets).
    header = f"━━━ pair '{r.name}' replied ━━━"
    lines = [header, r.response]
    ctx = r.context
    footer_parts: list[str] = []
    if ctx:
        footer_parts.append(f"{ctx.percent:.0f}% ctx ({ctx.tokens_used:,}/{ctx.tokens_max:,})")
    footer_parts.append(r.model_used.split("-")[1] if r.model_used.startswith("claude-") else r.model_used)
    if r.duration_ms:
        footer_parts.append(f"{r.duration_ms / 1000:.1f}s")
    # Log audit pointer: which lines of which file this turn produced
    if r.log_path and r.log_line_start is not None and r.log_line_end is not None:
        try:
            from pathlib import Path as _P
            short_log = _P(r.log_path).name  # just "main.log" — full path is in registry/info
        except Exception:
            short_log = r.log_path
        footer_parts.append(f"log {short_log}:{r.log_line_start}-{r.log_line_end}")
    if r.subagent_logs:
        footer_parts.append(f"+{len(r.subagent_logs)} sub-agent log{'s' if len(r.subagent_logs) != 1 else ''}")
    lines.append(f"\n[{r.name}: " + ", ".join(footer_parts) + "]")
    if ctx and ctx.warning:
        lines.append(f"⚠ {ctx.warning}")
    if r.permission_denials:
        from collections import Counter
        counts = Counter(d.tool_name for d in r.permission_denials)
        denied_summary = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in counts.items())
        lines.append(
            f"\n⛔ PERMISSION HANDOFF: pair '{r.name}' was blocked by auto-mode for "
            f"{len(r.permission_denials)} tool call(s) — {denied_summary}.\n"
            f"   The pair worked around the denials in its reply above (or didn't, if the task required those tools).\n"
            f"\n"
            f"   To retry, the USER must explicitly authorize the blocked action. Do NOT assume or "
            f"retry autonomously — this MCP intentionally surfaces the denial.\n"
            f"\n"
            f"     1. Check the user's most recent message to YOU. If it explicitly authorizes the blocked "
            f"action (e.g. \"go ahead even if blocked\", \"let the pair access X\", \"bypass permissions for this\"), "
            f"proceed to step 3. Otherwise, ASK the user first and wait for a clear go-ahead.\n"
            f"     2. Decide which retry mechanism to use:\n"
            f"          • For most denials in this MCP's headless-CLI mode, the reliable path is to re-send "
            f"with `override_permission_mode=\"bypassPermissions\"`. This bypasses auto-mode for that one call. "
            f"Empirically, just including \"the user authorized this\" in the message text is NOT enough in "
            f"the pair's --print/--resume code path — the auto-mode classifier in headless mode does not "
            f"reliably treat conversation text as user authorization the way interactive Claude Code does.\n"
            f"          • For OUT-OF-SANDBOX path access (Read/Edit/Write of files outside the pair's cwd), "
            f"either bypassPermissions OR pair_clear+pair_create with a wider cwd / --add-dir.\n"
            f"          • For a persistent narrow allowlist of known-safe commands, pair_clear then pair_create "
            f"with `allowed_tools=[...]` (allowed_tools is pinned at create-time, so update→clear→recreate).\n"
            f"     3. Re-send. Pass the user's explicit authorization both as natural-language context in the "
            f"message AND via override_permission_mode=\"bypassPermissions\" (belt + suspenders): "
            f"`pair_send(name=\"...\", message=\"User explicitly authorized: ... Please retry: ...\", "
            f"override_permission_mode=\"bypassPermissions\")`.\n"
            f"\n"
            f"   What was tried but doesn't reliably work in this MCP's mode:\n"
            f"     • Just adding \"the user authorized this\" to the next message without override_permission_mode: "
            f"empirically, the headless CLI's classifier still blocks. Works in interactive Claude Code, NOT here."
        )
    return "\n".join(lines)


def _verbose_dump(model_obj) -> str:
    """JSON-stringified Pydantic model for verbose=True paths."""
    return model_obj.model_dump_json(indent=2, exclude_none=True)


def _verbose_dump_with_msgs(model_obj, transparency_msgs: list[str]) -> str:
    """verbose=True payload that wraps a spec/result with the transparency_msgs
    that the human-readable form surfaces. Without this wrapper, callers asking
    for the full JSON lose visibility into effort coercions / auto-resets — the
    state changed silently from their POV. Mirror the change_messages pattern
    used by pair_settings_set.
    """
    payload = {
        "spec": model_obj.model_dump(exclude_none=True),
        "transparency_msgs": transparency_msgs,
    }
    return json.dumps(payload, indent=2, default=str)


def _fmt_local(dt: datetime | str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a stored datetime (or ISO string) as local-time for human display.

    All stored datetimes in this codebase are naive UTC (``datetime.utcnow()``)
    by convention — that's the right choice for storage (correct cross-process
    comparisons, no DST surprises). For display we need to convert to the
    user's local timezone. This helper attaches UTC tzinfo to the naive
    datetime then converts via ``.astimezone()``.

    Accepts an ISO 8601 string too (e.g. from ``runtime.py:status()`` which
    returns the dict-friendly serialized form).

    Do NOT use this on already-local timestamps (e.g. main.log's ``[HH:MM:SS]``
    prefixes which use ``datetime.now()`` directly) — would double-shift.
    """
    if dt is None:
        return "?"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt  # not parseable — surface raw rather than swallow
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def _make_progress_callback(pair_name: str, ctx: Context):
    """Build a per-event callback that emits MCP log notifications for UI live updates.

    The callback is invoked for each stream-json event during a pair_send drain.
    We filter to the user-meaningful events (text/thinking/tool_use/status) and
    fire ctx.info() for each — Claude Desktop and other MCP hosts surface these
    as live progress in the UI while the tool call is still running.

    Errors in the callback are swallowed upstream so they never break the turn.
    """
    import asyncio as _asyncio

    def _cb(ev: dict) -> None:
        msg = _summarize_event(pair_name, ev)
        if not msg:
            return
        # ctx.info is async; we schedule it on the running loop without blocking
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                _asyncio.create_task(ctx.info(msg))
            else:
                loop.run_until_complete(ctx.info(msg))
        except Exception:
            pass

    return _cb


def _summarize_event(pair_name: str, ev: dict) -> str | None:
    """Convert a stream-json event into a one-line user-visible status string, or None."""
    t = ev.get("type")
    if t == "system":
        sub = ev.get("subtype")
        if sub == "init":
            return f"[{pair_name}] runtime initialized"
        if sub == "status":
            status = ev.get("status")
            if status:
                return f"[{pair_name}] {status}"
        if sub == "compact_boundary":
            meta = ev.get("compact_metadata", {})
            return f"[{pair_name}] compacted {meta.get('pre_tokens', '?')} → {meta.get('post_tokens', '?')} tokens"
    elif t == "assistant":
        msg = ev.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "thinking":
                preview = (block.get("thinking") or "")[:120].replace("\n", " ")
                if preview:
                    return f"[{pair_name}] thinking: {preview}"
            elif bt == "text":
                preview = (block.get("text") or "")[:160].replace("\n", " ")
                if preview:
                    return f"[{pair_name}] {preview}"
            elif bt == "tool_use":
                tool = block.get("name") or "?"
                inp = block.get("input") or {}
                inp_preview = json.dumps(inp, default=str)[:120]
                return f"[{pair_name}] using {tool}({inp_preview})"
    elif t == "user":
        msg = ev.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    preview = str(block.get("content"))[:100].replace("\n", " ")
                    return f"[{pair_name}] tool_result: {preview}"
    return None

# Per-pair locks for FIFO send queueing.
#
# Two layers compose into _PairLock below:
#  1. cross-process FileLock at ~/.claude/pairs/locks/<name>.lock — serializes
#     writes against the same pair's session JSONL across MCP subprocesses
#     (multiple CLI sessions, CLI+Desktop install both loaded, etc.).
#  2. in-process threading.Lock — serializes writes from concurrent threads
#     within ONE MCP subprocess (pair_send_async workers, etc.).
#
# The cross-process layer is the correctness fix: without it, two MCP processes
# would race on the same JSONL with whoever writes second winning, AND each
# process's warm PairRuntime would hold stale in-memory state. Combined with
# PairRuntime.is_stale() + ToolCounter.reload() under this lock, the same pair
# can be safely addressed by multiple Claude sessions in sequence.
_pair_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _pair_lock_path(name: str) -> Path:
    locks_dir = reg_mod.pairs_dir() / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"{name}.lock"


class _PairLock:
    """Combined cross-process (filelock) + in-process (threading.Lock) per-pair lock.

    Use as a context manager:
        with _PairLock(name):
            ...do anything that mutates the pair's session JSONL or pinned config...

    Raises ``filelock.Timeout`` if another process holds the lock longer than the
    configured timeout. That timeout becomes a structured ``PairError`` at the
    public tool boundary (see ``_with_pair_lock``).

    Default timeout 60s. Short turns finish well within this; if a long
    pair_send is in flight elsewhere, you'll wait — which is the FIFO semantics
    we want, not a failure mode.
    """

    def __init__(self, name: str, timeout_s: float = 60.0):
        self.name = name
        self.timeout_s = timeout_s
        self._file_lock = FileLock(str(_pair_lock_path(name)), timeout=timeout_s)
        with _locks_guard:
            self._thread_lock = _pair_locks[name]

    def __enter__(self) -> "_PairLock":
        self._file_lock.__enter__()
        try:
            self._thread_lock.acquire()
        except BaseException:
            # Roll back the file lock so we don't strand the cross-process lock
            self._file_lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self._thread_lock.release()
        finally:
            self._file_lock.__exit__(exc_type, exc_val, exc_tb)


def _with_pair_lock(name: str, timeout_s: float = 60.0) -> _PairLock:
    """Construct a pair-level lock context manager. Translate filelock Timeout to
    PairError at the tool boundary by catching ``FileLockTimeout``.

    Usage in tools:
        try:
            with _with_pair_lock(name):
                ...
        except FileLockTimeout as e:
            raise PairError(f"Pair '{name}' is busy (held by another process > {e.lock_file.timeout}s)")
    """
    return _PairLock(name, timeout_s=timeout_s)


# Legacy alias kept for any internal helpers that grabbed the in-process lock
# directly — new code should use _with_pair_lock for cross-process safety.
def _get_lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _pair_locks[name]


def _adapter_for(spec: PairSpec) -> PairAdapter:
    if spec.backend == "claude":
        return ClaudeAdapter()
    raise PairError(f"Unsupported backend '{spec.backend}'")


# Curated MCP-level commands surfaced via pair_actions
_PAIR_ACTIONS = {
    "send": "pair_send / pair_send_async — message the pair (sync FIFO-queued or async with task_id)",
    "compact": "pair_compact — native /compact via stream-json, optional steering prompt",
    "context": "pair_context — invoke /context for accurate token usage breakdown",
    "clear": "pair_clear — rotate to a new session_id (preserves pair config; old transcript archived)",
    "invoke": "pair_invoke — invoke a skill in the pair (translates to /<skill> via stream-json)",
    "transcript": "pair_transcript — tail recent turns from the pair's JSONL",
    "info": "pair_info — full pair details + transcript path + stats",
    "update": "pair_update — change pair config (model, effort, permission_mode, allowed_tools, purpose)",
    "forget": "pair_forget — remove from registry; archives transcript by default",
    "adopt": "pair_adopt — register an existing claude session UUID as a pair",
    "agent_define": "pair_agent_define — write a custom agent definition to ~/.claude/agents/",
    "agent_list": "pair_agent_list — list defined custom agents",
}


# ============================================================================
# Lifecycle
# ============================================================================

@mcp.tool(
    output_schema=None,
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
def pair_create(
    name: str,
    purpose: str = "",
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    system_prompt_append: str | None = None,
    profile_name: str | None = None,
    allowed_tools: str | list[str] | None = None,
    mcp_whitelist: str | list[str] | None = None,
    cwd: str | None = None,
    extra_dirs: str | list[str] | None = None,
    persistent: bool | None = None,
    allowed_invocations: str | list[str] | None = None,
    initial_message: str | None = None,
    session_id: str | None = None,
    parent_model: str | None = None,
    verbose: bool = False,
) -> str:
    """Create a new long-running addressable pair (Claude Code CLI sub-session).

    The child has true recursion (can spawn its own Agent sub-tasks), persistent context
    across turns, and is specialized via pinned config (system prompt, allowed tools,
    MCP scope — all persist across resume).

    Defaults for ``model`` / ``effort`` / ``permission_mode`` / ``persistent`` /
    ``extra_dirs`` come from ``pair_settings_get`` (or hardcoded fallback: opus / xhigh /
    auto / False / None). All list-typed args (``allowed_tools``, ``mcp_whitelist``,
    ``extra_dirs``, ``allowed_invocations``) accept a list, JSON-array string, or
    semicolon/newline-separated string. See README for invocation allow-list, mid-flight
    config changes, and the full list of pair tools.

    Args:
        name: Unique addressable handle (e.g. "reviewer", "scout").
        purpose: Human-readable description, stored only.
        model: opus|sonnet|haiku alias or full name. Special: ``"match-parent"`` detects
            the calling session's model from JSONL (falls back to opus; pass
            ``parent_model`` to short-circuit detection).
        effort: low|medium|high|xhigh|max. Coerced silently against model capability
            (Sonnet xhigh/max → high; Haiku any → None) with a transparency message.
        permission_mode: auto|acceptEdits|plan|default|dontAsk|bypassPermissions.
            ``bypassPermissions`` skips ALL gates — only use with tightly-scoped
            ``allowed_tools``/``cwd`` or when deliberately opting out of safety.
        system_prompt_append: Text appended to the default system prompt. Pinned at create.
        profile_name: ``~/.claude/pairs/profiles/<name>.md`` used as system_prompt_append
            (combined with ``system_prompt_append`` if both given).
        allowed_tools: Permission patterns like ``["Bash(git *)", "Read", "Edit"]``.
            Pinned at create.
        mcp_whitelist: MCP server names to enable. None or [] = all MCPs disabled.
        cwd: Absolute workspace root for auto-mode (gates file ops OUTSIDE this path).
            Default: MCP server's cwd (which inherits from the calling session for CLI
            users). Use ``extra_dirs`` to widen access without changing root.
        extra_dirs: Additional ``--add-dir`` paths beyond cwd. Each entry runs through
            ~/$VAR/%VAR% expansion ("~/OneDrive" works). Comma-splitting NOT supported
            (OneDrive-Business folders like "OneDrive - Acme, Inc" would break in half).
        persistent: True = subprocess kept alive for MCP server lifetime (no idle
            eviction). Use for pairs you chat with frequently. Default False = lazy-spawn
            + 10-min idle eviction.
        allowed_invocations: ``pair_invoke`` allow-list (fnmatch globs). ``None`` = allow
            all (default, backward-compat); ``[]`` = deny all (lockdown); ``["clear",
            "mcp__claude_ai_*"]`` = allow matching only. Mutable via ``pair_update``
            without eviction. See README "Per-pair invocation allow-list" — this is
            safety rails, not enforcement (``pair_send`` natural language can still
            self-invoke commands).
        initial_message: If set, sent as the first turn. Response in initial_response.
        session_id: Caller-supplied UUID; auto-generated if omitted.
        parent_model: Explicit "my model is X" hint for ``model="match-parent"`` —
            skips JSONL detection (e.g. ``"claude-opus-4-7"``).
        verbose: If True, return full JSON instead of one-line summary.
    """
    sid = session_id or str(uuid.uuid4())
    # Defensive coercion: hosts may JSON-encode list params into strings; widen accepted shapes.
    allowed_tools_norm = _coerce_to_str_list(allowed_tools)
    mcp_whitelist_norm = _coerce_to_str_list(mcp_whitelist)
    # allowed_invocations: distinguish "not passed" (None → allow all) from "explicit
    # empty list" (deny all). preserve_empty=True keeps `[]` as `[]` instead of
    # collapsing to `None` — without it, the lockdown intent is silently lost
    # before the PairSpec/PairDefaults validators can react.
    allowed_invocations_norm = _coerce_to_str_list(allowed_invocations, preserve_empty=True)
    extra_dirs_norm = _normalize_path_list(_coerce_to_str_list(extra_dirs))
    cwd_norm = _normalize_path(cwd)
    # Default cwd to the MCP server's actual cwd (which for CLI usage is inherited from
    # the calling Claude Code session). Resolve once at creation time so the registry
    # records an absolute path — avoids surprises if the server's cwd changes later.
    resolved_cwd = cwd_norm or os.getcwd()

    # Resolve per-call args + ~/.claude/pairs/defaults.json + hardcoded fallback.
    # Returns transparency messages (match-parent detection, effort coercion) to
    # surface in the response — agent always sees what model/effort actually got
    # applied, even when defaults silently filled in or when coercion fired.
    resolved, transparency_msgs = _resolve_pair_create_args(
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        persistent=persistent,
        parent_model=parent_model,
    )
    # Merge per-call extra_dirs with defaults' extra_dirs (per-call wins on overlap;
    # defaults add to the set when no per-call value).
    if extra_dirs_norm is None and resolved.get("extra_dirs_default"):
        extra_dirs_norm = list(resolved["extra_dirs_default"])
    # Same per-call-wins layering for allowed_invocations: only inherit defaults if
    # the caller explicitly omitted (None). Explicit [] is a lockdown intent and
    # must NOT be overridden by defaults.
    if allowed_invocations_norm is None and resolved.get("allowed_invocations_default") is not None:
        allowed_invocations_norm = list(resolved["allowed_invocations_default"])

    spec = PairSpec(
        name=name,
        session_id=sid,
        purpose=purpose,
        model=resolved["model"],
        effort=resolved["effort"],  # type: ignore[arg-type]
        permission_mode=resolved["permission_mode"],  # type: ignore[arg-type]
        system_prompt_append=system_prompt_append,
        profile_name=profile_name,
        allowed_tools=allowed_tools_norm,
        mcp_whitelist=mcp_whitelist_norm,
        allowed_invocations=allowed_invocations_norm,
        cwd=resolved_cwd,
        extra_dirs=extra_dirs_norm,
        persistent=resolved["persistent"],
    )
    # Register first so concurrent pair_send calls see it
    reg_mod.add_pair(spec)
    try:
        # Step 1: materialize the empty session JSONL via the cheap "pair-ready"
        # probe. This always finishes in ~2-3s — well under any host RPC timeout.
        # We do NOT use the user's initial_message here; that's a separate async
        # send below so a long briefing doesn't time out at the host JSON-RPC
        # layer (Claude Desktop's RPC timeout is typically ~60s; an Opus briefing
        # reading 5 docs can take minutes).
        with _with_pair_lock(name):
            adapter = _adapter_for(spec)
            result = adapter.create(spec, initial_message=None)
            reg_mod.update_pair(name, session_id=result.session_id, turn_count=0)
    except FileLockTimeout:
        try:
            reg_mod.remove_pair(name)
        except PairNotFound:
            pass
        raise PairError(f"Pair '{name}' is busy in another process; could not acquire lock to create.")
    except Exception:
        try:
            reg_mod.remove_pair(name)
        except PairNotFound:
            pass
        raise

    # Echo the RESOLVED values (not what the caller passed) so the agent always
    # sees what got applied — defaults filled in, match-parent expanded, effort
    # coerced. Followed by any transparency messages (one per line of context).
    effort_display = resolved["effort"] if resolved["effort"] is not None else "none"
    line = (
        f"Created '{name}' (session {_short(result.session_id)}, "
        f"model {resolved['model']}, effort {effort_display}, "
        f"permission_mode {resolved['permission_mode']})"
    )
    for msg in transparency_msgs:
        line += f"\n  {msg}"

    # Step 2: if the caller passed an initial_message, route it through the
    # async machinery so it returns instantly with a task_id — no RPC timeout
    # risk even if the pair takes 10+ minutes to process. Agent then polls
    # via pair_poll or watches via `python -m claude_squared wait <task_id>`.
    if initial_message:
        try:
            current = reg_mod.get_pair(name)
            runner = _build_send_runner(
                name, initial_message,
                hard_timeout_seconds=None,  # no auto-kill ceiling — full briefing time
                override_model=None,
                override_effort=None,
                override_permission_mode=None,
                on_event=None,
                lock_acquire_timeout_s=3600.0,
            )
            state = async_tasks.start_task(name, initial_message, runner)
            # Reuse the standard async-handle framing so initial-message guidance
            # matches what agents see from pair_send / pair_send_async — keeps
            # the imperative "RUN THIS NOW" wording consistent across all paths.
            line += "\n" + _format_async_handle(
                state.task_id,
                "Initial message queued as an async task (long Opus briefings "
                "can take minutes; this avoids the host RPC timeout).",
            )
        except Exception as e:
            line += f"\n(initial_message queue failed: {e}; send manually via pair_send)"

    if verbose:
        return _verbose_dump_with_msgs(result, transparency_msgs)
    return line


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def pair_adopt(
    name: str,
    session_id: str,
    purpose: str = "",
    model: str = "opus",
    effort: str = "xhigh",
    permission_mode: str = "auto",
    cwd: str | None = None,
    verbose: bool = False,
) -> str:
    """Register an EXISTING claude session UUID as a pair.

    Use when you have a session created elsewhere (interactive claude, another tool) and
    want to address it through this MCP. The session_id must reference a session that
    exists under ~/.claude/projects/.
    """
    # Surface effort coercion message to the caller (Sonnet xhigh/max → high,
    # Haiku any → None) — the PairSpec model_validator silently coerces but
    # the caller deserves to know if their requested effort got changed.
    coerced_effort, coercion_msg = coerce_effort_for_model(model, effort)
    spec = PairSpec(
        name=name,
        session_id=session_id,
        purpose=purpose,
        model=model,
        effort=coerced_effort,  # type: ignore[arg-type]
        permission_mode=permission_mode,  # type: ignore[arg-type]
        cwd=cwd,
    )
    reg_mod.add_pair(spec)
    if verbose:
        return _verbose_dump(spec)
    eff_display = coerced_effort if coerced_effort is not None else "none"
    line = f"Adopted session {_short(session_id)} as pair '{name}' (model {model}, effort {eff_display})"
    if coercion_msg:
        line += f"\n  {coercion_msg}"
    return line


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def pair_forget(name: str, archive: bool = True) -> str:
    """Remove a pair from the registry.

    Args:
        name: Pair to remove.
        archive: If True, copy the pair's JSONL transcript to ~/.claude/pairs/archive/
            before removal. Default True.
    """
    # Stop any live runtime for this pair before removing it
    try:
        runtime_mod.registry().evict(name)
    except Exception:
        pass
    spec = reg_mod.remove_pair(name)
    archived: str | None = None
    if archive:
        adapter = _adapter_for(spec)
        src = adapter.transcript_path(spec)
        if src and src.exists():
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            dst = reg_mod.archive_dir() / f"{name}-{ts}.jsonl"
            shutil.copy2(src, dst)
            archived = str(dst)
    msg = f"Forgot '{name}' (session {_short(spec.session_id)})"
    if archived:
        msg += f"; archived to {archived}"
    return msg


# ============================================================================
# Communication
# ============================================================================

def _build_send_runner(
    name: str,
    message: str,
    *,
    hard_timeout_seconds: int | None,
    override_model: str | None,
    override_effort: str | None,
    override_permission_mode: str | None,
    on_event: "Callable[[dict], None] | None" = None,
    lock_acquire_timeout_s: float | None = None,
) -> "Callable[[], SendResult]":
    """Construct the closure that performs one pair send under the cross-process lock.

    Shared between sync ``pair_send`` and ``pair_send_async``. The returned callable
    holds the cross-process pair lock for the entire send.

    ``hard_timeout_seconds=None`` means no auto-kill ceiling — the runtime
    read loop runs as long as needed. Use ``pair_stop(name)`` to cancel manually.

    ``on_event`` (if given) receives every non-result stream-json event observed
    during the send — used by sync path to forward to MCP ``ctx.info()``.
    """
    if lock_acquire_timeout_s is None:
        # Generous default. If a hard timeout is set, base on it; otherwise 1 hour.
        lock_acquire_timeout_s = (
            float(hard_timeout_seconds) + 60.0
            if hard_timeout_seconds is not None
            else 3600.0
        )

    def _run() -> SendResult:
        with _with_pair_lock(name, timeout_s=lock_acquire_timeout_s):
            current = reg_mod.get_pair(name)
            adapter = _adapter_for(current)
            result = adapter.send(
                current, message,
                model=override_model,
                effort=override_effort,
                permission_mode=override_permission_mode,
                timeout_seconds=hard_timeout_seconds,
                on_event=on_event,
            )
            reg_mod.update_pair(
                name,
                last_active_at=datetime.utcnow(),
                turn_count=current.turn_count + 1,
                total_cost_usd=current.total_cost_usd + result.cost_usd,
            )
            return result

    return _run


def _format_async_handle(task_id: str, why: str) -> str:
    """Terse runtime hint when an operation degrades to async.

    Returns only the task_id, three concrete commands, and a one-line nudge
    to run the Bash watcher for hands-off notification — full pedagogy lives
    in pair_send / pair_send_async docstrings.

    The watcher command points at the standalone ``~/.claude/pairs/wait.py``
    (installed at server startup) — stdlib-only, so it works regardless of
    whether ``claude_squared`` is importable from the agent's PATH-resolved
    ``python``. Falls back to ``python -m claude_squared wait`` if the install
    failed (non-fatal — only affects this hint, not actual functionality).
    """
    if _WAIT_SCRIPT_PATH is not None:
        # POSIX-style path is friendlier to bash on both Windows (Git Bash) and
        # POSIX shells than the native Windows backslashed form.
        wait_cmd = f"python {_WAIT_SCRIPT_PATH.as_posix()} {task_id}"
    else:
        wait_cmd = f"python -m claude_squared wait {task_id}"
    return (
        f"{why}\n"
        f"Async task: {task_id}\n"
        f"  pair_poll('{task_id}')                             # status\n"
        f"  pair_poll('{task_id}', with_turn_log=True)         # content\n"
        f"  Bash(run_in_background=True, command=\"{wait_cmd}\")  # notify on done\n"
        f"Tip: run the Bash command now to get a notification when it finishes — otherwise poll manually."
    )


# Hardcoded fallback defaults — used when neither the per-call arg nor the
# defaults.json value specifies a field. Kept in sync with PairSpec's defaults
# so the out-of-the-box behavior matches the original (pre-v0.8.0) signature.
_HARDCODED_DEFAULTS = {
    "model": "opus",
    "effort": None,         # None → derive from model via default_effort_for_model
    "permission_mode": "auto",
    "persistent": False,
    "extra_dirs": None,
}


def _resolve_match_parent_model(parent_model_arg: str | None) -> tuple[str, str | None]:
    """Resolve ``model="match-parent"`` to a real model name.

    Resolution ladder (each step falls through gracefully):
      1. ``parent_model_arg`` (explicit, fast, no I/O)
      2. ``CLAUDE_CODE_SESSION_ID`` env var → JSONL parse → latest assistant.model
      3. Hardcoded fallback: ``"opus"``

    Returns ``(resolved_model, transparency_message_or_None)``. The message is
    surfaced to the agent in pair_create's response so they always know which
    detection step actually fired (and what to do if detection failed).
    """
    if parent_model_arg:
        return parent_model_arg, (
            f"match-parent: using explicit parent_model='{parent_model_arg}'."
        )

    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return _HARDCODED_DEFAULTS["model"], (
            "match-parent: no CLAUDE_CODE_SESSION_ID env var (the calling host "
            f"isn't Claude Code, or env got dropped). Falling back to "
            f"'{_HARDCODED_DEFAULTS['model']}'. Pass parent_model='<your model>' "
            f"explicitly to skip detection."
        )

    # Reuse the existing _encode_cwd_for_project helper defined later in this
    # file (used by _move_session_jsonl_for_cwd_change). Same regex as the
    # adapter / runtime mirrors, kept as a single source within server.py.
    encoded = _encode_cwd_for_project(str(Path.cwd()))
    jsonl_path = reg_mod.claude_home() / "projects" / encoded / f"{sid}.jsonl"
    if not jsonl_path.exists():
        return _HARDCODED_DEFAULTS["model"], (
            f"match-parent: session JSONL not found at {jsonl_path} "
            f"(unexpected — this path formula is shared with sub-agent JSONL "
            f"discovery, so something upstream changed). Falling back to "
            f"'{_HARDCODED_DEFAULTS['model']}'. Pass parent_model='<your model>' "
            f"explicitly to skip detection."
        )

    try:
        last_model: str | None = None
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message", {})
                if isinstance(msg, dict) and msg.get("model"):
                    last_model = msg["model"]
        if last_model:
            return last_model, (
                f"match-parent: detected '{last_model}' from session JSONL "
                f"({jsonl_path.name})."
            )
    except OSError as e:
        return _HARDCODED_DEFAULTS["model"], (
            f"match-parent: couldn't read session JSONL ({e}). Falling back to "
            f"'{_HARDCODED_DEFAULTS['model']}'. Pass parent_model='<your model>' "
            f"explicitly to skip detection."
        )

    return _HARDCODED_DEFAULTS["model"], (
        "match-parent: session JSONL had no assistant messages with model field "
        "(brand-new session?). Falling back to "
        f"'{_HARDCODED_DEFAULTS['model']}'. Pass parent_model='<your model>' "
        f"explicitly."
    )


def _resolve_pair_create_args(
    *,
    model: str | None,
    effort: str | None,
    permission_mode: str | None,
    persistent: bool | None,
    parent_model: str | None,
) -> tuple[dict, list[str]]:
    """Resolve per-call args + defaults file + hardcoded fallback into a
    concrete dict of values to pass to PairSpec. Returns ``(resolved_dict,
    transparency_messages)``.

    Resolution order per field (per-call wins):
      1. per-call arg (if not None)
      2. ~/.claude/pairs/defaults.json value (if set)
      3. Hardcoded fallback from ``_HARDCODED_DEFAULTS``

    Special: ``model="match-parent"`` (from any layer) triggers the JSONL
    detection ladder; ``effort`` for match-parent uses model-appropriate
    default (Opus→xhigh, Sonnet→high, Haiku→None) unless explicitly passed.
    """
    defaults = settings_mod.load_defaults()
    messages: list[str] = []

    def _layered(per_call, default_val, fallback):
        if per_call is not None:
            return per_call
        if default_val is not None:
            return default_val
        return fallback

    resolved_model = _layered(model, defaults.model, _HARDCODED_DEFAULTS["model"])
    resolved_effort = _layered(effort, defaults.effort, _HARDCODED_DEFAULTS["effort"])
    resolved_perm = _layered(
        permission_mode, defaults.permission_mode, _HARDCODED_DEFAULTS["permission_mode"]
    )
    resolved_persist = _layered(persistent, defaults.persistent, _HARDCODED_DEFAULTS["persistent"])

    # Match-parent expansion. Happens AFTER layering so it works whether
    # match-parent comes from per-call (model="match-parent") or from
    # defaults.json. Effort under match-parent uses per-model default.
    if resolved_model == "match-parent":
        resolved_model, msg = _resolve_match_parent_model(parent_model)
        if msg:
            messages.append(msg)
        # Only auto-derive effort if user didn't explicitly pass one anywhere.
        # (User explicit effort + match-parent model is valid: "use my parent's
        # model but with effort=low to save tokens".)
        effort_was_explicit = effort is not None or defaults.effort is not None
        if not effort_was_explicit:
            resolved_effort = default_effort_for_model(resolved_model)

    # Effort coercion against the now-resolved model. Surface a transparency
    # message if coercion happened so the user knows what the actual stored
    # value is (not what they asked for).
    coerced_effort, coercion_msg = coerce_effort_for_model(resolved_model, resolved_effort)
    if coercion_msg:
        messages.append(coercion_msg)
    resolved_effort = coerced_effort

    return {
        "model": resolved_model,
        "effort": resolved_effort,
        "permission_mode": resolved_perm,
        "persistent": resolved_persist,
        "extra_dirs_default": defaults.extra_dirs,  # caller merges with per-call
        # Surface the defaults' allowed_invocations so caller can layer it on
        # only when the per-call value is None (preserves explicit-[] lockdown).
        "allowed_invocations_default": defaults.allowed_invocations,
    }, messages


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def pair_send(
    name: str,
    message: str,
    timeout_seconds: int = 45,
    hard_timeout_seconds: int | None = None,
    override_model: str | None = None,
    override_effort: str | None = None,
    override_permission_mode: str | None = None,
    verbose: bool = False,
    ctx: Context | None = None,
) -> str:
    """Send a message to a pair and wait for the response.

    Sync wait blocks for up to ``timeout_seconds``; if the pair is still working
    when the wait expires, returns a "still running, here's the task_id" handle
    (no work is lost, no second turn is queued). Server caps the RPC-hold at
    ``CLAUDE_PAIR_SYNC_CAP_SECONDS`` (default 45s) below the host's RPC timeout —
    if your patience > cap, the handle includes both numbers and the remaining
    patience for ``pair_poll``/``pair_wait``. See README "Async handles" for
    the recommended notification-driven pattern (run a backgrounded
    ``python -m claude_squared wait <id>`` to get fired a completion notification).

    Args:
        timeout_seconds: Your stated patience (default 45s). Work continues
            regardless — this only affects whether you see the result inline or
            via a poll. Values > sync cap degrade gracefully to an async handle.
        hard_timeout_seconds: Auto-kill ceiling for the underlying claude
            operation. Default None (no ceiling) — pair runs until done, errors,
            or you call ``pair_stop``. Long Opus + sub-agent recursion runs can
            legitimately take 30+ minutes; default is "run as long as needed."
        override_model / override_effort / override_permission_mode: per-call
            overrides; cause the adapter to fall back to one-shot ``--print``
            instead of using the warm runtime (those flags are pinned at runtime
            spawn).
        verbose: If True, return the full JSON SendResult instead of the short
            text + footer.

    If another send to this pair is in progress (in this process or another MCP
    subprocess), this call queues behind it via the cross-process pair lock.

    Live updates: text/tool_use/thinking events surface as MCP log notifications
    via ``ctx.info()`` for the host UI, and stream into ``~/.claude/pairs/logs/<name>/main.log``
    in real time (see ``pair_tail``).
    """
    # Decouple two concepts that share the same parameter name:
    #
    # 1. stated_patience_s: what the AGENT said — "I'm willing to wait this long
    #    before doing something else." Preserved as information only.
    # 2. rpc_hold_s: how long the SERVER will hold the JSON-RPC call open.
    #    Bounded by the host's RPC timeout (CLAUDE_PAIR_SYNC_CAP_SECONDS).
    #
    # The work runs async under the hood regardless — it keeps going whether
    # we hold the RPC or return a handle. So agent patience never drives
    # underlying work; it only drives whether they SEE the result inline.
    stated_patience_s = int(timeout_seconds)
    rpc_hold_s = min(stated_patience_s, _sync_cap_seconds())

    # Resolve hard timeouts. None (default) means no auto-kill ceiling — the pair
    # runs until done, errors, or pair_stop is called. Only validate when set.
    if hard_timeout_seconds is not None and hard_timeout_seconds < rpc_hold_s:
        raise PairError(
            f"hard_timeout_seconds ({hard_timeout_seconds}) must be >= "
            f"the effective sync wait ({rpc_hold_s}s)."
        )

    # Sanity: pair must exist before we spawn a worker
    reg_mod.get_pair(name)

    # Build the same runner the async path uses, but with the MCP progress callback
    # wired in so live events surface to the host UI during the sync wait window.
    progress_cb = _make_progress_callback(name, ctx) if ctx is not None else None
    runner = _build_send_runner(
        name, message,
        hard_timeout_seconds=hard_timeout_seconds,
        override_model=override_model,
        override_effort=override_effort,
        override_permission_mode=override_permission_mode,
        on_event=progress_cb,
    )

    state = async_tasks.start_task(name, message, runner)
    final = async_tasks.wait_for_task(state.task_id, timeout_s=float(rpc_hold_s))

    if final is None:
        # Should be impossible — we just created the task
        raise PairError(f"Internal: task {state.task_id} disappeared after creation.")

    if final.status == "done" and final.result is not None:
        if verbose:
            return _verbose_dump(final.result)
        return _fmt_send_result(final.result)

    if final.status == "failed":
        raise PairError(f"pair_send to '{name}' failed: {final.error or '(no error message)'}")

    if final.status == "stopped":
        return (
            f"pair_send to '{name}' was stopped by pair_stop (task {state.task_id}).\n"
            f"The pair's runtime is preserved — call pair_send again to continue."
        )

    # status == "running" → degrade gracefully to async handle.
    hard_str = (
        f"{hard_timeout_seconds}s" if hard_timeout_seconds is not None else "no auto-kill"
    )
    # When the agent's stated patience exceeds the server's RPC-hold cap, name
    # both numbers explicitly so the response is honest about what happened.
    # No silent override — the agent sees server-cap framing AND their original
    # patience preserved, with a concrete path to wait the remainder.
    if stated_patience_s > rpc_hold_s:
        framing = (
            f"Sync wait held for {rpc_hold_s}s (server's RPC-hold cap; "
            f"agent's stated patience was {stated_patience_s}s). "
            f"Pair '{name}' is still working under hard_timeout={hard_str}."
        )
    else:
        framing = (
            f"Sync wait timed out at {rpc_hold_s}s (pair '{name}' still working "
            f"under hard_timeout={hard_str})."
        )
    return _format_async_handle(state.task_id, framing)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def pair_send_async(
    name: str,
    message: str,
    timeout_seconds: int | None = None,
    override_model: str | None = None,
    override_effort: str | None = None,
    override_permission_mode: str | None = None,
) -> str:
    """Send a message to a pair asynchronously. Returns task_id immediately.

    ``timeout_seconds=None`` (default) = no auto-kill ceiling — pair runs until
    done or ``pair_stop`` is called. Multiple async sends to the same pair stay
    FIFO-serialized via the per-pair lock.

    Use ``pair_poll(task_id)`` for status. For notification-on-completion (no
    manual polling), background-run ``python -m claude_squared wait <id>`` via
    the Bash tool — see README "Async handles" for the full pattern.
    """
    # Sanity: pair must exist before we spawn a worker
    reg_mod.get_pair(name)

    runner = _build_send_runner(
        name, message,
        hard_timeout_seconds=timeout_seconds,
        override_model=override_model,
        override_effort=override_effort,
        override_permission_mode=override_permission_mode,
        on_event=None,  # async path: no MCP context to push to
        # Generous lock-acquire window; if a sync send is in flight elsewhere,
        # async should patiently queue rather than fail loudly.
        lock_acquire_timeout_s=(
            max(120.0, float(timeout_seconds) + 60.0)
            if timeout_seconds is not None else 3600.0
        ),
    )
    state = async_tasks.start_task(name, message, runner)
    return _format_async_handle(state.task_id, f"Started async task for pair '{name}'.")


def _read_current_or_last_turn_log(
    pair_name: str, task_finished: bool = False,
) -> tuple[list[str], str]:
    """Return (lines, descriptor) for the current in-flight turn or, if no
    turn is in flight, the most recently completed turn.

    Strategy: scan main.log for ``=== TURN <subtype> (Yms) ===`` markers
    (written by ``format_event`` at the end of every turn). Lines AFTER the
    latest marker = the current in-flight turn. Lines BETWEEN the two latest
    markers = the just-completed turn. ``RUNTIME START`` / ``SESSION INIT``
    are NOT turn boundaries — they sit at the top of turn 1.

    Args:
        pair_name: The pair whose log to read.
        task_finished: When True (task in terminal state), always pick the
            most-recently-COMPLETED turn (between the two latest markers).
            Ignores any stray post-completion stream events that may have
            trickled into main.log after the result event. When False (task
            still running), prefer the current in-flight turn (lines after
            the latest marker), falling back to the just-completed turn if
            there are no fresh lines yet.
    """
    log_path = runtime_mod.logs_dir() / pair_name / "main.log"
    if not log_path.exists():
        return [], "no main.log yet"
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = [l.rstrip("\n") for l in f.readlines()]
    except Exception:
        return [], "log read error"
    import re as _re
    turn_marker_re = _re.compile(r"=== TURN .* \(\d+ms\) ===")
    marker_indices = [i for i, l in enumerate(all_lines) if turn_marker_re.search(l)]
    if not marker_indices:
        # No turn has completed yet — everything in the log is the first in-flight turn
        return all_lines, "first turn (not yet completed)"
    last_marker = marker_indices[-1]

    if task_finished:
        # Terminal state: show the most-recently-completed turn (between the
        # two latest markers, or from start to the only marker). Stray events
        # after the latest marker are ignored.
        prev_marker = marker_indices[-2] if len(marker_indices) >= 2 else -1
        return all_lines[prev_marker + 1: last_marker + 1], "most recently completed turn"

    # Task still running: prefer the current in-flight turn (anything after
    # the latest marker), falling back to the last completed turn if nothing
    # has been logged yet for the new turn.
    if last_marker < len(all_lines) - 1:
        return all_lines[last_marker + 1:], "current in-flight turn"
    prev_marker = marker_indices[-2] if len(marker_indices) >= 2 else -1
    return all_lines[prev_marker + 1: last_marker + 1], "most recently completed turn"


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_poll(task_id: str, with_turn_log: bool = False, verbose: bool = False) -> str:
    """Inspect a specific async task — works for ALL statuses (running, done,
    failed, stopped). **This is the right tool whenever you have a task_id**,
    regardless of whether the task is still in flight or has already finished.

    Default returns a brief status line:
      - running   → "running for Xs on pair 'name' (started ...)" + hang warning if idle >120s
      - done      → the pair's full response text with footer (ctx %, model, duration, log refs)
      - failed    → "failed: <error>"
      - stopped   → "stopped: stopped by pair_stop"

    For richer inspection, pass ``with_turn_log=True`` to also see the FULL
    log of the relevant turn — works for any status:
      - running   → the current in-flight turn (everything since the user
        message: thinking, ``[T-N] tool_use``, ``tool_result``, etc.)
      - done/failed/stopped → the just-completed turn's log

    Each tool_use carries its ``[T-N]`` tag, so call
    ``pair_tool_detail(<pair>, 'T-N')`` to drill into any specific call.

    **When to use pair_poll vs pair_transcript**: ``pair_poll`` is task-scoped
    — give it a task_id and you get exactly that task's content. Use it for
    "what did this send produce?" or "what is this in-flight task doing?".
    Use ``pair_transcript`` only when browsing broader conversation history
    across multiple turns where you don't have a specific task_id in mind.

    For arbitrary line ranges or earlier turns, use ``pair_log`` directly.

    Args:
        task_id: Async task ID from ``pair_send_async`` (or a sync-degraded
            ``pair_send``). Prefix is accepted as long as it's unique.
        with_turn_log: If True, append the current or just-completed turn's
            log lines. Default False (quick status only — doesn't flood context).
        verbose: If True, return the full JSON ``AsyncTaskState``.
    """
    state = async_tasks.load_task(task_id)
    if state is None:
        # Maybe the caller passed an 8-char prefix from pair_status output —
        # try to resolve. Reject if ambiguous (multiple matches).
        candidates = async_tasks.find_task_by_prefix(task_id)
        if len(candidates) == 1:
            state = async_tasks.load_task(candidates[0])
            task_id = candidates[0]
        elif len(candidates) > 1:
            raise PairError(
                f"Task ID prefix '{task_id}' is ambiguous; matches {len(candidates)} "
                f"tasks: {', '.join(c[:12] for c in candidates[:5])}. Provide more characters."
            )
        else:
            raise PairError(f"No async task with id '{task_id}'.")
    if verbose:
        return _verbose_dump(state)

    # Build the headline status
    if state.status == "running":
        dur_s = (datetime.utcnow() - state.started_at).total_seconds()
        headline = (
            f"running for {dur_s:.0f}s on pair '{state.pair_name}' "
            f"(started {_fmt_local(state.started_at)})"
        )
    elif state.status == "failed":
        headline = f"failed: {state.error}"
    elif state.status == "stopped":
        headline = f"stopped: {state.error or 'stopped by pair_stop'}"
    elif state.status == "done":
        headline = _fmt_send_result(state.result) if state.result else "done (no result captured)"
    else:
        headline = f"unknown status: {state.status}"

    lines = [headline]

    # Auto hang-warning for running tasks — always shown, even without with_turn_log
    if state.status == "running":
        runtime_obj = runtime_mod.registry().get_or_none(state.pair_name)
        if runtime_obj is not None and runtime_obj._last_log_activity_at is not None:  # noqa: SLF001
            idle_s = (datetime.utcnow() - runtime_obj._last_log_activity_at).total_seconds()  # noqa: SLF001
            if idle_s > 120:
                lines.append(
                    f"  ⚠ no log activity for {idle_s:.0f}s — pair may be hung. "
                    f"Re-poll with with_turn_log=True to inspect, then pair_stop if truly stuck."
                )
            elif idle_s > 30:
                lines.append(f"  (last log activity {idle_s:.0f}s ago — still working)")

    # Opt-in turn log
    if with_turn_log:
        # For terminal tasks, prefer the SendResult's recorded log line range
        # (which precisely scopes to THIS task's turn). Falling back to the
        # "most recently completed turn" heuristic would otherwise return the
        # latest turn in main.log — wrong when polling an older task_id after
        # newer turns have been written by other sends.
        turn_lines: list[str] = []
        descriptor = ""
        result_log_start = (
            getattr(state.result, "log_line_start", None) if state.result else None
        )
        result_log_end = (
            getattr(state.result, "log_line_end", None) if state.result else None
        )
        if (
            state.status != "running"
            and result_log_start is not None
            and result_log_end is not None
        ):
            main_log = runtime_mod.logs_dir() / state.pair_name / "main.log"
            if main_log.exists():
                try:
                    with open(main_log, "r", encoding="utf-8") as f:
                        all_lines = [l.rstrip("\n") for l in f.readlines()]
                    # log_line_start / log_line_end are 1-indexed inclusive
                    turn_lines = all_lines[result_log_start - 1 : result_log_end]
                    descriptor = f"this task's turn (main.log:{result_log_start}-{result_log_end})"
                except Exception:
                    turn_lines = []
        if not turn_lines:
            # Fallback to the global heuristic (running tasks, or terminal tasks
            # whose SendResult is missing — e.g. failed/stopped before result event).
            turn_lines, descriptor = _read_current_or_last_turn_log(
                state.pair_name,
                task_finished=(state.status != "running"),
            )
        if turn_lines:
            lines.append(f"  --- {descriptor} ({len(turn_lines)} log lines) ---")
            lines.extend(f"    {l}" for l in turn_lines)
            lines.append(
                f"  -> for the full input/output of any [T-N] above, "
                f"call pair_tool_detail('{state.pair_name}', 'T-N')."
            )
        else:
            lines.append(f"  ({descriptor})")

    return "\n".join(lines)


# ============================================================================
# Inspection
# ============================================================================

@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_list(verbose: bool = False) -> str:
    """List all registered pairs.

    Default: one line per pair. Pass verbose=True for the full JSON list.
    """
    reg = reg_mod.load()
    if not reg.pairs:
        return "(no pairs registered)"
    if verbose:
        items = [
            PairListItem(
                name=s.name, purpose=s.purpose, model=s.model, backend=s.backend,
                last_active_at=s.last_active_at, turn_count=s.turn_count,
            ).model_dump(mode="json")
            for s in reg.pairs.values()
        ]
        return json.dumps({"pairs": items}, indent=2, default=str)
    lines = []
    for s in reg.pairs.values():
        last = _fmt_local(s.last_active_at, "%H:%M")
        purpose = f" - {s.purpose}" if s.purpose else ""
        lines.append(f"  {s.name} ({s.model}, {s.turn_count} turns, last {last}){purpose}")
    # Defaults header — surfaces what NEW pairs would inherit so agents picking
    # up mid-session can see the configured state at a glance. Cheap; no I/O
    # beyond the defaults.json read (cached by OS, sub-millisecond).
    defaults = settings_mod.load_defaults()
    defaults_summary_parts = []
    for field in ("model", "effort", "permission_mode", "persistent"):
        v = getattr(defaults, field)
        if v is not None:
            defaults_summary_parts.append(f"{field}={v}")
    if defaults_summary_parts:
        defaults_line = f"  defaults for new pairs: {', '.join(defaults_summary_parts)} (see pair_settings_get for full)"
    else:
        defaults_line = "  defaults for new pairs: (none configured — using hardcoded fallbacks; see pair_settings_get)"
    return (
        f"{len(reg.pairs)} pair{'s' if len(reg.pairs) != 1 else ''}:\n"
        + "\n".join(lines)
        + "\n" + defaults_line
    )


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_info(name: str, verbose: bool = False) -> str:
    """Get full configuration + stats for one pair.

    Default: a few lines of summary. Pass verbose=True for the full JSON.
    """
    spec = reg_mod.get_pair(name)
    adapter = _adapter_for(spec)
    tpath = adapter.transcript_path(spec)
    info = PairInfo(
        **spec.model_dump(),
        transcript_path=str(tpath) if tpath else None,
        transcript_exists=tpath is not None and tpath.exists(),
    )
    if verbose:
        return _verbose_dump(info)
    return (
        f"{name}:\n"
        f"  session: {spec.session_id} (transcript: {'ok' if info.transcript_exists else 'MISSING'})\n"
        f"  model: {spec.model}, effort: {spec.effort}, permissions: {spec.permission_mode}\n"
        f"  turns: {spec.turn_count}, last active: {_fmt_local(spec.last_active_at)}\n"
        f"  cwd: {spec.cwd or '(default)'}\n"
        f"  purpose: {spec.purpose or '(none)'}"
    )


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_transcript(
    name: str,
    last_n: int = 10,
    max_content_chars: int = 0,
    verbose: bool = False,
) -> str:
    """Browse the last N user/assistant turns of a pair's overall conversation.

    **When to use which content tool**:
      - You have a specific ``task_id`` (from pair_send / pair_send_async /
        pair_status) and want THAT task's content → use ``pair_poll(task_id,
        with_turn_log=True)`` instead. It's task-scoped and works for any
        status (running, done, failed, stopped).
      - You want to browse broader conversation history across many turns
        without a specific task_id in mind → **this tool**.
      - You want raw main.log lines (arbitrary slicing) → ``pair_log``.

    Default returns FULL text content of user + assistant turns. Tool_use
    blocks are summarized as ``[+N tool_uses]`` (use ``pair_tool_detail`` for
    full tool input/output). Thinking blocks are truncated at 200c (reasoning,
    not the pair's actual reply).

    Args:
        name: The pair.
        last_n: How many recent turns to return. Default 10.
        max_content_chars: 0 (default) = no truncation on text content. Pass a
            positive integer to cap long replies. Useful when scrolling a large
            transcript and you only want a preview.
        verbose: If True, return the full JSON (with tool_uses, timestamps).
    """
    spec = reg_mod.get_pair(name)
    adapter = _adapter_for(spec)
    tpath = adapter.transcript_path(spec)
    if not tpath:
        return "(no transcript found)"
    turns = transcript_mod.tail_turns(tpath, last_n=last_n)
    if not turns:
        return "(no turns)"
    if verbose:
        return json.dumps({"turns": turns}, indent=2, default=str)
    out = []
    for t in turns:
        role = t.get("role", "?")
        content = (t.get("content") or "").strip()
        tu = t.get("tool_uses") or []
        tu_summary = f" [+{len(tu)} tool_use{'s' if len(tu) != 1 else ''}]" if tu else ""
        if max_content_chars > 0 and len(content) > max_content_chars:
            content = content[:max_content_chars] + "…"
        out.append(f"[{role}]{tu_summary} {content}")
    return "\n\n".join(out)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_actions(name: str | None = None, verbose: bool = False) -> str:
    """List curated MCP actions; if `name` given, also probe pair's available
    slash commands and agents (via stream-json init) and mark each ✓/✗ against
    the pair's allowed_invocations list.
    """
    out: dict[str, Any] = {"actions": _PAIR_ACTIONS}
    spec = None
    if name is not None:
        spec = reg_mod.get_pair(name)
        adapter = _adapter_for(spec)
        try:
            events = adapter._run_stream_json(spec, ["/context"], timeout_seconds=30)  # type: ignore[attr-defined]
            for ev in events:
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    out["pair_skills"] = ev.get("slash_commands") or []
                    out["pair_agents"] = ev.get("agents") or []
                    break
        except Exception as e:
            out["pair_skills_error"] = str(e)
        # Surface the allow-list so the agent can see what the structured invoke
        # channel (pair_invoke) will accept. None = allow all; [] = deny all.
        out["allowed_invocations"] = spec.allowed_invocations
    if verbose:
        return json.dumps(out, indent=2, default=str)
    lines = ["Available actions:"]
    for k, v in out["actions"].items():
        lines.append(f"  {k}: {v}")
    if "pair_skills" in out:
        skills = out["pair_skills"]
        # When an allow-list is set, mark each skill ✓/✗ against pair_invoke's
        # actual filter so the agent knows at a glance what's reachable via the
        # structured channel.
        if spec is not None and spec.allowed_invocations is not None:
            allow = spec.allowed_invocations
            marked = [
                f"{'✓' if _invocation_allowed(s, allow) else '✗'}{s}"
                for s in skills
            ]
            allow_repr = "[] (deny all)" if not allow else repr(allow)
            lines.append(
                f"\nPair '{name}' skills ({len(skills)}, allow-list {allow_repr}): "
                + ", ".join(marked)
            )
        else:
            lines.append(f"\nPair '{name}' skills ({len(skills)}, allow-list: unset → allow all): "
                         + ", ".join(skills))
    if "pair_agents" in out:
        agents = out["pair_agents"]
        lines.append(f"Pair '{name}' agents ({len(agents)}): " + ", ".join(agents))
    if "pair_skills_error" in out:
        lines.append(f"(skills probe failed: {out['pair_skills_error']})")
    return "\n".join(lines)


# ============================================================================
# Mutation
# ============================================================================

@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def pair_update(
    name: str,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    purpose: str | None = None,
    allowed_tools: str | list[str] | None = None,
    allowed_invocations: str | list[str] | None = None,
    cwd: str | None = None,
    extra_dirs: str | list[str] | None = None,
    verbose: bool = False,
) -> str:
    """Update mutable settings of an existing pair (per-pair, not defaults —
    use ``pair_settings_set`` for user-wide defaults).

    Three field categories with different propagation semantics — see README
    "Mid-flight config changes":
      - **Per-send** (``model``/``effort``/``permission_mode``): registry write
        + runtime eviction → next ``pair_send`` respawns with new values.
      - **Server-side** (``allowed_invocations``): MCP-layer only, no eviction;
        takes effect on next ``pair_invoke``. Pass ``[]`` for lockdown.
      - **Pinned-at-create** (``allowed_tools``/``mcp_whitelist``/
        ``system_prompt_append``): registry updated but only takes effect after
        ``pair_clear`` (rotates session). ``cwd``/``extra_dirs`` take effect on
        next runtime spawn after eviction; ``cwd`` change also moves the session
        JSONL across project dirs (rejected with recovery hint if move fails).
    """
    # Hold the cross-process lock for the whole update: cwd-move + registry write
    # + runtime eviction must all be atomic against other processes' pair_send.
    transparency_msgs: list[str] = []
    try:
        with _with_pair_lock(name):
            fields: dict[str, Any] = {}
            if model is not None:
                fields["model"] = model
            if effort is not None:
                fields["effort"] = effort
            if permission_mode is not None:
                fields["permission_mode"] = permission_mode
            if purpose is not None:
                fields["purpose"] = purpose
            if allowed_tools is not None:
                fields["allowed_tools"] = _coerce_to_str_list(allowed_tools)
            if allowed_invocations is not None:
                # Server-side allow-list. preserve_empty=True keeps `[]` as `[]`
                # so passing an explicit empty list locks down the pair (deny all)
                # instead of silently collapsing to None (allow all).
                fields["allowed_invocations"] = _coerce_to_str_list(
                    allowed_invocations, preserve_empty=True
                )
            if extra_dirs is not None:
                fields["extra_dirs"] = _normalize_path_list(_coerce_to_str_list(extra_dirs))

            # Apply effort coercion against the resolved model. We do this
            # explicitly here (in addition to PairSpec's model_validator) because
            # update_pair uses ``model_copy(update=fields)`` which does NOT run
            # Pydantic validators by default. Without this, you could get a
            # haiku pair with effort='xhigh' stored in the registry, and the
            # mismatch would only surface at runtime.start() when --effort fails.
            #
            # Effective model for coercion = new model if changing, else current.
            if "model" in fields or "effort" in fields:
                old_spec = reg_mod.get_pair(name)
                effective_model = fields.get("model", old_spec.model)

                # Auto-reset effort when ONLY model is changing — mirrors
                # pair_settings_set's UX so the same single-field-change flow
                # works the same way at both layers. Without this, switching
                # a haiku pair to opus alone would leave effort=None, and the
                # next send would run opus without --effort (defaults to whatever
                # CLI uses bare). Surface the auto-reset in transparency_msgs.
                if "model" in fields and "effort" not in fields:
                    new_default_effort = default_effort_for_model(effective_model)
                    if new_default_effort != old_spec.effort:
                        fields["effort"] = new_default_effort
                        eff_display = (
                            new_default_effort if new_default_effort is not None else "none"
                        )
                        transparency_msgs.append(
                            f"effort auto-reset to '{eff_display}' for new model "
                            f"'{effective_model}' (was {old_spec.effort!r})."
                        )

                effective_effort = fields.get("effort", old_spec.effort)
                coerced, msg = coerce_effort_for_model(effective_model, effective_effort)
                if msg:
                    transparency_msgs.append(msg)
                # Always write coerced effort to registry if it changed at all
                # (covers the case: model changes alone → coerce existing effort
                # against new model capability).
                if coerced != old_spec.effort or "effort" in fields:
                    fields["effort"] = coerced

            # cwd is special: changing it requires moving the session JSONL between project dirs
            # because --resume looks up the JSONL under the new cwd's encoded project path.
            cwd_move_msg: str | None = None
            if cwd is not None:
                cwd_norm = _normalize_path(cwd)
                old_spec = reg_mod.get_pair(name)
                if cwd_norm and cwd_norm != old_spec.cwd:
                    try:
                        cwd_move_msg = _move_session_jsonl_for_cwd_change(old_spec, cwd_norm)
                    except Exception as e:
                        raise PairError(
                            f"Could not change cwd for pair '{name}': {e}\n"
                            f"To change cwd cleanly, run pair_clear (rotates session_id, loses history) "
                            f"or pair_forget + pair_create (fresh start)."
                        ) from e
                    fields["cwd"] = cwd_norm

            if not fields:
                spec = reg_mod.get_pair(name)
                return f"No fields to update for '{name}'."
            spec = reg_mod.update_pair(name, **fields)
            # Material config changes invalidate any live runtime — next send will re-spawn
            if any(k in fields for k in ("model", "permission_mode", "cwd", "extra_dirs", "allowed_tools")):
                try:
                    runtime_mod.registry().evict(name)
                except Exception:
                    pass
            # Operational warning when pinned-at-create fields change: the
            # underlying claude --resume subprocess was started with the OLD
            # values. The registry is now correct but the existing session
            # keeps using its startup config until pair_clear rotates it. This
            # is the one place agents reliably make the wrong assumption — flag
            # it inline so they don't have to dig into the README.
            pinned_at_create = {"allowed_tools", "system_prompt_append", "mcp_whitelist"}
            pinned_changed = sorted(pinned_at_create & fields.keys())
            if pinned_changed:
                pinned_warning = (
                    f"Note: {', '.join(pinned_changed)} pinned to existing session. "
                    f"Run pair_clear('{name}') for the new value(s) to take effect "
                    f"(rotates session_id; old transcript archived)."
                )
            else:
                pinned_warning = None
            update_msgs = list(transparency_msgs)
            if cwd_move_msg:
                update_msgs.append(cwd_move_msg)
            if pinned_warning:
                update_msgs.append(pinned_warning)
            if verbose:
                return _verbose_dump_with_msgs(spec, update_msgs)
            msg = f"Updated '{name}': " + ", ".join(f"{k}={v!r}" for k, v in fields.items())
            for tmsg in transparency_msgs:
                msg += f"\n  {tmsg}"
            if cwd_move_msg:
                msg += f"\n  {cwd_move_msg}"
            if pinned_warning:
                msg += f"\n  {pinned_warning}"
            return msg
    except FileLockTimeout:
        raise PairError(f"Pair '{name}' is busy in another process; could not acquire lock to update.")


def _move_session_jsonl_for_cwd_change(old_spec: PairSpec, new_cwd: str) -> str:
    """Move the pair's session JSONL from the old cwd's project dir to the new one.

    Returns a one-line status message. Raises if the move can't be done safely.

    Edge cases handled:
    - Old JSONL doesn't exist (session not yet spawned) → no-op, just returns OK.
    - Live runtime holding the JSONL open → caller is expected to evict before/after.
    - Target directory doesn't exist → mkdir.
    - Target file already exists with same name → refuse (don't clobber).
    - Same encoded path (different cwd strings encode identically) → no-op.
    """
    from pathlib import Path as _P
    if not old_spec.cwd:
        raise ValueError("pair has no cwd set; nothing to move")
    old_encoded = _encode_cwd_for_project(old_spec.cwd)
    new_encoded = _encode_cwd_for_project(new_cwd)
    if old_encoded == new_encoded:
        return f"cwd encoded to same project path; no JSONL move needed"

    # First evict any live runtime so it's not holding the file open
    try:
        runtime_mod.registry().evict(old_spec.name)
    except Exception:
        pass

    home = _P(reg_mod.claude_home())
    src = home / "projects" / old_encoded / f"{old_spec.session_id}.jsonl"
    dst = home / "projects" / new_encoded / f"{old_spec.session_id}.jsonl"

    if not src.exists():
        # Session not spawned yet (or already moved); just ensure new project dir exists
        dst.parent.mkdir(parents=True, exist_ok=True)
        return f"no JSONL at {src} to move (session not spawned or already moved); new dir prepared"

    if dst.exists():
        raise FileExistsError(
            f"target already exists: {dst}. "
            f"This shouldn't normally happen with UUID-named files."
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    # os.replace is atomic on the same filesystem (Windows + same drive = atomic)
    os.replace(src, dst)

    # Also move the per-session directory (which holds sub-agent JSONLs in subagents/)
    # if it exists. This keeps the sub-agent transcripts attached to the session.
    src_session_dir = home / "projects" / old_encoded / old_spec.session_id
    dst_session_dir = home / "projects" / new_encoded / old_spec.session_id
    moved_session_dir = False
    if src_session_dir.exists():
        try:
            shutil.move(str(src_session_dir), str(dst_session_dir))
            moved_session_dir = True
        except Exception as e:
            # Don't fail the whole update — JSONL moved, just sub-agent dir is stuck.
            # Log to caller via the return message.
            return (f"moved JSONL: {src.parent} -> {dst.parent} "
                    f"(WARN: could not move session dir {src_session_dir} -> {dst_session_dir}: {e}; "
                    f"sub-agent transcripts remain at the old location)")

    extra = " + session dir (with sub-agent transcripts)" if moved_session_dir else ""
    return f"moved JSONL{extra}: {src.parent} -> {dst.parent}"


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def pair_persist(name: str, on: bool = True) -> str:
    """Toggle the pair's persistent flag.

    When `on=True`, the pair's underlying claude subprocess is exempt from the 10-minute
    idle eviction — useful for pairs you'll chat with frequently throughout a session.
    When `on=False`, the pair returns to lazy-spawn + 10-min idle eviction (default).

    Pairs always start in lazy mode unless created with `persistent=True`. This toggle
    flips that flag without restarting the runtime.
    """
    spec = reg_mod.update_pair(name, persistent=on)
    state = "persistent (no idle eviction)" if on else "lazy (10-min idle eviction)"
    return f"Pair '{name}' is now {state}"


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_tool_detail(
    name: str,
    tool_id: str,
    subagent: str | None = None,
    max_chars: int = 50_000,
) -> str:
    """Return full input + result for one tool_use, identified by T-N tag.

    Resolution: T-N → tool_use_id is read from the index file (`main.idx.json` or
    `subagent-N-<type>.idx.json`), which is written incrementally as tool_uses are
    observed. This is robust to JSONL gaps — failed/cancelled tool_uses that never
    persisted to JSONL are still resolvable via the index, and the tool will report
    "issued live but not in JSONL" cleanly rather than mis-mapping to a different N.

    Args:
        name: The pair.
        tool_id: "T-12" or "12".
        subagent: If given (e.g. "1" or "1-Explore"), reads from that sub-agent's
            log+JSONL+index. Default: main pair session.
        max_chars: Cap on returned content per side (input/result). Default 50,000.
    """
    spec = reg_mod.get_pair(name)
    log_dir = reg_mod.logs_dir() / name

    # Resolve the index file path
    if subagent is None:
        index_path = log_dir / "main.idx.json"
        jsonl_path = _resolve_main_jsonl_path(spec)
        scope_label = f"main session {spec.session_id}"
    else:
        # Match the same logic pair_log uses to find the sub-agent's files
        if "-" in subagent:
            log_file = log_dir / f"subagent-{subagent}.log"
            index_path = log_dir / f"subagent-{subagent}.idx.json"
        else:
            matches = sorted(log_dir.glob(f"subagent-{subagent}-*.log"))
            if not matches:
                raise PairError(f"no sub-agent log #{subagent} for pair '{name}'")
            log_file = matches[0]
            index_path = log_file.with_suffix(".idx.json")
        jsonl_path = _resolve_subagent_jsonl_path(spec, subagent)
        scope_label = f"sub-agent {subagent}"

    # Look up T-N in the index (authoritative source)
    entry = runtime_mod.lookup_tool_in_index(index_path, tool_id)
    if entry is None:
        # Fall back: scan the index file even without tracking, for legacy logs
        if not index_path.exists():
            raise PairError(
                f"No index file for {scope_label} at {index_path}. This pair predates "
                f"index tracking — pair_clear+pair_create to enable it for new sessions."
            )
        raise PairError(
            f"No tool_use {tool_id} recorded in index for {scope_label}. "
            f"Available IDs in index: see {index_path}"
        )

    tool_use_id = entry.get("tool_use_id") or ""
    tool_name_from_index = entry.get("tool_name", "?")
    issued_at = entry.get("ts", "?")

    if not tool_use_id:
        return (f"=== {tool_id} in {scope_label} ===\n"
                f"Index entry has no tool_use_id (likely an extraction artifact).\n"
                f"Tool name (from index): {tool_name_from_index}\n"
                f"Issued at: {issued_at}")

    # Look up the tool_use + tool_result in the JSONL by canonical id
    if jsonl_path is None or not jsonl_path.exists():
        return (f"=== {tool_id} in {scope_label} ===\n"
                f"Tool: {tool_name_from_index}\n"
                f"tool_use_id: {tool_use_id}\n"
                f"Issued at: {issued_at}\n"
                f"\n(JSONL not found at {jsonl_path} — content unavailable)")

    tool_use, tool_result_block, is_err = runtime_mod.find_tool_use_in_jsonl(
        jsonl_path, tool_use_id
    )

    out_parts: list[str] = [f"=== {tool_id} in {scope_label} ==="]
    out_parts.append(f"Tool: {tool_use.get('name') if tool_use else tool_name_from_index}")
    out_parts.append(f"tool_use_id: {tool_use_id}")
    out_parts.append(f"Issued at: {issued_at}")

    if tool_use is None:
        # Fallback: this might be a sub-agent's tool_use that leaked into the parent stream.
        # Scan all sub-agent index files for the same tool_use_id; fetch from there if found.
        if subagent is None:
            for sub_idx in sorted(log_dir.glob("subagent-*-*.idx.json")):
                try:
                    sub_data = json.loads(sub_idx.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(sub_data, dict):
                    continue
                for sub_tn, sub_entry in sub_data.items():
                    if isinstance(sub_entry, dict) and sub_entry.get("tool_use_id") == tool_use_id:
                        # Found in this sub-agent's index — fetch its JSONL
                        sub_log_name = sub_idx.name.removesuffix(".idx.json")
                        sub_jsonl = _resolve_subagent_jsonl_path(spec, sub_log_name.removeprefix("subagent-"))
                        if sub_jsonl and sub_jsonl.exists():
                            su, sr, serr = runtime_mod.find_tool_use_in_jsonl(sub_jsonl, tool_use_id)
                            if su is not None:
                                out_parts.append("")
                                out_parts.append(f"↳ This tool_use is actually from {sub_log_name} ({sub_tn} in that log).")
                                out_parts.append(f"  Pulled detail from sub-agent JSONL: {sub_jsonl}")
                                out_parts.append("\nInput:")
                                inp = su.get("input") or {}
                                inp_str = json.dumps(inp, indent=2, default=str)
                                if len(inp_str) > max_chars:
                                    inp_str = inp_str[:max_chars] + f"\n... [truncated; full input is {len(inp_str)} chars]"
                                out_parts.append(inp_str)
                                out_parts.append(f"\nResult ({'ERROR' if serr else 'ok'}):")
                                if sr is None:
                                    out_parts.append("(no tool_result in sub-agent JSONL)")
                                else:
                                    rc = sr.get("content")
                                    res_str = json.dumps(rc, indent=2, default=str) if isinstance(rc, list) else str(rc)
                                    if len(res_str) > max_chars:
                                        res_str = res_str[:max_chars] + f"\n... [truncated; full result is {len(res_str)} chars]"
                                    out_parts.append(res_str)
                                return "\n".join(out_parts)
        out_parts.append("")
        out_parts.append("⚠ This tool_use was emitted live (logged in main.log) but is NOT in the parent JSONL")
        out_parts.append("  and we couldn't find it in any sub-agent log either. Likely the tool call was")
        out_parts.append("  cancelled or errored before persistence.")
        return "\n".join(out_parts)

    inp = tool_use.get("input") or {}
    inp_str = json.dumps(inp, indent=2, default=str)
    if len(inp_str) > max_chars:
        inp_str = inp_str[:max_chars] + f"\n... [truncated; full input is {len(inp_str)} chars]"
    out_parts.append("\nInput:")
    out_parts.append(inp_str)
    out_parts.append(f"\nResult ({'ERROR' if is_err else 'ok'}):")
    if tool_result_block is None:
        out_parts.append("(no tool_result found in JSONL — likely still running or cancelled)")
    else:
        tool_result = tool_result_block.get("content")
        if isinstance(tool_result, list):
            res_str = json.dumps(tool_result, indent=2, default=str)
        else:
            res_str = str(tool_result)
        if len(res_str) > max_chars:
            res_str = res_str[:max_chars] + f"\n... [truncated; full result is {len(res_str)} chars]"
        out_parts.append(res_str)
    return "\n".join(out_parts)


def _resolve_main_jsonl_path(spec: PairSpec) -> Path | None:
    if not spec.cwd:
        return None
    encoded = _encode_cwd_for_project(spec.cwd)
    return reg_mod.claude_home() / "projects" / encoded / f"{spec.session_id}.jsonl"


def _resolve_subagent_jsonl_path(spec: PairSpec, subagent_ref: str) -> Path | None:
    """Resolve subagent JSONL via the sub-agent log file's header (which records the path).

    The sub-agent log (subagent-N-<type>.log) starts with a line like:
        === source jsonl: <path> ===
    We extract that path so this works even after cwd moves (the header is captured
    at extraction time and doesn't drift).
    """
    log_dir = reg_mod.logs_dir() / spec.name
    if "-" in subagent_ref:
        log_file = log_dir / f"subagent-{subagent_ref}.log"
    else:
        matches = sorted(log_dir.glob(f"subagent-{subagent_ref}-*.log"))
        if not matches:
            return None
        log_file = matches[0]
    if not log_file.exists():
        return None
    # Read header for source jsonl path
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()[:5]:
        if "source jsonl:" in line:
            # `=== source jsonl: <path> ===`
            path_str = line.split("source jsonl:", 1)[1].rsplit("===", 1)[0].strip()
            return Path(path_str)
    return None


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_log(
    name: str,
    start: int | None = None,
    end: int | None = None,
    last_n: int | None = 50,
    subagent: str | None = None,
) -> str:
    """Read a slice of a pair's main.log (or one of its sub-agent logs).

    Args:
        name: The pair.
        start, end: 1-indexed inclusive line range. If both given, returns those lines.
        last_n: If start/end omitted, returns the last N lines (default 50).
        subagent: If given (e.g. "1" or "2-Explore"), reads from
            `~/.claude/pairs/logs/<name>/subagent-<subagent>.log` instead of main.log.
            Use just the leading number to match by sub-agent index.

    Returns the requested slice as plain text. Use after pair_send to inspect what
    the pair (or one of its sub-agents) actually did this turn — pair_send's footer
    includes the line range it produced (e.g. `log main.log:47-63`).
    """
    log_dir = reg_mod.logs_dir() / name
    if not log_dir.exists():
        raise PairError(f"No log folder for pair '{name}' yet. Send a message first to create one.")
    if subagent is None:
        target = log_dir / "main.log"
    else:
        # Allow either "1" (match by index) or full filename suffix like "1-Explore"
        if "-" in subagent:
            target = log_dir / f"subagent-{subagent}.log"
        else:
            matches = sorted(log_dir.glob(f"subagent-{subagent}-*.log"))
            if not matches:
                raise PairError(
                    f"No sub-agent log #{subagent} for pair '{name}'. "
                    f"Available: {[p.name for p in log_dir.glob('subagent-*.log')]}"
                )
            target = matches[0]
    if not target.exists():
        raise PairError(f"Log file not found: {target}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    if start is not None and end is not None:
        # 1-indexed inclusive; clamp
        s = max(1, start) - 1
        e = min(total, end)
        slice_ = lines[s:e]
        header = f"=== {target.name} lines {s+1}-{e} of {total} ==="
    else:
        n = last_n or 50
        slice_ = lines[-n:]
        first_line = max(1, total - n + 1)
        header = f"=== {target.name} last {len(slice_)} lines (lines {first_line}-{total} of {total}) ==="
    return header + "\n" + "\n".join(slice_)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_tail(name: str) -> str:
    """Get the live activity log paths for a pair and shell commands to tail them.

    Each pair has a log folder at `~/.claude/pairs/logs/<name>/` containing:
      - main.log: pair's own thinking, tool_use, text, turn boundaries (live-streamed)
      - subagent-N-<type>.log: each sub-agent's activity (one-shot extracted post-completion)

    Tail main.log in a separate terminal to watch the pair work in real time.
    Sub-agent logs are written when each sub-agent finishes (use pair_log for slices).
    """
    spec = reg_mod.get_pair(name)
    log_dir = reg_mod.logs_dir() / name
    main_log = log_dir / "main.log"
    main_exists = "exists" if main_log.exists() else "not yet created (first send creates it)"
    sub_logs = sorted(log_dir.glob("subagent-*.log")) if log_dir.exists() else []
    sub_summary = ""
    if sub_logs:
        sub_summary = "\nSub-agent logs (extracted post-completion):\n"
        for p in sub_logs:
            sub_summary += f"  {p}\n"
    return (
        f"Log folder for pair '{name}': {log_dir}\n"
        f"\n"
        f"main.log ({main_exists}):\n"
        f"  {main_log}\n\n"
        f"Tail main.log in PowerShell:\n"
        f"  Get-Content -Path '{main_log}' -Wait -Tail 50\n\n"
        f"Tail in bash/git-bash:\n"
        f"  tail -f -n 50 '{main_log}'\n\n"
        f"Or open a new terminal pre-tailing:\n"
        f"  Start-Process powershell -ArgumentList '-NoExit','-Command',\"Get-Content -Path '{main_log}' -Wait -Tail 50\""
        f"{sub_summary}"
    )


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_runtimes() -> str:
    """Inspect which pair runtimes are currently alive (subprocesses kept warm).

    Each entry shows: alive, started_at, last_activity, persistent flag, and model.
    Useful for understanding which pairs are paying the ~3s startup cost vs reusing
    a warm subprocess.
    """
    info = runtime_mod.registry().status()
    if not info:
        return "(no live runtimes)"
    lines = [f"{len(info)} live runtime{'s' if len(info) != 1 else ''}:"]
    for name, st in info.items():
        flag = " [persistent]" if st["persistent"] else ""
        lines.append(f"  {name}{flag}: {st['model']}, last_activity={_fmt_local(st['last_activity'])}, alive={st['alive']}")
    return "\n".join(lines)


# ============================================================================
# User-configurable defaults (v0.8.0+)
# ============================================================================

@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_settings_get(verbose: bool = False) -> str:
    """Show current settings: writable defaults + file paths + read-only env knobs.

    Three sections in the output:

    1. **Writable defaults** (mutable via ``pair_settings_set``): model, effort,
       permission_mode, persistent, extra_dirs. Each shows the configured value
       OR ``"(unset → hardcoded fallback X)"`` for fields you haven't set.
    2. **File paths**: locations of registry, defaults file, logs, async tasks,
       and per-pair session JSONLs root. Useful for direct inspection or backup.
    3. **Infra knobs** (env-var-only, READ-ONLY here): ``CLAUDE_PAIR_SYNC_CAP_SECONDS``,
       ``CLAUDE_HOME``, ``CLAUDE_PAIR_CLI_PATH``. Set these in your shell environment;
       this tool surfaces their effective values for transparency.

    Args:
        verbose: If True, return the full JSON dict instead of formatted text.
    """
    defaults = settings_mod.load_defaults()
    paths = {
        "claude_home": str(reg_mod.claude_home()),
        "pairs_dir": str(reg_mod.pairs_dir()),
        "registry": str(reg_mod.registry_path()),
        "defaults": str(settings_mod.defaults_path()),
        "logs_root": str(runtime_mod.logs_dir()),
        "async_tasks": str(reg_mod.async_dir()),
        "session_jsonls_root": str(reg_mod.claude_home() / "projects"),
    }
    env_knobs = {
        "CLAUDE_PAIR_SYNC_CAP_SECONDS": os.environ.get("CLAUDE_PAIR_SYNC_CAP_SECONDS", "(unset → 45)"),
        "CLAUDE_HOME": os.environ.get("CLAUDE_HOME", "(unset → ~/.claude)"),
        "CLAUDE_PAIR_CLI_PATH": os.environ.get("CLAUDE_PAIR_CLI_PATH", "(unset → shutil.which('claude'))"),
    }

    if verbose:
        return json.dumps({
            "writable_defaults": defaults.model_dump(),
            "hardcoded_fallbacks": _HARDCODED_DEFAULTS,
            "paths": paths,
            "env_knobs": env_knobs,
        }, indent=2)

    # Human-readable text view
    def _show(field: str, configured: Any, fallback: Any) -> str:
        if configured is None:
            return f"(unset → '{fallback}')"
        return repr(configured)

    lines = ["pair MCP settings:", "", "  Writable defaults (set via pair_settings_set):"]
    lines.append(f"    model           = {_show('model', defaults.model, _HARDCODED_DEFAULTS['model'])}")
    eff_fallback = "derived from model" if _HARDCODED_DEFAULTS["effort"] is None else _HARDCODED_DEFAULTS["effort"]
    lines.append(f"    effort          = {_show('effort', defaults.effort, eff_fallback)}")
    lines.append(f"    permission_mode = {_show('permission_mode', defaults.permission_mode, _HARDCODED_DEFAULTS['permission_mode'])}")
    lines.append(f"    persistent      = {_show('persistent', defaults.persistent, _HARDCODED_DEFAULTS['persistent'])}")
    lines.append(f"    extra_dirs      = {_show('extra_dirs', defaults.extra_dirs, _HARDCODED_DEFAULTS['extra_dirs'])}")
    lines.append(f"    allowed_invocations = {_show('allowed_invocations', defaults.allowed_invocations, 'None (allow all)')}")
    lines.append("")
    lines.append("  File paths:")
    for k, v in paths.items():
        lines.append(f"    {k:20} = {v}")
    lines.append("")
    lines.append("  Infra knobs (env vars; READ-ONLY from this tool — set in shell):")
    for k, v in env_knobs.items():
        lines.append(f"    {k:30} = {v}")
    lines.append("")
    lines.append("  Notes:")
    lines.append("    - model='match-parent' triggers detection from session JSONL")
    lines.append("    - bypassPermissions refused as default (foot-gun); pass per-call")
    lines.append("    - Effort coerced per model: Sonnet xhigh/max→high, Haiku any→None")
    lines.append("    - allowed_invocations=[] (deny-all) refused as default (foot-gun); pass per-pair")
    return "\n".join(lines)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def pair_settings_set(
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    persistent: bool | None = None,
    extra_dirs: str | list[str] | None = None,
    allowed_invocations: str | list[str] | None = None,
    verbose: bool = False,
) -> str:
    """Set per-user defaults for new pairs (writable subset of pair_settings_get).

    Per-call args ALWAYS override defaults — these only fill in fields you don't
    explicitly pass on ``pair_create``. Existing pairs unaffected; defaults apply
    only to NEW pairs. Pass any field to set; omit to leave unchanged. To clear,
    use ``pair_settings_reset`` or edit ``defaults.json`` by hand.

    Args:
        model: Default model alias / full name. ``"match-parent"`` triggers JSONL
            detection on each pair_create.
        effort: Coerced against model (Sonnet xhigh/max → high, Haiku any → None).
            If model is changing in the same call without explicit effort, effort
            auto-resets to the new model's default (Opus→xhigh, Sonnet→high,
            Haiku→None).
        permission_mode: ``bypassPermissions`` is REFUSED as a default (foot-gun:
            every new pair would silently lose guardrails). Pass per-pair instead.
        persistent: Default persistent flag for new pairs.
        extra_dirs: Default ``--add-dir`` paths for every new pair.
        allowed_invocations: Default ``pair_invoke`` allow-list (see ``pair_create``
            for syntax). ``[]`` REFUSED as a default (same foot-gun principle as
            bypassPermissions: silent deny-all on every fresh pair).
        verbose: If True, return full JSON + change messages.
    """
    # Build the kwargs dict with only the fields actually passed (None means
    # "don't touch this field"). Coerce list inputs the same way pair_create does.
    fields: dict = {}
    if model is not None:
        fields["model"] = model
    if effort is not None:
        fields["effort"] = effort
    if permission_mode is not None:
        fields["permission_mode"] = permission_mode
    if persistent is not None:
        fields["persistent"] = persistent
    if extra_dirs is not None:
        fields["extra_dirs"] = _normalize_path_list(_coerce_to_str_list(extra_dirs))
    if allowed_invocations is not None:
        # Don't path-normalize — these are fnmatch glob patterns over slash-command
        # names like "mcp__claude_ai_*", not filesystem paths. preserve_empty=True
        # so passing `[]` reaches the PairDefaults foot-gun guard (which raises
        # ValueError) instead of being silently collapsed to None.
        fields["allowed_invocations"] = _coerce_to_str_list(
            allowed_invocations, preserve_empty=True
        )

    if not fields:
        return "No fields passed — nothing to update. Use pair_settings_get() to see current values."

    try:
        new_defaults, change_msgs = settings_mod.update_defaults(**fields)
    except ValueError as e:
        # Catches: unknown field, bypassPermissions guard, Pydantic Literal mismatches
        raise PairError(str(e))

    if verbose:
        return json.dumps({
            "updated": new_defaults.model_dump(),
            "change_messages": change_msgs,
        }, indent=2, default=str)

    lines = ["Defaults updated:"]
    for k, v in fields.items():
        # Show the FINAL value (post-coercion) for the fields the user touched.
        final = getattr(new_defaults, k)
        if final == v:
            lines.append(f"  {k} = {final!r}")
        else:
            lines.append(f"  {k} = {final!r} (you passed {v!r}; coerced)")
    for msg in change_msgs:
        lines.append(f"  {msg}")
    lines.append("")
    lines.append("Affects only NEW pairs. Existing pairs untouched (use pair_update for those).")
    return "\n".join(lines)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def pair_settings_reset() -> str:
    """Remove ~/.claude/pairs/defaults.json entirely. Subsequent pair_create calls
    will use only the hardcoded fallbacks (Opus, xhigh, auto, persistent=False)."""
    settings_mod.reset_defaults()
    return "Defaults file removed. New pairs will use hardcoded fallbacks (Opus/xhigh/auto)."


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def pair_clear(name: str, archive_old: bool = True, verbose: bool = False) -> str:
    """Reset a pair's conversation history while preserving its config.

    Implementation: rotates to a new session_id and starts a fresh session with the same
    pinned config (system prompt, allowed_tools, MCP scope). Optionally archives the old
    transcript to ~/.claude/pairs/archive/. Note: /clear in the underlying CLI is a UI
    operation and does NOT actually wipe the JSONL — rotation is the correct primitive.

    Returns: {old_session_id, new_session_id, archived_to?}
    """
    try:
        with _with_pair_lock(name):
            spec = reg_mod.get_pair(name)
            adapter = _adapter_for(spec)
            old_sid = spec.session_id
            old_path = adapter.transcript_path(spec)

            # Tear down any live runtime — it's pinned to the old session_id
            try:
                runtime_mod.registry().evict(name)
            except Exception:
                pass

            new_sid = str(uuid.uuid4())
            new_spec = spec.model_copy(update={"session_id": new_sid, "turn_count": 0})
            # Initialize the new session by sending a minimal probe
            adapter.create(new_spec)
            reg_mod.update_pair(name, session_id=new_sid, turn_count=0, total_cost_usd=0.0,
                                last_active_at=datetime.utcnow())

            archived: str | None = None
            if archive_old and old_path and old_path.exists():
                ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
                dst = reg_mod.archive_dir() / f"{name}-{ts}-cleared.jsonl"
                shutil.copy2(old_path, dst)
                archived = str(dst)
            if verbose:
                return json.dumps({"old_session_id": old_sid, "new_session_id": new_sid,
                                   "archived_to": archived}, indent=2)
            msg = f"Cleared '{name}': {_short(old_sid)} → {_short(new_sid)}"
            if archived:
                msg += f" (archived to {archived})"
            return msg
    except FileLockTimeout:
        raise PairError(f"Pair '{name}' is busy in another process; could not acquire lock to clear.")


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def pair_compact(
    name: str,
    steering_prompt: str | None = None,
    timeout_seconds: int = 600,
    verbose: bool = False,
) -> str:
    """Compact a pair's conversation history via native /compact (stream-json).

    Args:
        name: Pair to compact.
        steering_prompt: Optional custom steering text. If omitted, uses Claude's default
            summarization. For high-quality compactions, focus the steering on:
            (1) the conversational arc and decisions made,
            (2) binding user preferences and rules,
            (3) in-flight work state.
            Defer technical detail to ``.md`` files in the project rather than restating
            it in the summary — the post-compaction agent can re-read those.
        timeout_seconds: Max wait. Default 600 (compaction can be slow on long sessions).
    """
    try:
        with _with_pair_lock(name, timeout_s=max(120.0, float(timeout_seconds))):
            spec = reg_mod.get_pair(name)
            # Compaction rewrites the session JSONL — any live runtime has stale state
            try:
                runtime_mod.registry().evict(name)
            except Exception:
                pass
            adapter = _adapter_for(spec)
            result = adapter.compact(spec, steering_prompt=steering_prompt, timeout_seconds=timeout_seconds)
            if verbose:
                return _verbose_dump(result)
            ratio = (result.post_tokens / result.pre_tokens * 100) if result.pre_tokens else 0
            return (f"Compacted '{name}': {result.pre_tokens:,} → {result.post_tokens:,} tokens "
                    f"({ratio:.1f}% retained, {result.duration_ms / 1000:.1f}s, trigger={result.trigger})")
    except FileLockTimeout:
        raise PairError(f"Pair '{name}' is busy in another process; could not acquire lock to compact.")


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
def pair_stop(
    name: str,
    force: bool = False,
    hard: bool = False,
    drain_queue: bool = False,
) -> str:
    """Cancel the pair's in-flight operation. UI-stop-button equivalent.

    By default, sends the claude CLI's in-band ``control_request: interrupt``
    over the runtime's stdin. The pair gracefully cancels the current turn
    (any mid-flight Bash sleep, model generation, sub-agent call), emits a
    ``subtype: error_during_execution`` result event, and **stays alive** for
    the next send. No process kill, no orphaned child processes — claude.exe
    cleans up its own tool subprocesses. The runtime is preserved; the pair
    is immediately ready for ``pair_send`` again.

    Args:
        name: Pair to stop.
        force: Skip the soft in-band interrupt and immediately tree-kill the
            runtime subprocess (``taskkill /F /T`` on Windows, ``killpg
            SIGKILL`` on POSIX). Use when the pair is wedged and doesn't
            respond to interrupt. The next ``pair_send`` will respawn the
            runtime from the JSONL (resumes from the partial turn).
        hard: After stopping, ALSO rotate the session ID via the same logic as
            ``pair_clear`` — the pair starts FRESH on next send instead of
            resuming from the partially-completed turn. Use when you want to
            throw away the in-flight work entirely.
        drain_queue: If multiple async tasks are queued FIFO for this pair (one
            running, others waiting on the cross-process lock), mark the queued
            ones as stopped too. Default False: only the running task is stopped.

    Returns a one-line summary of what happened (what tier fired, how many
    queued tasks drained, etc.).
    """
    # Sanity
    spec = reg_mod.get_pair(name)

    actions: list[str] = []
    runtime_obj = runtime_mod.registry().get_or_none(name)

    # Find any in-flight async task IDs for this pair BEFORE we tear anything down
    running_task_ids = async_tasks.list_running_task_ids_for_pair(name)
    for tid in running_task_ids:
        async_tasks.mark_task_stopped(tid)

    runtime_alive = runtime_obj is not None and runtime_obj.is_alive()
    has_inflight = len(running_task_ids) > 0

    # Soft path: only attempt the in-band interrupt if there's ACTUAL in-flight
    # work to cancel. Sending interrupt to an idle warm runtime just times out
    # at 3s waiting for a result event that never comes (no turn in progress).
    if runtime_alive and not force and has_inflight:
        soft_succeeded = runtime_obj.send_interrupt(wait_for_result_seconds=3.0)
        if soft_succeeded:
            actions.append("sent in-band interrupt (pair stays alive)")
        else:
            actions.append("in-band interrupt didn't ack within 3s — escalating to tree-kill")
            force = True

    # Force path: tree-kill the runtime. Skipped if no runtime is alive.
    if force and runtime_alive:
        try:
            runtime_mod.registry().evict(name)
            actions.append("tree-killed runtime subprocess (and descendants)")
        except Exception as e:
            actions.append(f"evict error: {e}")

    if running_task_ids:
        actions.append(
            f"marked {len(running_task_ids)} in-flight async task(s) as stopped"
        )

    if drain_queue:
        # Best-effort: search async_dir for tasks with status=running for this
        # pair that we didn't already mark. The cross-process file lock keeps
        # additional waiters from advancing; if a queued task was about to
        # start, we want it to abort instead.
        # Reload list now that we've marked the current ones.
        still_queued = [
            tid for tid in async_tasks.list_running_task_ids_for_pair(name)
            if tid not in set(running_task_ids)
        ]
        for tid in still_queued:
            async_tasks.mark_task_stopped(tid)
        if still_queued:
            actions.append(f"drained {len(still_queued)} queued task(s)")

    if hard:
        # Rotate session: pair_clear logic inline. Don't call pair_clear via
        # mcp.call_tool to avoid extra lock-acquire choreography — just do it
        # directly. We acquire the lock briefly; should succeed quickly since
        # we just torn down the in-flight work.
        try:
            with _with_pair_lock(name, timeout_s=10.0):
                # Tear down any live runtime — soft interrupt left it alive
                try:
                    runtime_mod.registry().evict(name)
                except Exception:
                    pass
                cur_spec = reg_mod.get_pair(name)
                adapter = _adapter_for(cur_spec)
                new_sid = str(uuid.uuid4())
                new_spec = cur_spec.model_copy(update={"session_id": new_sid, "turn_count": 0})
                adapter.create(new_spec)
                reg_mod.update_pair(
                    name, session_id=new_sid, turn_count=0, total_cost_usd=0.0,
                    last_active_at=datetime.utcnow(),
                )
                actions.append(f"rotated session: {_short(cur_spec.session_id)} → {_short(new_sid)} (hard reset)")
        except FileLockTimeout:
            actions.append("hard rotate skipped: pair is busy in another process")

    if not actions:
        return f"Pair '{name}': nothing to stop (no live runtime or in-flight tasks)"
    return f"Stopped '{name}': " + "; ".join(actions)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_status(name: str, last_n_log: int = 5, verbose: bool = False) -> str:
    """Inspect a pair's CURRENT liveness — is it actively working, slow, or hung?

    The most useful tool to call before ``pair_stop`` when an agent suspects
    its pair has gotten stuck (e.g. on a hanging Bash subprocess). Combines
    runtime state, last activity timestamp, recent log lines (with their
    timestamps), and an in-flight task list.

    Heuristic (the active/slow/likely-hung gradient only applies when there
    is actually in-flight work to monitor — an alive runtime with no
    in-flight task is just idle, not hung):

    - **idle**: runtime alive, no in-flight task (perfectly normal between sends)
    - **active**: in-flight task + last log line < 30s ago
    - **slow**: in-flight task + last log line 30-120s ago
    - **likely-hung**: in-flight task + no log activity in 120+ seconds

    Args:
        name: Pair to inspect.
        last_n_log: How many recent main.log lines to include. Default 5.
        verbose: If True, return the full JSON status dict.
    """
    spec = reg_mod.get_pair(name)
    runtime_obj = runtime_mod.registry().get_or_none(name)
    now = datetime.utcnow()

    runtime_alive = runtime_obj is not None and runtime_obj.is_alive()
    started_at = runtime_obj.started_at if runtime_obj else None
    last_activity = runtime_obj._last_log_activity_at if runtime_obj else None  # noqa: SLF001

    # Cross-process fallback: when no in-process runtime exists but main.log
    # was recently updated, the pair's runtime is live in ANOTHER MCP server
    # process (e.g. CLI install while Desktop install also runs). Read the log
    # file mtime to infer liveness so pair_status doesn't misleadingly say "cold"
    # while the pair is actively working in a sibling process.
    #
    # Same-process disambiguation: a recent runtime eviction in THIS process
    # (e.g. pair_compact/pair_invoke/pair_update mid-flight, which evict the
    # runtime then run a one-shot subprocess that does NOT write to main.log)
    # leaves runtime_alive=False and main.log mtime fresh. We'd misattribute
    # that to "another process." The in-process per-pair thread lock is held
    # for the duration of any mutating tool call, so a non-blocking acquire
    # probe distinguishes "this process is mid-tool-call" from "another process
    # really does hold the work." Tiny race window between probe and read is
    # harmless — status is a snapshot and self-corrects on next call.
    main_log = (runtime_mod.logs_dir() / name / "main.log") if name else None
    cross_process_active = False
    same_process_busy = False
    if not runtime_alive and main_log and main_log.exists():
        try:
            log_mtime = datetime.utcfromtimestamp(main_log.stat().st_mtime)
            log_idle = (now - log_mtime).total_seconds()
            if log_idle < 120:
                # Probe the in-process thread lock state. ``dict.get`` (NOT ``[]``)
                # so we don't grow the _pair_locks defaultdict by accident.
                # ``lock.locked()`` queries state without acquire/release — no race,
                # no leak risk.
                with _locks_guard:
                    inproc_lock = _pair_locks.get(name)
                if inproc_lock is not None and inproc_lock.locked():
                    same_process_busy = True
                else:
                    cross_process_active = True
                if last_activity is None:
                    last_activity = log_mtime
        except Exception:
            pass

    idle_seconds: float | None = None
    if last_activity is not None:
        idle_seconds = (now - last_activity).total_seconds()

    # In-flight async task ids — needed BEFORE the heuristic so we can distinguish
    # "runtime alive but no work is happening (just idle, perfectly normal)" from
    # "runtime alive with in-flight work that hasn't logged for 120s (likely hung)".
    # Without this distinction, a warm runtime sitting between sends would
    # eventually be reported as "likely-hung" purely because nobody had sent it
    # a message recently — which is not what the agent wants to see.
    inflight_tasks = async_tasks.list_running_task_ids_for_pair(name)

    # Heuristic. The active/slow/likely-hung gradient only makes sense when
    # there's actually in-flight work to monitor; otherwise an alive runtime
    # with no tasks is just idle (and that's fine).
    if not runtime_alive and same_process_busy:
        # Same MCP process is mid-call on this pair — runtime was just evicted
        # (compact / invoke / update) and a one-shot subprocess is running.
        heuristic = (
            f"this MCP process is mid-operation on the pair ({idle_seconds:.0f}s since last log line). "
            f"Runtime evicted for the duration of the call; will respawn on the next pair_send."
        )
    elif not runtime_alive and cross_process_active:
        heuristic = (
            f"runtime live in another MCP process ({idle_seconds:.0f}s since last log line). "
            f"In-process runtime: cold. Use pair_poll(<task_id>) or wait for current turn."
        )
    elif not runtime_alive:
        heuristic = "no runtime (cold; will spawn on next send)"
    elif not inflight_tasks:
        # Runtime alive, no work in flight — pair is idle and ready for the next send.
        if idle_seconds is None:
            heuristic = "idle (runtime alive, no work in flight)"
        else:
            heuristic = f"idle ({idle_seconds:.0f}s since last activity; runtime alive, no work in flight)"
    elif idle_seconds is None:
        heuristic = "runtime alive, no log activity recorded yet"
    elif idle_seconds < 30:
        heuristic = f"active (last activity {idle_seconds:.1f}s ago)"
    elif idle_seconds < 120:
        heuristic = f"slow ({idle_seconds:.0f}s since last log line; still working but worth watching)"
    else:
        heuristic = f"likely-hung ({idle_seconds:.0f}s idle — consider pair_log to inspect, then pair_stop if truly stuck)"

    # Recent log lines
    recent_lines: list[str] = []
    if main_log and main_log.exists():
        try:
            with open(main_log, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            recent_lines = [l.rstrip("\n") for l in all_lines[-max(1, int(last_n_log)):]]
        except Exception:
            pass

    if verbose:
        return json.dumps({
            "name": name,
            "runtime_alive": runtime_alive,
            "started_at": started_at.isoformat() if started_at else None,
            "last_log_activity_at": last_activity.isoformat() if last_activity else None,
            "idle_seconds": idle_seconds,
            "heuristic": heuristic,
            "inflight_async_tasks": inflight_tasks,
            "recent_log_lines": recent_lines,
            "persistent": spec.persistent,
        }, indent=2, default=str)

    lines = [f"Pair '{name}' status:"]
    lines.append(f"  runtime: {'alive' if runtime_alive else 'cold'}")
    if started_at:
        age_s = (now - started_at).total_seconds()
        lines.append(f"  runtime age: {age_s:.0f}s")
    lines.append(f"  liveness: {heuristic}")
    if inflight_tasks:
        # Show FULL task IDs when there are 1-2 (agent can paste straight into
        # pair_poll); truncate to 8-char prefix for 3+ to keep the line readable
        # (pair_poll accepts unique prefixes, so the agent can still resolve them).
        if len(inflight_tasks) <= 2:
            ids_str = ", ".join(inflight_tasks)
        else:
            ids_str = ", ".join(t[:8] for t in inflight_tasks[:3]) + "..."
        lines.append(f"  in-flight async tasks: {len(inflight_tasks)} ({ids_str})")
        # Hint the focused tool. pair_poll is the task-scoped content viewer
        # for ANY status (running/done/failed/stopped); agents otherwise reach
        # for pair_transcript, which is the broader conversation browser.
        lines.append(
            f"  -> for this task's content (live now, or response once done): "
            f"pair_poll('{inflight_tasks[0]}', with_turn_log=True)"
        )
    else:
        lines.append("  in-flight async tasks: none")
    if recent_lines:
        lines.append(f"  last {len(recent_lines)} log line(s):")
        for l in recent_lines:
            lines.append(f"    {l}")
    return "\n".join(lines)


# ============================================================================
# Skill / command invocation
# ============================================================================

def _invocation_allowed(skill_name: str, allow_list: list[str] | None) -> bool:
    """fnmatch-glob check for the pair_invoke allow-list.

    Returns True if invocation is permitted. Semantics:
      - allow_list is None  → allow all (backward compat with pre-v0.8.1)
      - allow_list is []    → deny all (explicit lockdown)
      - allow_list is [...] → allow if skill_name matches ANY pattern via fnmatch.

    Patterns use ``fnmatch`` (stdlib) — same syntax as shell globs and as the
    CLI's ``--disallowed-tools``. ``mcp__claude_ai_*`` matches MCP-server skill
    prefixes; ``clear`` exactly matches the bare command name.
    """
    if allow_list is None:
        return True
    if not allow_list:  # explicit empty list = deny all
        return False
    import fnmatch as _fnmatch
    return any(_fnmatch.fnmatchcase(skill_name, pat) for pat in allow_list)


@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def pair_invoke(
    name: str,
    skill_name: str,
    args: str | None = None,
    timeout_seconds: int = 300,
    verbose: bool = False,
) -> str:
    """Invoke a skill in the pair by name (translates to /skill-name via stream-json).

    Skills are discovered via pair_actions(name).pair_skills. Examples: "init", "review",
    "security-review", or any user-installed skill. Slash commands fire natively in
    stream-json mode (unlike plain --print which treats them as literal text).

    Allow-list (v0.8.1+): if the pair was created with ``allowed_invocations`` set
    (or inherited a default from ``pair_settings_set``), invocations not matching
    any pattern in the list are refused with a PairError before reaching the CLI.
    Patterns use ``fnmatch`` glob syntax. Modify via ``pair_update``.
    """
    try:
        with _with_pair_lock(name, timeout_s=max(60.0, float(timeout_seconds))):
            spec = reg_mod.get_pair(name)
            # Server-side allow-list check BEFORE invoking the adapter. No CLI work,
            # no token spend, no race against runtime spawn — fail fast and clearly.
            if not _invocation_allowed(skill_name, spec.allowed_invocations):
                allow_list_repr = (
                    "[] (deny all)" if spec.allowed_invocations == []
                    else repr(spec.allowed_invocations)
                )
                raise PairError(
                    f"Invocation '/{skill_name}' is not permitted on pair '{name}'. "
                    f"Allow-list: {allow_list_repr}. "
                    f"To permit, run pair_update(name='{name}', "
                    f"allowed_invocations=[...patterns...]). "
                    f"Discover available commands via pair_actions('{name}')."
                )
            adapter = _adapter_for(spec)
            result = adapter.invoke_skill(spec, skill_name, args=args, timeout_seconds=timeout_seconds)
            if verbose:
                return _verbose_dump(result)
            return _fmt_send_result(result)
    except FileLockTimeout:
        raise PairError(f"Pair '{name}' is busy in another process; could not acquire lock to invoke skill.")


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_context(name: str, timeout_seconds: int = 60, verbose: bool = False) -> str:
    """Invoke /context in the pair and return its rich token-usage breakdown.

    Costs one inference. For free context tracking, every pair_send response already
    includes a `context` block computed from the result's usage data — only use this when
    you want the full categorized breakdown (system prompt, system tools, memory, skills,
    free space, autocompact buffer).

    Default: returns just the markdown body (which is already human-readable).
    Pass verbose=True for the full JSON wrapper (model, tokens_used, tokens_max, percent + markdown).
    """
    spec = reg_mod.get_pair(name)
    adapter = _adapter_for(spec)
    report = adapter.context(spec, timeout_seconds=timeout_seconds)
    if verbose:
        return _verbose_dump(report)
    return report.raw_markdown


# ============================================================================
# Custom agent management
# ============================================================================

@mcp.tool(output_schema=None, annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def pair_agent_define(
    name: str,
    description: str,
    prompt: str,
    tools: str | list[str] | None = None,
    model: str | None = None,
) -> str:
    """Define a custom claude-code agent (visible to all claude sessions globally).

    Writes ~/.claude/agents/<name>.md with YAML frontmatter. Pairs can then reference
    this agent by name as subagent_type when invoking the Agent tool.

    Args:
        tools: Tool allowlist for the agent. Accepts a real list, a JSON-array string,
            a semicolon- or newline-separated string ("Read;Edit;Bash"), or a single
            value. None = inherit defaults.

    NOTE: Agent definitions are GLOBAL — they're loaded by every Claude Code session.
    Name them to avoid pollution (e.g. "rust-reviewer" rather than "reviewer").
    """
    tools_norm = _coerce_to_str_list(tools)
    path = agents_mod.define_agent(name, description, prompt, tools=tools_norm, model=model)
    return f"Defined agent '{name}' at {path}"


@mcp.tool(output_schema=None, annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
def pair_agent_list(verbose: bool = False) -> str:
    """List all custom agents under ~/.claude/agents/."""
    agents = agents_mod.list_agents()
    if not agents:
        return "(no custom agents defined)"
    if verbose:
        return json.dumps({"agents": agents}, indent=2, default=str)
    lines = [f"{len(agents)} custom agent{'s' if len(agents) != 1 else ''}:"]
    for a in agents:
        model = f", model {a['model']}" if a.get("model") else ""
        lines.append(f"  {a.get('name', '?')}{model} — {a.get('description', '')[:80]}")
    return "\n".join(lines)


# Re-export entry point
def run() -> None:
    mcp.run()
