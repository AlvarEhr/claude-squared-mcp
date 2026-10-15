"""CodexAdapter: wraps the OpenAI Codex CLI (``codex exec``) as a pair backend.

Phase 1 = one-shot subprocess per turn (no warm runtime, no self-woken
machinery): every send is ``codex exec --json … resume <thread-id> <message>``
with stdin at EOF (exec blocks on an open pipe even with a prompt argument),
options BEFORE the subcommand, and ``--skip-git-repo-check -C <cwd>`` on every
call. Model + effort + sandbox are per-invocation in Codex (never inherited
from the thread), so they are re-passed every time — same discipline as
``claude --model``.

Everything empirical here was verified against codex-cli 0.153.4 on Windows
with ChatGPT-plan auth on 2026-09-07 — see HANDOFF.md "Codex backend".
"""

from __future__ import annotations

import glob
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from claude_squared.adapters.base import PairAdapter
from claude_squared import connectors
from claude_squared.adapters.appserver import AppServerClient
from claude_squared.codex_models import (
    CODEX_1M_CONFIG,
    codex_home,
    context_windows,
    resolve_codex_model,
)
from claude_squared.errors import CLIError, CommandTimeout, SessionMissing, TaskStopped
from claude_squared.models import (
    CODEX_PERMISSION_MAP,
    CompactResult,
    ContextReport,
    ContextStatus,
    CreateResult,
    PairSpec,
    PermissionDenial,
    SendResult,
    normalize_permission,
)
from claude_squared.registry import logs_dir
from claude_squared.process_spawn import hidden_process_kwargs
from claude_squared.runtime import ToolCounter
from claude_squared.tool_details import save_codex_item


WARNING_THRESHOLD = 0.60
STRONG_WARNING_THRESHOLD = 0.85
THREAD_SOURCE = "claude-squared"   # tags our threads in ~/.codex sqlite + the Desktop UI
PROBE_PROMPT = "Reply with exactly: pair-ready"
CONTEXT_POLL_SECONDS = 10.0


class _NoCodexActivity:
    """Activity publication is optional; an unavailable writer cannot block exec."""

    def __init__(self, spec: PairSpec):
        self.spec = spec
        self.limit = None
        self.status, self.preview = "failed", ""

    def observe(self, event: dict, *, position: int | None = None) -> None:
        pass

    def poll(self, *, force: bool = False) -> None:
        pass

    def close(self) -> None:
        pass


def _new_codex_activity(spec: PairSpec, task_id: str | None):
    try:
        from claude_squared import activity

        # Short-lived CLI observers can publish recovery activity without ever
        # entering server.run(). Install before creating their owner file —
        # the process's single exit handler (runtimes first, then activity),
        # never a separate activity.shutdown that could run ahead of the
        # runtimes' own loss records (atexit is LIFO; critic, 2026-09-25).
        from claude_squared import runtime
        runtime.install_exit_handler()
        return _CodexActivity(spec, task_id)
    except Exception:
        return _NoCodexActivity(spec)


def _configured_compact_limit(args: list[str]) -> int | None:
    """Last CLI config assignment wins, including backend_options.args overrides."""
    value = None
    for index, arg in enumerate(args):
        assignment = (args[index + 1] if arg in ("-c", "--config") and index + 1 < len(args)
                      else arg[len("--config="):] if arg.startswith("--config=")
                      else arg[2:].lstrip("=") if arg.startswith("-c") else "")
        key, sep, raw = assignment.partition("=")
        if sep and key.strip() == "model_auto_compact_token_limit":
            try:
                value = int(raw.strip())
            except ValueError:
                value = None
    return value if value is not None and value > 0 else None


class _CodexActivity:
    """One send's activity; rollout I/O stays on the exec supervisor thread.

    The event reader only enqueues compaction notices. Sampling and activity
    mutations are best-effort and must never turn a useful reply into an error.
    """

    def __init__(self, spec: PairSpec, task_id: str | None):
        from claude_squared import activity

        self.spec, self.task_id = spec, task_id
        self.writer = activity.writer(spec.name, "codex")
        self.writer.turn_opened(task_id, "send")
        self.status, self.preview = "failed", ""
        self.limit: int | None = None
        self.used: int | None = None
        self._rollout: Path | None = None
        self._next_sample = 0.0
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._pre: dict[str, int | None] = {}
        self._completed: set[object] = set()
        self._seen_positions: set[int] = set()
        self._anonymous_pre: list[int | None] = []

    def observe(self, event: dict, *, position: int | None = None) -> None:
        """Reader hot path: memory only; capture the most recent known fill."""
        item = event.get("item")
        if (isinstance(item, dict) and item.get("type") in ("contextCompaction", "context_compaction")
                and event.get("type") in ("item.started", "item.completed")):
            self._events.put((event, self.used, position))

    def _sample(self, force: bool) -> None:
        from claude_squared import activity

        now = time.monotonic()
        if not force and now < self._next_sample:
            return
        self._next_sample = now + CONTEXT_POLL_SECONDS
        if self._rollout is None:
            self._rollout = rollout_path_for(self.spec.session_id)
        # Codex 0.155, 2026-09-25 live probe: token_count may be flushed only
        # after the tool call it launched finishes. This is the latest REPORTED
        # fill; an alert can arrive one tool call late, including just after exit.
        payload = read_last_token_count(self._rollout) or {}
        info = payload.get("info") or {}
        window = int(info.get("model_context_window") or 0)
        if window <= 0:
            return
        limit = activity.compaction_point(window, "codex", self.limit)
        self.writer.note_reported_window(window, model_key=f"{self.spec.model}|{self.spec.context_window}",
                                          model=self.spec.model, source="rollout",
                                          auto_compact_token_limit=limit)
        # Match the SendResult footer: current model-call input, never the
        # rollout's cumulative total_token_usage (design revision E). Whether
        # native auto-compaction counts output too is unmeasured; this proxy
        # and the default 90% compaction point are alerting estimates.
        used = live_context_tokens(info)
        if used is None:
            return
        self.used = used
        self.writer.note_context(self.used, window, window_source="rollout", source="rollout",
                                  auto_compact_token_limit=limit)

    def poll(self, *, force: bool = False) -> None:
        try:
            completed = []
            while True:
                try:
                    event, sampled_pre, position = self._events.get_nowait()
                except queue.Empty:
                    break
                if position is not None:
                    if position in self._seen_positions:
                        continue
                    self._seen_positions.add(position)
                item = event["item"]
                item_id = item.get("id")
                key = ("id", str(item_id)) if item_id else (
                    "offset", position if position is not None else len(self._completed))
                if key in self._completed:
                    continue
                pre = item.get("pre_tokens", item.get("preTokens", sampled_pre))
                if event["type"] == "item.started":
                    if item_id:
                        self._pre[key] = pre
                    else:
                        self._anonymous_pre.append(pre)
                else:
                    self._completed.add(key)
                    pre = (self._pre.pop(key, pre) if item_id else
                           self._anonymous_pre.pop(0) if self._anonymous_pre else pre)
                    completed.append((pre, item.get("post_tokens", item.get("postTokens"))))
            try:
                self._sample(force or bool(completed))
            except Exception:
                pass  # a missing/half-written rollout must not hide compaction events
            for pre, post in completed:
                # A newer, smaller sample is evidence of the post-compaction
                # fill; an unchanged old token_count isn't a measured post.
                if post is None and pre is not None and self.used is not None and self.used < pre:
                    post = self.used
                self.writer.note_compaction("auto", pre, post)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.writer.turn_closed(self.task_id, status=self.status, preview=self.preview)
        except Exception:
            pass


def record_recovered_activity(state) -> None:
    """After terminal task persistence, publish under the recovering owner's PID.

    The dead owner's file is left untouched. A fresh window excludes its
    synthesized owner_died turn; the terminal task file remains authoritative.
    """
    from claude_squared import codex_turns, registry

    try:
        current = registry.get_pair(state.pair_name)
    except Exception:
        return  # forgotten/quarantined pair: its task result is still recoverable
    if current.backend != "codex":
        return
    try:
        path = Path(state.events_path)
        receipt = codex_turns.read_receipt(path)
        spec = PairSpec.model_validate(receipt["spec"])
    except Exception:
        spec = current
        receipt = {}
    if spec.session_id != current.session_id:
        return  # do not replace a newer session's activity with an old task
    turn = _new_codex_activity(spec, state.task_id)
    try:
        turn.limit = receipt.get("auto_compact_token_limit")
        if state.recovered_at:
            try:
                for position, event in codex_turns.read_events(Path(state.events_path)):
                    turn.observe(event, position=position)
            except OSError:
                pass
        turn.poll(force=True)
        turn.status = state.status
        if state.status == "failed":
            from claude_squared.async_tasks import ORPHAN_ERROR_PREFIX, CRASHED_ERROR_PREFIX
            if (state.error or "").startswith(ORPHAN_ERROR_PREFIX):
                turn.status = "owner_died"
            elif (state.error or "").startswith(CRASHED_ERROR_PREFIX):
                turn.status = "crashed"
        turn.preview = getattr(state.result, "response", None) or state.error or ""
    finally:
        turn.close()

# User-config keys re-added under ``--ignore-user-config``. The flag is
# required (the user's config mounts claude-squared itself as an MCP server →
# recursion; plus Desktop plugins/hooks a pair shouldn't inherit), but it also
# drops settings the sandbox NEEDS: on Windows, without ``windows.sandbox =
# "elevated"`` a ``-s workspace-write`` run silently degrades to a READ-ONLY
# sandbox (verified: "writing is blocked by read-only sandbox"). Whitelist, not
# blanket: each key is re-passed as ``-c dotted.key=<toml literal>``.
CONFIG_READD_KEYS: tuple[str, ...] = (
    "windows",                    # windows.sandbox = "elevated"
    "sandbox_workspace_write",    # network_access etc.
    "model_provider",
    "model_providers",
    "web_search",
    "shell_environment_policy",
)

# stderr lines that mean "the sandbox / approval policy blocked an action".
# There is NO denial event in the --json stream — the model just sees a
# failed tool call — so this is the only structured signal.
_DENIAL_RE = re.compile(
    r"rejected by user approval settings|blocked by read-only sandbox|"
    r"writing outside of the project|sandbox.*denied|approval.*denied",
    re.IGNORECASE,
)
_READONLY_DEGRADE_RE = re.compile(r"blocked by read-only sandbox", re.IGNORECASE)
_OS_DENIED_RE = re.compile(
    r"Access to the path '[^']*' is denied|Access is denied|PermissionDenied|"
    r"Permission denied|Operation not permitted|EACCES|EPERM",
)
_MODEL_UNSUPPORTED_RE = re.compile(
    r"model is not supported|Model metadata for .* not found|invalid_request_error",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|quota", re.IGNORECASE)


# In-flight one-shot processes, so pair_stop / pair_status can reach them
# (there is no PairRuntime for codex). name → {"proc": Popen, "task_id": str,
# "started_at": datetime, "last_activity": datetime}.
_INFLIGHT: dict[str, dict[str, Any]] = {}
_INFLIGHT_LOCK = threading.Lock()


def inflight_info(name: str) -> dict[str, Any] | None:
    with _INFLIGHT_LOCK:
        info = _INFLIGHT.get(name)
        return dict(info) if info else None


def _tree_kill(proc: subprocess.Popen) -> str:
    """Kill the codex process AND its command grandchildren (PowerShell
    wrappers); ``proc.kill()`` alone would orphan a running command."""
    if proc.poll() is not None:
        return "already-exited"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False,
                           **hidden_process_kwargs(console=False))
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return "tree-killed"
    except Exception:
        try:
            proc.kill()
            return "fallback-kill"
        except Exception:
            return "kill-failed"


def stop_inflight(name: str) -> str | None:
    """Tree-kill the pair's in-flight codex turn (pair_stop path). Returns an
    outcome string, or None when nothing was in flight in this process."""
    with _INFLIGHT_LOCK:
        info = _INFLIGHT.get(name)
    if not info:
        return None
    proc = info.get("proc")
    if proc is None:
        return None
    return _tree_kill(proc)


def stop_detached(state) -> str | None:
    """Stop exactly a persisted detached child, never the former owner PID.

    Caller holds the control lock, so task selection and cancellation agree.
    Do not acquire the pair lock here: sends take pair -> control.
    """
    from claude_squared import async_tasks

    current = async_tasks.load_task(state.task_id)
    if (current is None or not async_tasks.is_detached(current)
            or current.child_pid != state.child_pid or not current.child_pid
            or not async_tasks._is_pid_alive(current.child_pid)):
        return None
    from claude_squared import process_identity
    pid = current.child_pid
    try:
        with process_identity.matching_process(pid, current.child_started_at) as matches:
            if not matches:
                return "identity-unverified (child exited or PID was reused; no process killed)"
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                        capture_output=True, timeout=10, check=False,
                                        **hidden_process_kwargs(console=False))
                return "tree-killed" if result.returncode == 0 else "kill-failed"
            import signal
            os.killpg(pid, signal.SIGKILL)  # exec starts its own session/process group
            return "tree-killed"
    except ProcessLookupError:
        return "already-exited"
    except (OSError, subprocess.TimeoutExpired):
        return "kill-failed"


def recover_turn(state) -> SendResult | None:
    """Collect an exited exec under the pair lock; reuse the live result builder."""
    from claude_squared import codex_turns

    events_path = Path(state.events_path)
    try:
        rows = codex_turns.read_events(events_path)
    except FileNotFoundError:
        return None
    events = [event for _, event in rows]
    completed = any(e.get("type") == "turn.completed" for e in events)
    failed = any(e.get("type") == "turn.failed" for e in events)
    # A store conflict can fail before turn.started/turn.failed. Preserve it
    # as a work error instead of disguising it as supervision loss.
    conflict = any(e.get("type") == "error" and "thread-store conflict" in str(e.get("message", "")).lower()
                   for e in events)
    stderr_path = events_path.parent / events_path.name.replace(".events.jsonl", ".stderr.log")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    conflict |= "thread-store conflict" in stderr.lower()
    if not completed and not failed and not conflict:
        return None
    receipt = codex_turns.read_receipt(events_path)
    spec = PairSpec.model_validate(receipt["spec"])
    adapter = CodexAdapter()
    codex_turns.finish_pending_log(events_path, receipt)
    result_data = receipt.get("recovery_result")
    if result_data:
        result = SendResult.model_validate(result_data)
    else:
        log_dir = logs_dir() / spec.name
        main_log = Path(receipt["log_path"])
        # Persist the replay scope before appending; a recovering process may
        # itself die. The append receipt prevents duplicate replay on retry.
        if "replay_start" not in receipt:
            receipt["replay_start"] = adapter._count_lines(main_log) + 1
            codex_turns.save_receipt(events_path, receipt)
        adapter._replay_events(events_path, receipt, rows, recovered=True)
        events.append({"type": "_log_scope", "log_path": str(main_log),
                       "start_line": receipt["replay_start"],
                       "end_line": adapter._count_lines(main_log)})
        dur_ms = int(max(0, events_path.stat().st_mtime - receipt["started_epoch"]) * 1000)
        result = adapter._build_send_result(
            spec, events, stderr, 0 if completed else 1, dur_ms, receipt["started_epoch"],
            model_used=receipt["model_used"], permission_level=receipt["permission_level"],
        )
        result.notes.append("recovered after owner death")
        receipt["recovery_result"] = result.model_dump(mode="json")
        codex_turns.save_receipt(events_path, receipt)
    codex_turns.bookkeep_send(events_path, result)
    return result


# ---------------------------------------------------------------------------
# Binary + config resolution
# ---------------------------------------------------------------------------

_EXE_CACHE: dict[str, Any] = {}


def _codex_version(exe: str) -> tuple[int, ...]:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=15, stdin=subprocess.DEVNULL,
                             **hidden_process_kwargs(console=False)).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
        return tuple(int(x) for x in m.groups()) if m else ()
    except Exception:
        return ()


def _user_config_path() -> Path:
    return codex_home() / "config.toml"


try:  # py3.11+ stdlib; ``tomli`` (declared for <3.11 in pyproject) is API-compatible
    import tomllib as _toml
except ImportError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ImportError:
        _toml = None  # type: ignore[assignment]


def _load_user_config() -> dict[str, Any]:
    """Parsed ``~/.codex/config.toml`` or {} when missing/unreadable. Without a
    TOML parser (Python 3.10 with no ``tomli``) this silently returns {} —
    which drops the ``windows.sandbox`` re-add; ``sandbox_support_note`` says so."""
    p = _user_config_path()
    if _toml is None:
        return {}
    try:
        with open(p, "rb") as f:
            return _toml.load(f)
    except Exception:
        return {}


def codex_executable(*, refresh: bool = False) -> str:
    """Locate the codex binary. Order: ``CLAUDE_PAIR_CODEX_PATH`` env →
    ``CODEX_CLI_PATH`` recorded in the user's config.toml (the Desktop-managed
    build, which is the only one whose updater works) → newest
    ``%LOCALAPPDATA%/OpenAI/Codex/bin/<hash>/codex.exe`` by ``--version`` →
    ``codex`` on PATH.

    The first two are read on EVERY call: the Codex app's updater installs a
    new ``bin/<hash>/``, rewrites CODEX_CLI_PATH and deletes the old folder,
    and a long-lived MCP process must follow it (it once cached the old path,
    and every Codex spawn failed with WinError 2 until a restart). A config
    path counts only once the file exists (the config may be rewritten before
    the new binary is in place). The scan's answer (``--version`` of every
    candidate) is cached only while its file exists and the set of candidate
    folders is unchanged — a new ``bin/<hash>/`` triggers a rescan even if the
    old one survives; ``refresh=True`` rescans."""
    env = os.environ.get("CLAUDE_PAIR_CODEX_PATH")
    if env and Path(env).exists():
        return env
    cfg = _load_user_config()
    # The Desktop writes CODEX_CLI_PATH into an MCP server's env block; scan
    # every [mcp_servers.*.env] for it rather than assuming a server name.
    for srv in (cfg.get("mcp_servers") or {}).values():
        envblk = (srv or {}).get("env") if isinstance(srv, dict) else None
        cand = (envblk or {}).get("CODEX_CLI_PATH") if isinstance(envblk, dict) else None
        if cand and os.path.isfile(str(cand)):
            return str(cand)
    local = os.environ.get("LOCALAPPDATA")
    candidates = tuple(sorted(glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")))) \
        if local else ()
    cached = _EXE_CACHE.get("exe")
    if (cached and not refresh and _EXE_CACHE.get("candidates") == candidates
            and os.path.isfile(cached)):
        return cached
    _EXE_CACHE.clear()
    best: tuple[tuple[int, ...], str] | None = None
    for cand in candidates:
        v = _codex_version(cand)
        if best is None or v > best[0]:
            best = (v, cand)
    found = best[1] if best else shutil.which("codex")
    if found:
        _EXE_CACHE.update(exe=found, candidates=candidates)
        return found
    return "codex"


def _with_fresh_codex(spawn: Callable, args: list[str], **kwargs: Any):
    """Run ``spawn(args, …)`` for a command whose ``args[0]`` came from
    :func:`codex_executable`. The updater can delete the binary between
    resolution and spawn, so a binary that is gone is re-resolved once and the
    spawn retried; ``args[0]`` is updated in place so callers report the
    binary that ran (or was last tried)."""
    try:
        return spawn(args, **kwargs)
    except FileNotFoundError:
        if os.path.isfile(args[0]):
            raise  # the binary is there; something else is missing (the cwd)
        fresh = codex_executable(refresh=True)
        if fresh == args[0]:
            raise
        args[0] = fresh
        return spawn(args, **kwargs)


def popen_codex(args: list[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` with :func:`_with_fresh_codex`'s one retry."""
    return _with_fresh_codex(subprocess.Popen, args, **hidden_process_kwargs(kwargs))


def run_codex(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with :func:`_with_fresh_codex`'s one retry."""
    return _with_fresh_codex(subprocess.run, args, **hidden_process_kwargs(kwargs))


def codex_spawn_error(args: list[str], exc: OSError, cwd: str | None, what: str = "codex") -> str:
    """Error text for a codex process that could not be started, naming the
    likely cause: a missing working directory (Windows reports it as WinError
    3/267, much like a missing binary) or no Codex binary at all."""
    msg = f"could not start {what} ({args[0]}): {exc}"
    if cwd and not os.path.isdir(cwd):
        return msg + f" — the working directory does not exist: {cwd} (pair_update(cwd=…) to move the pair)"
    if not os.path.isfile(args[0]) and not shutil.which(args[0]):
        msg += (" — no Codex binary found (CLAUDE_PAIR_CODEX_PATH, CODEX_CLI_PATH in the Codex "
                "config.toml, the Codex app's bin folder and PATH are re-read on every spawn). "
                "Install or repair Codex, or set CLAUDE_PAIR_CODEX_PATH.")
    return msg


def _toml_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # TOML basic strings share JSON escaping for our values
    if isinstance(v, list):
        return "[" + ", ".join(_toml_literal(x) for x in v) + "]"
    return json.dumps(str(v))


def _flatten(prefix: str, obj: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out.append((prefix, _toml_literal(obj)))


def config_readd_args() -> list[str]:
    """``-c key=value`` for every whitelisted top-level key present in the
    user's config (flattened to dotted paths)."""
    cfg = _load_user_config()
    pairs: list[tuple[str, str]] = []
    for key in CONFIG_READD_KEYS:
        if key in cfg:
            _flatten(key, cfg[key], pairs)
    args: list[str] = []
    for k, v in pairs:
        args += ["-c", f"{k}={v}"]
    return args


def sandbox_support_note() -> str | None:
    """Windows without ``windows.sandbox`` configured → workspace-write can't
    be enforced (degrades to read-only). Surfaced at create."""
    if os.name != "nt":
        return None
    if _toml is None:
        return ("this Python has no TOML parser (3.10 without `tomli`), so ~/.codex/config.toml "
                "cannot be read and `[windows] sandbox` is NOT re-added under --ignore-user-config — "
                "'workspace'/'auto' levels will run READ-ONLY. Install `tomli` or use Python 3.11+.")
    cfg = _load_user_config()
    if isinstance(cfg.get("windows"), dict) and cfg["windows"].get("sandbox"):
        return None
    return (
        "Codex on Windows needs `[windows] sandbox = \"elevated\"` in ~/.codex/config.toml "
        "for the workspace-write sandbox; without it 'workspace'/'auto' levels silently "
        "run READ-ONLY. Open the Codex app once (it writes the key) or add it by hand."
    )


# ---------------------------------------------------------------------------
# Thread store access (read-only)
# ---------------------------------------------------------------------------

def _state_db_path() -> Path | None:
    cands = sorted(codex_home().glob("state_*.sqlite"))
    return cands[-1] if cands else None


def _ro_connect(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)


def rollout_path_for(thread_id: str) -> Path | None:
    """Authoritative: ``threads.rollout_path`` in the state sqlite; fallback:
    glob the sessions tree for ``rollout-*-<id>.jsonl``."""
    db = _state_db_path()
    if db is not None:
        try:
            con = _ro_connect(db)
            try:
                row = con.execute("select rollout_path from threads where id=?",
                                  (thread_id,)).fetchone()
            finally:
                con.close()
            if row and row[0]:
                p = Path(row[0])
                if p.exists():
                    return p
        except Exception:
            pass
    for cand in glob.glob(str(codex_home() / "sessions" / "*" / "*" / "*" / f"rollout-*{thread_id}*.jsonl")):
        return Path(cand)
    return None


def guardian_verdicts_since(since_epoch: float, parent_thread_id: str | None = None) -> list[dict[str, Any]]:
    """Verdicts of ``codex-auto-review`` guardian threads created since
    ``since_epoch`` (the ``--approve-for-me`` reviewer). Each: {thread_id,
    outcome, risk_level, rationale}. When ``parent_thread_id`` is given only
    guardian threads whose rollout references it (their ``token_usage_record``
    carries the parent thread as ``session_id``) are returned, so concurrent
    pairs / Desktop activity can't cross-attribute verdicts (Astra catch).
    Best-effort; [] on any failure."""
    db = _state_db_path()
    if db is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        con = _ro_connect(db)
        try:
            rows = con.execute(
                "select id, rollout_path, created_at from threads "
                "where thread_source='guardian_review' and created_at >= ? "
                "order by created_at asc", (int(since_epoch) - 1,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    for tid, rp, _ts in rows:
        verdict: dict[str, Any] = {"thread_id": tid}
        matched_parent = parent_thread_id is None
        try:
            p = Path(rp) if rp else rollout_path_for(tid)
            if p and p.exists():
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if parent_thread_id and parent_thread_id in line:
                        matched_parent = True
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    pl = d.get("payload") or {}
                    if d.get("type") == "event_msg" and pl.get("type") == "agent_message":
                        try:
                            verdict.update(json.loads(pl.get("message") or "{}"))
                        except Exception:
                            verdict["raw"] = str(pl.get("message"))[:200]
        except Exception:
            pass
        if matched_parent:
            out.append(verdict)
    return out


def _reverse_complete_lines(stream, chunk_size: int = 64 * 1024):
    """Read complete JSONL records newest first, without loading the history.

    Keep fragments as a list so a large tool-output line does not cause
    quadratic concatenation. The unterminated EOF fragment is never parsed.
    A fresh EOF snapshot per call also handles rewind/truncate/replacement.
    """
    stream.seek(0, os.SEEK_END)
    position = stream.tell()
    fragments: list[bytes] = []
    trailing = True
    while position:
        start = max(0, position - chunk_size)
        stream.seek(start)
        parts = stream.read(position - start).split(b"\n")
        position = start
        if len(parts) == 1:
            fragments.append(parts[0])
            continue
        if not trailing:
            yield parts[-1] + b"".join(reversed(fragments))
        trailing = False
        fragments = [parts[0]]
        yield from reversed(parts[1:-1])
    if not trailing:
        yield b"".join(reversed(fragments))


def read_last_token_count(rollout: Path | None) -> dict[str, Any] | None:
    """The LAST ``token_count`` event_msg payload in a rollout: carries
    ``info.total_token_usage``, ``info.last_token_usage``,
    ``info.model_context_window`` (the EFFECTIVE window — 258,400 default,
    828,400 with the 1M config) and ``rate_limits`` (plan quota). Scans backward
    in chunks and returns at the first valid token_count; a long old thread
    must not stall exec supervision on every ten-second sample."""
    if rollout is None:
        return None
    try:
        with open(rollout, "rb") as f:
            for line in _reverse_complete_lines(f):
                if b'"token_count"' not in line:
                    continue
                try:
                    d = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                pl = d.get("payload") if isinstance(d, dict) else None
                if isinstance(pl, dict) and d.get("type") == "event_msg" and pl.get("type") == "token_count":
                    return pl
    except OSError:
        pass
    return None


def live_context_tokens(info: dict[str, Any] | None) -> int | None:
    """Live context fill from a ``token_count`` payload's ``info``: the last
    model call's ``input_tokens`` — never ``total_token_usage``, which is the
    thread's cumulative usage across every call (tens of millions on a long
    thread). After a compaction Codex writes a token_count whose
    ``last_token_usage`` has ``input_tokens`` 0 and ``total_tokens`` set to
    its estimate of the new history (verified 2026-10-15: 0 / 27,527 while
    the cumulative total stayed 56,802,796); that estimate is the fill until
    the next model call. None when neither is reported."""
    last = (info or {}).get("last_token_usage") or {}
    inp = last.get("input_tokens")
    if isinstance(inp, int) and inp > 0:
        return inp
    est = last.get("total_tokens")
    if isinstance(est, int) and est > 0:
        return est
    return None


def _post_compaction_tokens(before: dict[str, Any] | None, after: dict[str, Any] | None) -> int | None:
    """Codex's own post-compaction estimate, when compaction wrote a NEW
    token_count with the ``input_tokens`` 0 marker (see
    ``live_context_tokens``); None otherwise — the last token_count may still
    be the pre-compaction one, whose figures describe the old context."""
    if not after or after == before:
        return None
    last = (after.get("info") or {}).get("last_token_usage") or {}
    if last.get("input_tokens"):
        return None
    return live_context_tokens(after.get("info"))


def _estimate_compacted_tokens(rollout: Path | None) -> int:
    """Rough size of the LAST ``compacted`` record's replacement history
    (chars / 4). 0 when there is none."""
    if rollout is None or not rollout.exists():
        return 0
    last: dict[str, Any] | None = None
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"compacted"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "compacted":
                    last = d.get("payload") or {}
    except Exception:
        return 0
    if not last:
        return 0
    text = json.dumps(last.get("replacement_history") or last.get("message") or "", default=str)
    return max(0, len(text) // 4)


def plan_usage_text(rate_limits: dict[str, Any] | None) -> str | None:
    if not isinstance(rate_limits, dict):
        return None
    parts: list[str] = []
    for key, label in (("primary", ""), ("secondary", "secondary ")):
        win = rate_limits.get(key)
        if not isinstance(win, dict):
            continue
        used = win.get("used_percent")
        mins = win.get("window_minutes")
        resets = win.get("resets_at")
        if used is None:
            continue
        span = ""
        if isinstance(mins, (int, float)) and mins:
            days = mins / 1440
            span = (f"{int(days)}-day" if days >= 1 and float(days).is_integer()
                    else f"{int(mins/60)}h" if mins >= 60 else f"{int(mins)}min")
        when = ""
        if isinstance(resets, (int, float)) and resets:
            try:
                when = ", resets " + datetime.fromtimestamp(resets).astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = ""
        parts.append(f"{label}{used:.0f}% of the {span} limit used{when}")
    plan = rate_limits.get("plan_type")
    reached = rate_limits.get("rate_limit_reached_type")
    s = "; ".join(parts)
    if plan:
        s = f"{s} (plan {plan})" if s else f"plan {plan}"
    if reached:
        s = f"{s} — LIMIT REACHED ({reached})"
    return s or None


# ---------------------------------------------------------------------------
# Rollout transcript helpers (used by transcript.py + pair_rewind)
# ---------------------------------------------------------------------------

def is_codex_rollout(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        return '"session_meta"' in first
    except Exception:
        return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
                parts.append(str(c.get("text") or ""))
    return "\n".join(p for p in parts if p)


def tail_turns_codex(path: Path, last_n: int = 10) -> list[dict[str, Any]]:
    """Last N user/assistant turns from a Codex rollout JSONL, in the same
    shape ``transcript.tail_turns`` returns for Claude sessions."""
    turns: list[dict[str, Any]] = []
    pending_tools: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "response_item":
            continue
        pl = d.get("payload") or {}
        t = pl.get("type")
        if t == "message":
            role = pl.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _content_text(pl.get("content"))
            # Environment/context injections arrive as user messages wrapped in
            # <environment_context>/<permissions instructions> — skip those.
            if role == "user" and text.lstrip().startswith("<"):
                continue
            if not text:
                continue
            entry: dict[str, Any] = {"role": role, "content": text, "timestamp": d.get("timestamp")}
            if role == "assistant" and pending_tools:
                entry["tool_uses"] = pending_tools
                pending_tools = []
            turns.append(entry)
        elif t in ("custom_tool_call", "function_call", "local_shell_call"):
            pending_tools.append({
                "name": pl.get("name") or t,
                "input": pl.get("input") or pl.get("arguments"),
                "id": pl.get("call_id") or pl.get("id"),
            })
    return turns[-last_n:] if last_n > 0 else turns


def list_turn_points_codex(path: Path) -> list[dict[str, Any]]:
    """Rewind points for a Codex rollout: one per ``turn_context`` line (each
    turn starts with one, followed by the user message). Truncating the file
    just before a ``turn_context`` line drops that turn and everything after —
    verified 2026-09-07: ``exec resume`` reads the rollout JSONL (a truncated
    thread forgot the dropped turn; sqlite still held it, harmlessly)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    points: list[dict[str, Any]] = []
    tc_idx: list[int] = []
    for i, line in enumerate(lines):
        if '"turn_context"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "turn_context":
            tc_idx.append(i)
    for n, idx in enumerate(tc_idx):
        end = tc_idx[n + 1] if n + 1 < len(tc_idx) else len(lines)
        preview = ""
        asst = 0
        tools = 0
        files: list[str] = []
        ts = None
        for line in lines[idx:end]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            pl = d.get("payload") or {}
            if d.get("type") == "turn_context" and ts is None:
                ts = d.get("timestamp")
            if d.get("type") == "response_item":
                t = pl.get("type")
                if t == "message" and pl.get("role") == "user" and not preview:
                    txt = _content_text(pl.get("content")).strip()
                    if txt and not txt.startswith("<"):
                        preview = txt.replace("\n", " ")[:100]
                elif t == "message" and pl.get("role") == "assistant":
                    asst += 1
                elif t in ("custom_tool_call", "function_call", "local_shell_call"):
                    tools += 1
                    inp = str(pl.get("input") or pl.get("arguments") or "")
                    if "apply_patch" in inp:
                        for mm in re.finditer(r"\*\*\* (?:Add|Update|Delete) File: ([^\n\\]+)", inp):
                            fp = mm.group(1).strip()
                            if fp not in files:
                                files.append(fp)
        points.append({
            "point": n + 1, "raw_line_index": idx, "timestamp": ts, "preview": preview,
            "after_assistant_turns": asst, "after_tool_calls": tools, "after_files": files,
        })
    return points


def _history_db_path() -> Path | None:
    cands = sorted(codex_home().glob("thread_history_*.sqlite"))
    return cands[-1] if cands else None


def resync_history_after_truncate(thread_id: str, cut_ordinal: int, path: Path) -> str:
    """Bring Codex's sqlite history PROJECTION back in step with a truncated
    rollout. Codex keeps ``thread_history_projection_state(next_rollout_ordinal,
    next_rollout_byte_offset)`` + projected ``thread_turns`` / ``thread_items``
    rows; ``exec resume`` reads the rollout and simply appends, but ``exec
    fork`` (and the Desktop history view) go through the projection, which
    hard-errors once the rollout's ordinals no longer match ("expected ordinal
    23, got 22"). Verified 2026-09-07: deleting the projected rows at/after
    the cut and re-pointing the projection to (cut, new file size) makes fork
    work again and the projector re-projects the resumed turns itself.
    Best-effort: returns a one-line outcome, never raises."""
    db = _history_db_path()
    if db is None:
        return "no codex history sqlite found — projection left as is (fork may fail until Codex rebuilds it)"
    try:
        offset = path.stat().st_size
        con = sqlite3.connect(str(db), timeout=10)
        try:
            with con:
                for table in ("thread_items", "thread_turns", "thread_realtime_items"):
                    exists = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
                    if not exists and table == "thread_realtime_items":
                        continue  # optional on older Codex builds
                    # Any incompatible required table/column must roll back
                    # all deletes, without advancing the projection cursor.
                    con.execute(f"delete from {table} where thread_id=? and rollout_ordinal>=?",
                                (thread_id, cut_ordinal))
                n = con.execute(
                    "update thread_history_projection_state set next_rollout_ordinal=?, "
                    "next_rollout_byte_offset=? where thread_id=?",
                    (cut_ordinal, offset, thread_id),
                ).rowcount
        finally:
            con.close()
        if n:
            return f"codex history projection re-synced to ordinal {cut_ordinal} (fork keeps working)"
        return "no projection row for this thread (nothing to re-sync)"
    except Exception as e:  # noqa: BLE001
        return f"codex history projection re-sync failed ({e}); pair_fork may fail on this thread until Codex rebuilds it"


def truncate_rollout_before_line(path: Path, raw_line_index: int,
                                 thread_id: str | None = None) -> tuple[int, str | None]:
    """Keep lines [0, raw_line_index); drop the rest (atomic via tmp +
    replace), then re-sync Codex's sqlite projection when ``thread_id`` is
    given. Returns ``(lines_dropped, resync_note)``."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = lines[:raw_line_index]
    dropped = len(lines) - len(kept)
    cut_ordinal = raw_line_index
    try:
        cut_ordinal = int(json.loads(lines[raw_line_index]).get("ordinal", raw_line_index))
    except Exception:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    os.replace(tmp, path)
    note = resync_history_after_truncate(thread_id, cut_ordinal, path) if thread_id else None
    return dropped, note


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


HANDOFF_CODEX_OVERHEAD_TOKENS = 13_000


def delete_thread(thread_id: str) -> None:
    """Delete an imported thread; callers must report failures as orphaned IDs."""
    args = [codex_executable(), "delete", "--force", thread_id]
    try:
        result = run_codex(args, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CLIError(f"could not delete Codex thread {thread_id}: {exc}") from exc
    if result.returncode:
        raise CLIError(f"could not delete Codex thread {thread_id}",
                       stderr=result.stderr or result.stdout, exit_code=result.returncode)


def import_claude_session(path: Path, *, cwd: str, title: str,
                         timeout_seconds: int = 60) -> dict[str, str]:
    """Import exactly one Claude session without inference or config migration."""
    with AppServerClient([codex_executable(), "app-server"] + config_readd_args(),
                         cwd=cwd, timeout_seconds=timeout_seconds, kill=_tree_kill) as client:
        client.initialize()
        result = client.request("externalAgentConfig/import", {
            "migrationItems": [{
                "itemType": "SESSIONS", "description": f"Import Claude pair as {title}",
                "cwd": cwd, "details": {"sessions": [{
                    "cwd": cwd, "path": str(path), "title": title,
                }]},
            }],
        })
        import_id = result.get("importId") if isinstance(result, dict) else None
        if not isinstance(import_id, str) or not import_id:
            client.fail("Codex session import returned no importId")
        while time.monotonic() < client.deadline:
            for message in client.messages():
                if message.get("method") != "externalAgentConfig/import/completed":
                    continue
                params = message.get("params") or {}
                if params.get("importId") != import_id:
                    continue
                targets: list[str] = []
                failures: list = []
                for item in params.get("itemTypeResults") or []:
                    if item.get("itemType") != "SESSIONS":
                        continue
                    failures.extend(item.get("failures") or [])
                    targets.extend(success["target"] for success in item.get("successes") or []
                                   if isinstance(success.get("target"), str) and success["target"])
                if len(targets) != 1 or failures:
                    cleanup: list[str] = []
                    for thread_id in set(targets):
                        try:
                            delete_thread(thread_id)
                        except Exception as exc:
                            cleanup.append(f"orphaned thread {thread_id}: {exc}")
                    client.fail(f"Codex import {import_id} failed: expected one session target; "
                                f"got {len(targets)}, failures={json.dumps(failures)[:500]}. "
                                + "; ".join(cleanup))
                return {"thread_id": targets[0], "import_id": import_id,
                        "imported_at": datetime.utcnow().isoformat() + "Z"}
            if client.proc.poll() is not None:
                break
            time.sleep(0.2)
        client.fail(f"Codex import {import_id} ended without a completion notification; "
                    "no pair registered. An imported thread may need cleanup if the import completed late.")


def measure_imported_history(thread_id: str, briefing: str) -> int:
    """Estimate imported visible history plus Codex overhead and the first turn."""
    path = rollout_path_for(thread_id)
    if path is None:
        raise CLIError(f"cannot measure imported Codex thread {thread_id}: rollout missing")
    chars = 0
    seeded = 0
    with path.open(encoding="utf-8") as rollout:
        for raw in rollout:
            if not raw.strip():
                continue
            # Fail closed on a partial/corrupt rollout instead of undercounting it.
            event = json.loads(raw)
            payload = event.get("payload") or {}
            if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                # Ignore empty quota-only ticks; retain the last nonzero seed.
                info = payload.get("info") or {}
                for key in ("last_token_usage", "total_token_usage"):
                    usage = info.get(key) or {}
                    count = int(usage.get("total_tokens") or 0)
                    if not count:
                        count = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                    if count > 0:
                        seeded = count
                        break
            if event.get("type") != "response_item":
                continue
            kind = payload.get("type")
            if kind == "message":
                chars += len(_content_text(payload.get("content")))
            elif kind in ("function_call", "function_call_output", "custom_tool_call",
                          "custom_tool_call_output", "local_shell_call", "tool_call", "tool_result"):
                chars += len(json.dumps(payload, ensure_ascii=False))
    history = seeded if seeded > 0 else (chars + 3) // 4
    return history + HANDOFF_CODEX_OVERHEAD_TOKENS + (len(briefing) + 3) // 4


@dataclass(frozen=True)
class HandoffSize:
    tokens: int
    default_window: int
    max_window: int
    context_window: str

    @property
    def chosen_window(self) -> int:
        return self.max_window if self.context_window == "1m" else self.default_window

    @property
    def refused(self) -> bool:
        return self.tokens * 10 >= self.chosen_window * 7

    @property
    def warning(self) -> bool:
        return self.tokens * 2 >= self.chosen_window

    @property
    def fits_1m(self) -> bool:
        return self.tokens * 10 < self.max_window * 7

    def render(self) -> str:
        used_pct = 100 * self.tokens / self.chosen_window
        return (
            f"≈{self.tokens:,} tokens: {100 * self.tokens / self.default_window:.0f}% of the default "
            f"window (≈{self.default_window / 1000:.0f}k), "
            f"{100 * self.tokens / self.max_window:.0f}% with context_window='1m' "
            f"(≈{self.max_window / 1000:.0f}k). Chosen: {self.context_window}; "
            f"~{max(0.0, 90 - used_pct):.0f}% remains before Codex auto-compacts "
            "(~90% of the window). Includes estimated Codex overhead and the full first message."
        )


class CodexAdapter(PairAdapter):
    backend_name = "codex"

    # ---- public surface ------------------------------------------------

    def create(self, spec: PairSpec, initial_message: str | None = None) -> CreateResult:
        """Eager create: one tiny probe turn materializes the thread (row +
        rollout) and returns its thread id — Codex names the thread itself at
        ``thread.started``, so the caller MUST take ``session_id`` from the
        result (never a pre-generated id)."""
        prompt = initial_message or PROBE_PROMPT
        sys_text = self._resolve_system_prompt(spec)
        if sys_text:
            # Codex exec has no --append-system-prompt; the thread's first user
            # message is the closest persistent equivalent (it stays in the
            # thread history for every later resume).
            prompt = (
                "<operating instructions for this thread — follow them in every turn>\n"
                f"{sys_text}\n</operating instructions>\n\n{prompt}"
            )
        args = self._exec_args(spec, model=None, effort=None, permission_mode=None)
        # Prompt over stdin ("-"): a long briefing as an argv element would
        # exceed Windows' ~32K command-line limit (Astra catch). Verified:
        # ``exec … -`` reads the prompt from stdin and proceeds on EOF.
        args.append("-")
        events, stderr, code, _dur = self._run(args, spec, timeout_seconds=300,
                                               task_label="create", log=False,
                                               stdin_text=prompt)
        tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        failed = next((e for e in events if e.get("type") == "turn.failed"), None)
        if not tid:
            raise CLIError("codex exec produced no thread.started event",
                           stderr=stderr or "\n".join(json.dumps(e)[:200] for e in events[-3:]),
                           exit_code=code)
        if failed or code != 0:
            msg = str((failed or {}).get("error", {}).get("message") if failed else "") or stderr
            if _MODEL_UNSUPPORTED_RE.search(msg or ""):
                raise CLIError(
                    f"the Codex model '{self._model_for(spec)}' is not available on this "
                    f"plan/account ({msg.strip()[:200]}). Pick a listed model (see "
                    f"pair_settings_get) — e.g. 'sol', 'terra' or 'luna'.",
                    stderr=stderr, exit_code=code,
                )
            raise CLIError(f"codex create turn failed: {msg.strip()[:300]}",
                           stderr=stderr, exit_code=code)
        reply = ""
        for e in events:
            if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
                reply = str(e["item"].get("text") or "")
        new_spec = spec.model_copy(update={"session_id": tid})
        tp = self.transcript_path(new_spec)
        return CreateResult(name=spec.name, session_id=tid,
                            transcript_path=str(tp) if tp else None,
                            initial_response=reply or None,
                            notes=connectors.codex_startup_notes(spec.mcp_whitelist, spec.cwd, stderr, events))

    def send(self, spec: PairSpec, message: str, *, model: str | None = None,
             effort: str | None = None, permission_mode: str | None = None,
             timeout_seconds: int | None = 300,
             on_event: Callable[[dict], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             task_id: str | None = None) -> SendResult:
        turn = _new_codex_activity(spec, task_id)
        try:
            if not self.session_exists(spec):
                raise SessionMissing(spec.name, spec.session_id)
            args = self._exec_args(spec, model=model, effort=effort, permission_mode=permission_mode)
            args += ["resume", spec.session_id, "-"]
            run_spec = spec.model_copy(update={
                "model": self._model_for(spec, model),
                "effort": effort if effort is not None else spec.effort,
                "permission_mode": normalize_permission(permission_mode or spec.permission_mode),
            })
            turn.spec = run_spec
            turn.limit = _configured_compact_limit(args)
            started = time.time()
            events, stderr, code, dur_ms = self._run(
                args, run_spec, timeout_seconds=timeout_seconds, task_label="send", log=True,
                on_event=on_event, should_stop=should_stop, task_id=task_id,
                stdin_text=message, activity_turn=turn,
            )
            result = self._build_send_result(
                spec, events, stderr, code, dur_ms, started,
                model_used=run_spec.model, permission_level=run_spec.permission_mode,
            )
            completed = any(e.get("type") == "turn.completed" for e in events)
            failed = any(e.get("type") == "turn.failed" for e in events)
            turn.status = "failed" if failed or (code not in (0, None) and not completed) else "done"
            from claude_squared import async_tasks
            if task_id and async_tasks.was_stopped(task_id):
                turn.status = "stopped"
            turn.preview = result.response
            return result
        except CommandTimeout:
            turn.status = "timeout"
            raise
        except TaskStopped:
            turn.status = "stopped"
            raise
        except BaseException:
            from claude_squared import async_tasks
            if task_id and async_tasks.was_stopped(task_id):
                turn.status = "stopped"
            raise
        finally:
            turn.close()

    def fork(self, spec: PairSpec, sentinel: str | None = None, timeout_seconds: int = 300) -> str:
        """Native ``exec fork <thread-id>`` — no prompt needed (verified: a
        promptless fork with stdin closed creates the new thread, 0 tokens,
        no turn). Returns the new thread id; the source is untouched."""
        args = self._exec_args(spec, model=None, effort=None, permission_mode=None)
        args += ["fork", spec.session_id]
        events, stderr, code, _ = self._run(args, spec, timeout_seconds=timeout_seconds,
                                            task_label="fork", log=False)
        tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        if not tid or tid == spec.session_id:
            raise CLIError(f"codex exec fork did not produce a new thread id (got {tid!r})",
                           stderr=stderr, exit_code=code)
        return tid

    def compact(self, spec: PairSpec, steering_prompt: str | None = None,
                timeout_seconds: int = 600,
                should_stop: Callable[[], bool] | None = None) -> CompactResult:
        """Manual compaction through ``codex app-server`` (stdio JSON-RPC).

        ``codex exec`` has no compaction command (a literal ``/compact`` prompt
        is just text the model answers "Compacted." to — verified), but the
        app-server protocol has ``thread/compact/start``: initialize →
        ``thread/resume`` (with a per-thread ``config`` that blanks
        ``mcp_servers`` so this MCP isn't started recursively; the Desktop's
        plugin servers still start, ~10 s) → ``thread/compact/start`` → wait
        for the ``contextCompaction`` item + ``turn/completed``. The rollout
        gains a ``compacted`` line; a later ``exec resume`` continues from the
        summary (verified 0.153.4: the thread still knew its code word).
        Codex compaction takes no steering text — ``steering_prompt`` is
        ignored with a note.
        """
        if not self.session_exists(spec):
            raise SessionMissing(spec.name, spec.session_id)
        rollout = self.transcript_path(spec)
        tc0 = read_last_token_count(rollout) or {}
        pre = live_context_tokens(tc0.get("info")) or 0
        t0 = time.monotonic()
        outcome = self._appserver_compact(spec, timeout_seconds=timeout_seconds, should_stop=should_stop)
        dur_ms = int((time.monotonic() - t0) * 1000)
        tc1 = read_last_token_count(rollout) or {}
        # Never total_token_usage: it is cumulative thread usage, not the
        # context (2026-10-15: reported 318,367 -> 56,802,796 and tripped the
        # context alert). Codex's post-compaction token_count carries its own
        # estimate of the new history in last_token_usage.total_tokens.
        post = _post_compaction_tokens(tc0, tc1) or 0
        notes: list[str] = []
        if post:
            notes.append("post-compaction size is Codex's estimate of the compacted history; "
                         "the next reply's footer shows the real context fill")
        else:
            # Older Codex wrote all zeros here (verified on a 164k thread);
            # estimate from the compaction record's replacement history
            # instead — the next reply's footer shows the real fill.
            est = _estimate_compacted_tokens(rollout)
            if est:
                post = est
                notes.append("post-compaction size is an estimate from the summary (~chars/4); "
                             "the next reply's footer shows the real context fill")
        if steering_prompt:
            notes.append("steering_prompt ignored: Codex compaction takes no steering text")
        if outcome:
            notes.append(outcome)
        note = "; ".join(notes) if notes else None
        try:
            log_dir = logs_dir() / spec.name
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "main.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] === COMPACTED {pre:,} -> {post:,} "
                        f"tokens (manual, codex app-server) ===\n")
        except Exception:
            pass
        return CompactResult(name=spec.name, session_id=spec.session_id, pre_tokens=pre,
                             post_tokens=post, duration_ms=dur_ms, trigger="manual (app-server)",
                             summary_preview=note)

    def _appserver_compact(self, spec: PairSpec, *, timeout_seconds: int,
                           should_stop: Callable[[], bool] | None = None) -> str | None:
        """Drive compaction through the shared stdio JSON-RPC client."""
        client = AppServerClient(
            [codex_executable(), "app-server"] + config_readd_args(),
            cwd=spec.cwd or None, timeout_seconds=timeout_seconds, kill=_tree_kill,
            should_stop=should_stop,
            stop_message="compaction stopped by pair_stop (codex app-server tree-killed; "
                         "the thread is unchanged unless the compaction record was already written)",
        )
        try:
            with client:
                with _INFLIGHT_LOCK:
                    _INFLIGHT[spec.name] = {
                        "proc": client.proc, "task_id": None, "started_at": datetime.utcnow(),
                        "last_activity": datetime.utcnow(), "label": "compact",
                    }
                client.initialize()
                cfg: dict[str, Any] = {"mcp_servers": {}}
                if spec.effort:
                    cfg["model_reasoning_effort"] = spec.effort
                for k, v in (spec.backend_options or {}).get("config", {}).items():
                    cfg[k] = v
                client.request("thread/resume", {
                    "threadId": spec.session_id, "cwd": spec.cwd or os.getcwd(),
                    "model": self._model_for(spec), "sandbox": "read-only",
                    "config": cfg, "excludeTurns": True,
                })
                client.request("thread/compact/start", {"threadId": spec.session_id})
                # Keep the existing compaction notification/status interpretation.
                saw_item = False
                turn_status: str | None = None
                turn_error: str | None = None
                while time.monotonic() < client.deadline and client.proc.poll() is None:
                    client.check_stop()
                    for message in client.messages():
                        method = message.get("method")
                        params = message.get("params") or {}
                        if params.get("threadId") not in (None, spec.session_id):
                            continue
                        if method == "item/completed" and (params.get("item") or {}).get("type") == "contextCompaction":
                            saw_item = True
                        elif method == "thread/compacted":
                            saw_item = True
                        elif method == "turn/completed":
                            turn = params.get("turn") or {}
                            turn_status = str(turn.get("status") or "completed")
                            if turn.get("error"):
                                turn_error = json.dumps(turn["error"])[:300]
                        elif method == "error":
                            turn_error = json.dumps(params)[:300]
                            turn_status = "failed"
                    if turn_status is not None:
                        break
                    time.sleep(0.3)
                if turn_status is None:
                    _tree_kill(client.proc)
                    raise CommandTimeout(spec.name, int(timeout_seconds))
                if turn_error or (turn_status not in ("completed",) and not saw_item):
                    client.fail(f"codex compaction did not complete (status {turn_status}): "
                                f"{turn_error or 'no contextCompaction item observed'}")
                return None if saw_item else f"turn completed with status {turn_status} but no contextCompaction item was observed"
        finally:
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(spec.name, {}).get("proc") is client.proc:
                    _INFLIGHT.pop(spec.name, None)

    def context(self, spec: PairSpec, timeout_seconds: int = 60) -> ContextReport:
        """Zero-inference: the rollout's last ``token_count`` event."""
        tc = read_last_token_count(self.transcript_path(spec))
        if not tc:
            raise CLIError(f"no token_count recorded yet for pair '{spec.name}' (send a message first)")
        info = tc.get("info") or {}
        last = info.get("last_token_usage") or {}
        total = info.get("total_token_usage") or {}
        window = int(info.get("model_context_window") or 0)
        used = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
        compacted = not last.get("input_tokens") and bool(live_context_tokens(info))
        if compacted:
            used = live_context_tokens(info) or 0
        pct = (used / window * 100) if window else 0.0
        cw = context_windows(spec.model)
        md = [
            f"**Model:** {spec.model}",
            f"**Tokens:** {_fmt_tokens(used)} / {_fmt_tokens(window)} ({pct:.0f}%)"
            + (" — Codex's estimate after compaction; the next reply reports the real fill" if compacted else ""),
            f"- last call: input {last.get('input_tokens', 0):,} (cached {last.get('cached_input_tokens', 0):,}), "
            f"output {last.get('output_tokens', 0):,} (reasoning {last.get('reasoning_output_tokens', 0):,})",
            f"- thread total: input {total.get('input_tokens', 0):,}, output {total.get('output_tokens', 0):,}, "
            f"all {total.get('total_tokens', 0):,}",
            f"- context_window setting: {spec.context_window}"
            + (f" (this model: {cw[0]:,} usable by default, {cw[1]:,} with '1m')" if cw else ""),
        ]
        pu = plan_usage_text(tc.get("rate_limits"))
        if pu:
            md.append(f"- plan usage: {pu}")
        md.append("(zero inference — read from the thread's rollout; Codex has no /context command)")
        return ContextReport(name=spec.name, session_id=spec.session_id, model=spec.model,
                             tokens_used=used, tokens_max=window, percent=pct,
                             raw_markdown="\n".join(md))

    def invoke_skill(self, spec: PairSpec, skill_name: str, args: str | None = None,
                     timeout_seconds: int = 300) -> SendResult:
        raise CLIError(
            f"pair_invoke is not available for Codex pairs ('{spec.name}'): codex exec has "
            f"no slash-command channel. Ask for the behaviour in plain language via "
            f"pair_send instead (Codex skills under ~/.codex/skills are picked up by name)."
        )

    def transcript_path(self, spec: PairSpec) -> Path | None:
        if not spec.session_id:
            return None
        return rollout_path_for(spec.session_id)

    def session_exists(self, spec: PairSpec) -> bool:
        p = self.transcript_path(spec)
        return p is not None and p.exists()

    # ---- arg building --------------------------------------------------

    @staticmethod
    def _model_for(spec: PairSpec, override: str | None = None) -> str:
        slug, _note = resolve_codex_model(override or spec.model)
        return slug

    def _resolve_system_prompt(self, spec: PairSpec) -> str | None:
        from claude_squared.registry import profiles_dir
        parts = []
        if spec.profile_name:
            pp = profiles_dir() / f"{spec.profile_name}.md"
            if pp.exists():
                parts.append(pp.read_text(encoding="utf-8").strip())
        if spec.system_prompt_append:
            parts.append(spec.system_prompt_append.strip())
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def validate_backend_options(level: str, backend_options: dict[str, Any] | None) -> None:
        """Reject combinations the CLI itself refuses. Verified 0.153.4:
        ``--sandbox`` "cannot be used with ``--approve-for-me``" — the
        approve-for-me flag IMPLIES the workspace-write sandbox (help text +
        the recorded sandbox_policy of every auto-level probe thread), so an
        explicit ``backend_options.sandbox`` at the ``auto`` level has no
        legal spelling. Raises ``ValueError`` with the remedy."""
        bo = backend_options or {}
        if not bo:
            return
        lvl = normalize_permission(level)
        _sandbox, approval = CODEX_PERMISSION_MAP[lvl]
        if bo.get("approval"):
            a = str(bo["approval"]).lower()
            if a not in ("none", "never", "approve-for-me", "bypass"):
                raise ValueError(
                    f"backend_options.approval must be none|approve-for-me|bypass (got {a!r})")
            approval = None if a in ("none", "never") else a
        sb = bo.get("sandbox")
        if sb and str(sb) not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError(
                f"backend_options.sandbox must be read-only|workspace-write|danger-full-access (got {sb!r})")
        if sb and approval == "approve-for-me":
            raise ValueError(
                "backend_options.sandbox cannot be combined with the 'auto' level: codex exec "
                "rejects -s together with --approve-for-me (which already implies the "
                "workspace-write sandbox). Use permission_mode='workspace' (+ sandbox) or set "
                "backend_options.approval='none'."
            )

    @staticmethod
    def permission_args(level: str, backend_options: dict[str, Any] | None = None) -> list[str]:
        """Neutral level → exec flags, honoring ``backend_options.sandbox`` /
        ``backend_options.approval`` native overrides.

        ``auto`` emits ONLY ``--approve-for-me``: the flag implies the
        workspace-write sandbox and the CLI refuses an explicit ``-s`` next to
        it (verified 0.153.4). ``unrestricted`` emits only the bypass flag.
        """
        CodexAdapter.validate_backend_options(level, backend_options)
        lvl = normalize_permission(level)
        sandbox, approval = CODEX_PERMISSION_MAP[lvl]
        bo = backend_options or {}
        if bo.get("sandbox"):
            sandbox = str(bo["sandbox"])
        if bo.get("approval"):
            a = str(bo["approval"]).lower()
            approval = None if a in ("none", "never") else a
        if approval == "bypass":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        args: list[str] = []
        if approval == "approve-for-me":
            args.append("--approve-for-me")
        elif sandbox:
            args += ["-s", sandbox]
        return args

    def _exec_args(self, spec: PairSpec, *, model: str | None, effort: str | None,
                   permission_mode: str | None) -> list[str]:
        exe = codex_executable()
        cwd = spec.cwd or os.getcwd()
        args = [exe, "exec", "--json", "--skip-git-repo-check", "-C", cwd,
                "--ignore-user-config", "--thread-source", THREAD_SOURCE]
        args += config_readd_args()
        args += connectors.codex_args(spec.mcp_whitelist, permission_mode or spec.permission_mode, cwd)
        slug = self._model_for(spec, model)
        args += ["-m", slug]
        eff = effort if effort is not None else spec.effort
        if eff:
            args += ["-c", f"model_reasoning_effort={_toml_literal(str(eff))}"]
        if spec.context_window == "1m":
            # The documented recipe is window=1,000,000 / auto-compact=900,000
            # (what the Codex app writes). The server clamps the window to the
            # model's ceiling (828,400 usable on the 5.6 family), which would
            # leave a 900k compact threshold unreachable — so the threshold is
            # derived from the model's EFFECTIVE max window when the cache is
            # readable (90% of it, reachable whether Codex compares against raw
            # or effective counts — unverified which). Historian catch.
            args += ["-c", f"model_context_window={CODEX_1M_CONFIG['model_context_window']}"]
            cw = context_windows(slug)
            limit = int(cw[1] * 0.9) if cw and cw[1] else CODEX_1M_CONFIG["model_auto_compact_token_limit"]
            args += ["-c", f"model_auto_compact_token_limit={limit}"]
        bo = spec.backend_options or {}
        for k, v in (bo.get("config") or {}).items():
            args += ["-c", f"{k}={_toml_literal(v)}"]
        args += self.permission_args(permission_mode or spec.permission_mode, bo)
        for d in spec.extra_dirs or []:
            args += ["--add-dir", d]
        for extra in bo.get("args") or []:
            args.append(str(extra))
        return args

    # ---- subprocess + logging --------------------------------------------

    def _run(self, args: list[str], spec: PairSpec, *, timeout_seconds: int | None,
             task_label: str, log: bool,
             on_event: Callable[[dict], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             task_id: str | None = None,
             stdin_text: str | None = None,
             activity_turn: _CodexActivity | _NoCodexActivity | None = None,
             ) -> tuple[list[dict], str, int | None, int]:
        """File-backed exec: parent death must not break the child's next write.

        Verified 2026-09-19: Codex dies writing to orphaned PIPEs; ordinary
        files survive. Tail complete lines only, without locking/renaming any
        file inherited by the child. Prompt EOF is supplied by a regular file.
        """
        from claude_squared import async_tasks, codex_turns

        log_dir = (logs_dir() / spec.name).resolve()
        turn_dir = log_dir / "turns"
        turn_dir.mkdir(parents=True, exist_ok=True)
        codex_turns.prune_turns(turn_dir)
        main_log = log_dir / "main.log"
        counter = ToolCounter(index_path=log_dir / "main.idx.json") if log else None
        start_line = self._count_lines(main_log) + 1
        tags: dict[str, str] = {}
        run_id = task_id or uuid.uuid4().hex
        events_path = turn_dir / f"{run_id}.events.jsonl"
        stderr_path = turn_dir / f"{run_id}.stderr.log"
        stdin_path = turn_dir / f"{run_id}.stdin.txt"
        stdin_path.write_bytes((stdin_text or "").encode("utf-8"))
        started_at = datetime.utcnow()
        # send passes the effective per-call spec, including overrides. Keep
        # the result builder's permission level, even with native bo overrides.
        model_used = args[args.index("-m") + 1] if "-m" in args else self._model_for(spec)
        permission = normalize_permission(spec.permission_mode)
        receipt = {
            "run_id": run_id, "task_id": task_id, "spec": spec.model_dump(mode="json"),
            "model_used": model_used, "permission_level": permission,
            "started_epoch": time.time(), "log_path": str(main_log),
            "logged_offset": 0, "start_line": start_line, "log": log,
            "auto_compact_token_limit": _configured_compact_limit(args),
        }
        codex_turns.save_receipt(events_path, receipt)

        log_available = log
        log_lock = threading.RLock()
        reader_closed = threading.Event()

        def _log(lines: list[str], offset: int | None = None) -> None:
            nonlocal log_available
            with log_lock:
                if reader_closed.is_set() and threading.current_thread() is reader:
                    return
                if log_available and lines:
                    try:
                        codex_turns.append_log(events_path, receipt, lines,
                                               receipt["logged_offset"] if offset is None else offset)
                    except Exception:
                        # Logging was best-effort before file-backed exec too.
                        # Don't kill useful work on a main.log/receipt disk error,
                        # or overwrite a pending append with the next event. The
                        # durable checkpoint lets recovery replay the remainder.
                        log_available = False

        popen_kwargs: dict[str, Any] = {"cwd": spec.cwd or None}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        t0 = time.monotonic()
        if activity_turn is not None:
            activity_turn.poll(force=True)
        try:
            with open(stdin_path, "rb") as inp, open(events_path, "wb") as out, open(stderr_path, "wb") as err:
                proc = popen_codex(args, stdin=inp, stdout=out, stderr=err, **popen_kwargs)
        except OSError as e:
            msg = codex_spawn_error(args, e, spec.cwd)
            # No child was published, so there is nothing to recover: record
            # the attempt in the pair's own log and drop the run's files.
            if log:
                ts = datetime.now().strftime("%H:%M:%S")
                try:
                    with open(main_log, "a", encoding="utf-8") as f:
                        f.write(f"[{ts}] === CODEX TURN START (pair={spec.name}, model={model_used}, "
                                f"exe={args[0]}) ===\n"
                                f"[{ts}] === TURN ERROR: {msg.replace(chr(10), ' / ')[:500]} ===\n")
                except OSError:
                    pass
            for suffix in ("events.jsonl", "events.receipt.json", "stderr.log", "stdin.txt"):
                (turn_dir / f"{run_id}.{suffix}").unlink(missing_ok=True)
            raise CLIError(msg)
        try:
            from claude_squared import process_identity
            receipt["child_pid"] = proc.pid
            receipt["child_started_at"] = process_identity.started_at(proc.pid, handle=getattr(proc, "_handle", None))
            codex_turns.save_receipt(events_path, receipt)
            async_tasks.mark_codex_child(task_id, proc.pid, str(events_path), receipt["child_started_at"])
        except BaseException:
            # Never leave an unobservable child if publishing its identity fails.
            _tree_kill(proc)
            raise
        with _INFLIGHT_LOCK:
            _INFLIGHT[spec.name] = {"proc": proc, "task_id": task_id, "started_at": started_at,
                                    "last_activity": started_at, "label": task_label, "effort": spec.effort}

        events: list[dict] = []
        stderr_chunks: list[str] = []
        reader_errors: list[Exception] = []

        def _reader() -> None:
            try:
                with open(events_path, "rb") as stdout, open(stderr_path, "rb") as stderr_file:
                    while True:
                        if reader_closed.is_set():
                            return
                        # Poll BEFORE draining: once exit is known, this drain
                        # includes the final writes (no read-EOF/exit race).
                        exited = proc.poll() is not None
                        batch: list[tuple[int, dict]] = []
                        for offset, line in codex_turns.complete_lines(stdout, max_lines=None if exited else 64):
                            try:
                                ev = json.loads(line)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(ev, dict):
                                batch.append((offset, ev))
                        with log_lock:
                            if reader_closed.is_set():
                                return
                            output: list[str] = []
                            for offset, ev in batch:
                                if activity_turn is not None:
                                    activity_turn.observe(ev, position=offset)
                                with _INFLIGHT_LOCK:
                                    info = _INFLIGHT.get(spec.name)
                                    if info is not None and info.get("proc") is proc:
                                        info["last_activity"] = datetime.utcnow()
                                if log:
                                    output.extend(self._safe_format_event(ev, counter, tags,
                                                  detail_dir=log_dir, run_id=run_id))
                            for _, line in codex_turns.complete_lines(stderr_file, max_lines=None if exited else 64):
                                if "Reading additional input from stdin" in line:
                                    continue
                                if _DENIAL_RE.search(line):
                                    ts = datetime.now().strftime("%H:%M:%S")
                                    output.append(f"[{ts}] [sandbox/approval DENIED] {line.strip()[:300]}")
                            _log(output, batch[-1][0] if batch else None)
                        for _, ev in batch:
                            if reader_closed.is_set():
                                return
                            if on_event is not None:
                                try:
                                    on_event(ev)
                                except Exception:
                                    pass
                        if exited:
                            # Stderr can end without a newline; JSONL cannot.
                            stderr_chunks.append(stderr_file.read().decode("utf-8", errors="replace"))
                            return
                        time.sleep(0.1)
            except Exception as exc:
                reader_errors.append(exc)

        deadline = (t0 + timeout_seconds) if timeout_seconds else None
        killed_reason: str | None = None
        last_stop_check = 0.0
        reader = threading.Thread(target=_reader, daemon=True)
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            _log([f"[{ts}] === CODEX TURN START (pair={spec.name}, model={model_used}, "
                  f"thread={spec.session_id[:8] if spec.session_id else 'new'}, exe={args[0]}) ==="])
            reader.start()
            # Keep supervision independent of on_event: a slow consumer must
            # not delay cancellation or the hard timeout (same as PIPE reader).
            while proc.poll() is None:
                now = time.monotonic()
                if reader_errors:
                    raise reader_errors[0]
                if should_stop is not None and now - last_stop_check >= 1.0:
                    last_stop_check = now
                    try:
                        stop = should_stop()
                    except Exception:
                        stop = False
                    if stop:
                        killed_reason = "stopped"
                        if activity_turn is not None:
                            activity_turn.status = "stopped"
                        _tree_kill(proc)
                        break
                if deadline is not None and now >= deadline:
                    killed_reason = "timeout"
                    if activity_turn is not None:
                        activity_turn.status = "timeout"
                    _tree_kill(proc)
                    break
                if activity_turn is not None:
                    activity_turn.poll()
                time.sleep(0.2)
            reader.join(timeout=5)
            if reader_errors:
                raise reader_errors[0]
        except BaseException:
            _tree_kill(proc)
            raise
        finally:
            # A callback may outlast the bounded join. It must not later
            # overwrite the receipt after the send's bookkeeping has started.
            with log_lock:
                reader_closed.set()
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(spec.name, {}).get("proc") is proc:
                    _INFLIGHT.pop(spec.name, None)
        code = proc.poll()
        dur_ms = int((time.monotonic() - t0) * 1000)
        # A slow callback can outlast join(5). Files, not the observer's
        # in-memory prefix, are authoritative after process exit.
        rows = codex_turns.read_events(events_path)
        events = [event for _, event in rows]
        if activity_turn is not None:
            for position, event in rows:
                activity_turn.observe(event, position=position)
            activity_turn.poll(force=True)
            if killed_reason:
                activity_turn.status = killed_reason
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        try:
            receipt = codex_turns.read_receipt(events_path)
            self._replay_events(events_path, receipt, rows)
            log_available = log
        except Exception as exc:
            events.append({"type": "_log_warning", "message": f"event log replay failed: {exc}; events at {events_path}"})
        if killed_reason:
            _log([f"[{datetime.now().strftime('%H:%M:%S')}] === TURN {killed_reason.upper()} ({dur_ms}ms) ==="])
        elif not any(e.get("type") in ("turn.completed", "turn.failed") for e in events):
            _log([f"[{datetime.now().strftime('%H:%M:%S')}] === TURN ERROR ({dur_ms}ms) exit={code} ==="])
        if killed_reason == "timeout":
            raise CommandTimeout(spec.name, int(timeout_seconds or 0))
        if killed_reason == "stopped":
            raise CLIError("turn stopped by pair_stop (codex process tree-killed; the thread "
                           "resumes cleanly on the next send)", stderr=stderr)
        events.append({"type": "_log_scope", "log_path": str(main_log),
                       "start_line": start_line, "end_line": self._count_lines(main_log)})
        return events, stderr, code, dur_ms

    @staticmethod
    def _safe_format_event(ev: dict, counter: ToolCounter | None, tags: dict[str, str], **kwargs) -> list[str]:
        try:
            return CodexAdapter._format_event(ev, counter, tags, **kwargs)
        except Exception as exc:
            # Formatting is observability, never grounds to kill useful work.
            return [f"[{datetime.now().strftime('%H:%M:%S')}] [event formatting skipped] {type(exc).__name__}: {exc}"]

    def _replay_events(self, events_path: Path, receipt: dict, rows: list[tuple[int, dict]], *,
                       recovered: bool = False) -> None:
        from claude_squared import codex_turns

        codex_turns.finish_pending_log(events_path, receipt)
        if not receipt.get("log", True):
            return
        log_dir = Path(receipt["log_path"]).parent
        counter = ToolCounter(index_path=log_dir / "main.idx.json")
        tags: dict[str, str] = {}
        output = ([f"[{datetime.now().strftime('%H:%M:%S')}] === recovered after owner death ==="]
                  if recovered and not receipt.get("replay_logged") else [])
        offset = receipt["logged_offset"]
        for next_offset, event in rows:
            if next_offset <= offset:
                continue
            output.extend(self._safe_format_event(event, counter, tags, detail_dir=log_dir,
                                                  run_id=receipt["run_id"]))
            offset = next_offset
        if output:
            # The marker flag travels with the pending append, so a crash
            # retries its bytes instead of adding another replay marker.
            if recovered:
                receipt["replay_logged"] = True
            codex_turns.append_log(events_path, receipt, output, offset)

    @staticmethod
    def _count_lines(p: Path) -> int:
        if not p.exists():
            return 0
        try:
            with open(p, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    @staticmethod
    def _format_event(ev: dict, counter: ToolCounter | None, tags: dict[str, str], *,
                      detail_dir: Path | None = None, run_id: str | None = None) -> list[str]:
        """Codex ``--json`` event → main.log lines (T-N tagged like Claude's)."""
        ts = datetime.now().strftime("%H:%M:%S")
        t = ev.get("type")
        out: list[str] = []
        if t == "thread.started":
            out.append(f"[{ts}] === THREAD {str(ev.get('thread_id'))[:8]} ===")
        elif t == "item.started" or t == "item.completed":
            it = ev.get("item") or {}
            ity = it.get("type")
            iid = str(it.get("id") or "")
            # One PHYSICAL line per log line — a multi-line reply would
            # otherwise desync the recorded line ranges (Astra catch).
            if ity == "agent_message":
                if t == "item.completed":
                    txt = (it.get("text") or "").strip().replace("\r", "").replace("\n", " / ")
                    if txt:
                        out.append(f"[{ts}] [text] {txt[:400]}")
            elif ity == "reasoning":
                if t == "item.completed":
                    txt = str(it.get("text") or it.get("summary") or "").replace("\n", " / ")
                    if txt:
                        out.append(f"[{ts}] [thinking] {txt[:300]}")
            elif ity == "error":
                out.append(f"[{ts}] [error] {str(it.get('message')).replace(chr(10), ' / ')[:300]}")
            else:
                # command_execution / file_change / mcp_tool_call / web_search …
                if t == "item.started":
                    tag = counter.next_id_for(iid, tool_name=str(ity)) if counter else "T-?"
                    tags[iid] = tag
                    out.append(f"[{ts}] [{tag}] [tool_use] {ity}({CodexAdapter._item_preview(it)})")
                else:
                    # Codex item IDs can repeat in the next exec invocation.
                    # Only this run's map may correlate a completion.
                    tag = tags.get(iid, "T-?")
                    if tag == "T-?" and counter:
                        tag = counter.next_id_for(iid, tool_name=str(ity))
                        tags[iid] = tag
                        out.append(f"[{ts}] [{tag}] [tool_use] {ity}({CodexAdapter._item_preview(it)})")
                    status = it.get("status")
                    err = " ERR" if (status not in (None, "completed") or (it.get("exit_code") not in (None, 0))) else ""
                    res = it.get("aggregated_output")
                    if res is None:
                        res = json.dumps({k: v for k, v in it.items() if k not in ("id", "type")}, default=str)
                    preview = str(res).replace("\n", " / ")[:160]
                    exit_s = f"exit {it.get('exit_code')} " if it.get("exit_code") is not None else ""
                    out.append(f"[{ts}] [{tag}] [tool_result{err}] {exit_s}{preview}")
                if detail_dir is not None and tag != "T-?":
                    try:
                        save_codex_item(detail_dir, tag, it, completed=t == "item.completed", run_id=run_id)
                    except (OSError, ValueError, TypeError) as exc:
                        out.append(f"[{ts}] [tool detail unavailable] {type(exc).__name__}")
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            out.append(f"[{ts}] === TURN COMPLETED (in {u.get('input_tokens', 0):,} / cached "
                       f"{u.get('cached_input_tokens', 0):,} / out {u.get('output_tokens', 0):,} "
                       f"/ reasoning {u.get('reasoning_output_tokens', 0):,} tokens) ===")
        elif t == "turn.failed":
            out.append(f"[{ts}] === TURN FAILED: {str((ev.get('error') or {}).get('message'))[:300]} ===")
        return out

    @staticmethod
    def _item_preview(it: dict) -> str:
        ity = it.get("type")
        if ity == "command_execution":
            cmd = str(it.get("command") or "")
            # Drop the PowerShell wrapper prefix (and its quoting) for readability.
            cmd = re.sub(r'^"?[A-Za-z]:\\[^"]*powershell\.exe"?\s+-Command\s+', "", cmd).strip()
            if len(cmd) >= 2 and cmd[0] == cmd[-1] and cmd[0] in ('"', "'"):
                cmd = cmd[1:-1]
            return cmd[:140]
        if ity == "file_change":
            ch = it.get("changes") or []
            return ", ".join(f"{c.get('kind')} {os.path.basename(str(c.get('path') or ''))}" for c in ch)[:140]
        return json.dumps({k: v for k, v in it.items() if k not in ("id", "type", "status")}, default=str)[:140]

    # ---- result assembly -------------------------------------------------

    def _build_send_result(self, spec: PairSpec, events: list[dict], stderr: str,
                           code: int | None, dur_ms: int, started_epoch: float, *,
                           model_used: str, permission_level: str) -> SendResult:
        # A malformed optional item must not hide the terminal reply/usage.
        events = [e for e in events if e.get("type") not in ("item.started", "item.completed")
                  or isinstance(e.get("item"), dict)]
        scope = next((e for e in events if e.get("type") == "_log_scope"), {})
        reply = ""
        errors: list[str] = []
        for e in events:
            if e.get("type") == "error":
                errors.append(str(e.get("message") or ""))
            elif e.get("type") == "item.completed":
                it = e.get("item") or {}
                if it.get("type") == "agent_message":
                    reply = str(it.get("text") or "")
                elif it.get("type") == "error":
                    errors.append(str(it.get("message") or ""))
        failed = next((e for e in events if e.get("type") == "turn.failed"), None)
        completed = next((e for e in events if e.get("type") == "turn.completed"), None)
        usage = (completed or {}).get("usage") or {}

        # Context: prefer the rollout's token_count (effective window + last
        # call size); fall back to turn usage vs the cache's window.
        rollout = self.transcript_path(spec)
        tc = read_last_token_count(rollout)
        info = (tc or {}).get("info") or {}
        window = int(info.get("model_context_window") or 0)
        if not window:
            cw = context_windows(model_used)
            window = (cw[1] if spec.context_window == "1m" else cw[0]) if cw else 258_400
        used = live_context_tokens(info) or int(usage.get("input_tokens") or 0)
        pct = (used / window * 100) if window else 0.0
        warning = None
        if pct >= STRONG_WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct:.0f}%). Context near "
                       f"limit — STRONGLY consider pair_compact('{spec.name}') (Codex will "
                       f"auto-compact otherwise); or recreate with context_window='1m'.")
        elif pct >= WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct:.0f}%). Consider "
                       f"pair_compact('{spec.name}') to free context (Codex compaction takes "
                       f"no steering text).")

        notes = connectors.codex_startup_notes(spec.mcp_whitelist, spec.cwd, stderr, events)
        notes.extend(e["message"] for e in events if e.get("type") == "_log_warning")
        denials: list[PermissionDenial] = []
        for line in (stderr or "").splitlines():
            if _DENIAL_RE.search(line):
                tool = "apply_patch" if "patch" in line.lower() else "exec"
                denials.append(PermissionDenial(tool_name=tool,
                                                tool_input={"detail": line.strip()[:300]}))
        # A shell command that the SANDBOX blocked fails with an OS
        # permission error in its own output (no router-level "rejected"
        # line): "Access to the path '…' is denied" (PowerShell), "Permission
        # denied" / "Operation not permitted" (POSIX). Inside a workspace-
        # write sandbox that is the sandbox speaking — report it as a denial
        # so the PAIR HANDOFF fires instead of a silent "BLOCKED" reply.
        if permission_level != "unrestricted":
            for e in events:
                if e.get("type") != "item.completed":
                    continue
                it = e.get("item") or {}
                if it.get("type") != "command_execution":
                    continue
                if it.get("exit_code") in (None, 0) and it.get("status") == "completed":
                    continue
                out = str(it.get("aggregated_output") or "")
                if _OS_DENIED_RE.search(out):
                    cmd = self._item_preview(it)
                    denials.append(PermissionDenial(
                        tool_name="exec", tool_use_id=str(it.get("id") or "") or None,
                        tool_input={"command": cmd[:200], "detail": _OS_DENIED_RE.search(out).group(0)[:200]},
                    ))
        if permission_level in ("workspace", "auto") and _READONLY_DEGRADE_RE.search(stderr or ""):
            notes.append(
                "the Codex sandbox ran READ-ONLY although this pair's level allows workspace "
                "writes — on Windows this means `[windows] sandbox = \"elevated\"` is missing "
                "from ~/.codex/config.toml (open the Codex app once, or add it by hand)."
            )
        guardian: list[str] = []
        if permission_level == "auto":
            for v in guardian_verdicts_since(started_epoch, parent_thread_id=spec.session_id):
                outcome = str(v.get("outcome") or "?")
                rat = str(v.get("rationale") or v.get("raw") or "")[:200]
                risk = v.get("risk_level")
                guardian.append(f"{outcome}" + (f" (risk {risk})" if risk else "") + (f" — {rat}" if rat else ""))
                if outcome.lower() == "deny":
                    denials.append(PermissionDenial(tool_name="guardian-review",
                                                    tool_input={"rationale": rat}))

        safety_signal: str | None = None
        safety_kind: str | None = None
        terminal_error = (failed or {}).get("error") or {}
        terminal_message = str(terminal_error.get("message") or "") if isinstance(terminal_error, dict) else str(terminal_error)
        # Reconnect notices can mention rate limits unrelated to the eventual
        # failure. The terminal message is authoritative for classification.
        err_text = terminal_message or " ".join(errors)
        rl = (tc or {}).get("rate_limits") or {}
        if failed or (code not in (0, None) and not completed):
            if _MODEL_UNSUPPORTED_RE.search(err_text):
                safety_signal = (
                    f"the Codex model '{model_used}' is not available on this plan/account: "
                    f"{err_text.strip()[:200]}. Switch with pair_update(name, model='sol'|'terra'|'luna')."
                )
                safety_kind = "model_unavailable"
            elif _RATE_LIMIT_RE.search(err_text) or (not terminal_message and rl.get("rate_limit_reached_type")):
                safety_signal = f"usage limit reached on the ChatGPT plan: {err_text.strip()[:200]}"
                safety_kind = "usage_limit"
            else:
                safety_signal = f"turn failed: {err_text.strip()[:300] or (stderr or '').strip()[-300:]}"
                safety_kind = "error"
        elif rl.get("rate_limit_reached_type"):
            safety_signal = f"the plan's rate limit is reached ({rl.get('rate_limit_reached_type')})"
            safety_kind = "usage_limit"

        if not reply and safety_signal:
            reply = f"(no reply — {safety_signal})"

        return SendResult(
            name=spec.name,
            response=reply,
            session_id=spec.session_id,
            model_used=model_used,
            cost_usd=None,
            duration_ms=dur_ms,
            permission_denials=denials,
            context=ContextStatus(tokens_used=used, tokens_max=window, percent=pct, warning=warning),
            cache_read_tokens=int(usage.get("cached_input_tokens") or 0),
            log_path=scope.get("log_path"),
            log_line_start=scope.get("start_line"),
            log_line_end=scope.get("end_line"),
            safety_signal=safety_signal,
            safety_kind=safety_kind,
            backend="codex",
            plan_usage=plan_usage_text(rl),
            guardian_notes=guardian,
            notes=notes,
        )
