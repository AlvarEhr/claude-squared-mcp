"""Async task store for pair_send_async / pair_poll.

Tasks are persisted to ~/.claude/pairs/async/<task_id>.json so polls survive process restart.
The send itself runs in a daemon thread.

In v0.7.1 sync ``pair_send`` is implemented as ``start_task`` + ``wait_for_task`` so
the same machinery powers both sync and async; if the sync wait times out, the agent
gets back a running task_id to poll/wait on (graceful degradation, no lost work).

v0.7.5 adds an atexit handler that walks the async dir on MCP shutdown and marks any
remaining ``status="running"`` tasks as ``failed`` so they don't orphan forever when
Claude Desktop hot-restarts the MCP server (which happens on its own RPC-timeout
watchdog and on .mcpb reinstall).
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from claude_squared.models import AsyncTaskState, SendResult
from claude_squared.registry import async_dir


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check using stdlib only.

    Returns True if the PID currently maps to a live process on this machine.
    False if the process is gone OR if we can't tell (treat unknown as dead
    so the startup-sweep prefers cleaning over leaving orphans — false-negative
    is recoverable, false-positive leaves a stale running task forever).

    PID reuse caveat: PIDs are reused by the OS over time, so a long-dead
    task whose original owner crashed and whose PID has since been assigned
    to an unrelated process will look "alive" here. Real-world risk for this
    MCP is essentially zero (task durations are minutes-to-hours, not days)
    but worth knowing exists.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    # POSIX: os.kill with signal 0 raises ProcessLookupError if dead,
    # PermissionError if alive but inaccessible (treat as alive),
    # OSError on other failures (treat as alive for safety).
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


# Marker prefix on the error of a task that was finalized NOT because the work
# errored, but because its owning MCP server process died mid-turn (killed or
# restarted — e.g. by the host's MCP watchdog during a long/heavy turn). The
# status is still "failed" (it's a terminal non-success from the state-machine's
# view), but this prefix lets pair_poll / wait.py render it honestly as a
# supervision event rather than a work error. We control both ends of this
# string, so prefix-matching it is reliable (not fragile text-scraping).
ORPHAN_ERROR_PREFIX = "ORPHANED: "

_ORPHAN_MESSAGE = (
    ORPHAN_ERROR_PREFIX
    + "the owning MCP server process died mid-turn (killed or restarted — e.g. by "
    "the host's MCP watchdog during a long/heavy turn). This is a SUPERVISION event, "
    "NOT a work error: the pair's claude subprocess runs in its own process group and "
    "often runs to completion (committing files, writing docs) even after the server "
    "is gone — but the turn's final report text was not captured. To recover: inspect "
    "pair_transcript and your git/file state to see what actually landed, then pair_send "
    "to resume (the session JSONL persisted, so the pair continues from where it left off)."
)


def _task_path(task_id: str) -> Path:
    return async_dir() / f"{task_id}.json"


def _save(state: AsyncTaskState) -> None:
    p = _task_path(state.task_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    tmp.replace(p)


def load_task(task_id: str) -> AsyncTaskState | None:
    p = _task_path(task_id)
    if not p.exists():
        return None
    return AsyncTaskState.model_validate_json(p.read_text(encoding="utf-8"))


def reap_orphan(task_id: str) -> AsyncTaskState | None:
    """Finalize a task whose owning MCP server process has died, the moment we
    observe it — instead of waiting for the next server's startup sweep.

    Returns the (possibly-updated) state, or None if the task is unknown.

    Only flips a task whose ``status == "running"`` AND whose ``owner_pid`` is a
    real PID that is no longer alive — i.e. a genuine orphan. A task owned by a
    still-live server (this one or another coexisting MCP process) is left
    untouched, so this is safe to call from any observation path (pair_poll). A
    legacy task with no owner_pid is left for the startup sweep (we can't safely
    distinguish "old orphan" from "just-created" without the PID at runtime).

    Atomic write + event fire so an in-process ``wait_for_task`` wakes immediately.
    Idempotent across processes: concurrent reaps write identical content.
    """
    state = load_task(task_id)
    if state is None:
        return None
    if state.status != "running":
        return state
    pid = state.owner_pid
    if pid is None or pid <= 0 or _is_pid_alive(int(pid)):
        return state  # no PID to judge, or owner still alive → not an orphan
    # Owner is confirmed dead → finalize as an orphan.
    state.status = "failed"
    state.error = _ORPHAN_MESSAGE
    state.finished_at = datetime.utcnow()
    _save(state)
    with _task_events_lock:
        ev = _task_events.get(task_id)
    if ev is not None:
        ev.set()
    return state


# In-memory task-completion events for fast in-process wakeup. These are an
# optimization layer on top of the on-disk state file; cross-process waiters
# (and post-restart waiters) fall back to polling load_task() — see __main__.wait.
_task_events: dict[str, threading.Event] = {}
_task_events_lock = threading.Lock()

# In-memory set of task IDs that have been deliberately stopped via pair_stop.
# The worker thread checks this on exception to distinguish "stopped" from
# "failed" in the AsyncTaskState. In-process only (a separate MCP subprocess
# can't tell another process to flip its in-memory flag — and that's fine,
# since pair_stop only operates on the pair's local runtime).
_stopped_task_ids: set[str] = set()
_stopped_task_ids_lock = threading.Lock()


def mark_task_stopped(task_id: str) -> None:
    with _stopped_task_ids_lock:
        _stopped_task_ids.add(task_id)


def _was_stopped(task_id: str) -> bool:
    with _stopped_task_ids_lock:
        return task_id in _stopped_task_ids


def _clear_stopped(task_id: str) -> None:
    with _stopped_task_ids_lock:
        _stopped_task_ids.discard(task_id)


def list_running_task_ids_for_pair(pair_name: str) -> list[str]:
    """Find all tasks for a given pair that are currently in the on-disk state
    file with status='running'. Used by pair_stop to know which task IDs to
    mark for the worker thread."""
    out: list[str] = []
    d = async_dir()
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("pair_name") == pair_name and data.get("status") == "running":
                out.append(data["task_id"])
        except Exception:
            continue
    return out


def _get_or_create_event(task_id: str) -> threading.Event:
    with _task_events_lock:
        ev = _task_events.get(task_id)
        if ev is None:
            ev = threading.Event()
            _task_events[task_id] = ev
        return ev


def _drop_event(task_id: str) -> None:
    """Free the in-memory event after the task has finished and been observed."""
    with _task_events_lock:
        _task_events.pop(task_id, None)


def start_task(pair_name: str, message: str, runner: Callable[[], SendResult]) -> AsyncTaskState:
    """Spawn a daemon thread that runs `runner()` and writes the result.

    Sets an in-memory threading.Event on completion so ``wait_for_task`` can wake
    up immediately within the same process. Cross-process waiters fall back to
    polling ``load_task``.
    """
    task_id = str(uuid.uuid4())
    state = AsyncTaskState(
        task_id=task_id,
        pair_name=pair_name,
        message=message,
        status="running",
        started_at=datetime.utcnow(),
        owner_pid=os.getpid(),
    )
    _save(state)
    event = _get_or_create_event(task_id)

    def _go() -> None:
        try:
            result = runner()
            # If the task was stopped just before the result came back (the
            # interrupt acknowledged via error_during_execution result event),
            # report "stopped" rather than "done."
            if _was_stopped(task_id):
                state.status = "stopped"
                state.error = "stopped by pair_stop"
            else:
                state.status = "done"
                state.result = result
        except Exception as e:
            # Distinguish deliberate stop (interrupt-induced CLIError, or
            # tree-kill while the worker was waiting) from a real failure.
            if _was_stopped(task_id):
                state.status = "stopped"
                state.error = "stopped by pair_stop"
            else:
                state.status = "failed"
                state.error = f"{type(e).__name__}: {e}"
        finally:
            state.finished_at = datetime.utcnow()
            _save(state)
            event.set()
            _clear_stopped(task_id)

    threading.Thread(target=_go, daemon=True).start()
    return state


def wait_for_task(task_id: str, timeout_s: float) -> AsyncTaskState | None:
    """Block up to ``timeout_s`` for the task to finish (status done|failed).

    Returns the final ``AsyncTaskState`` if it finished in time, or the current
    state if still running. Returns None only if the task ID is unknown.

    Uses an in-process Event for immediate wakeup; falls back to a single
    state-file load on Event timeout. Cross-process waiters should call
    ``load_task`` directly in a poll loop or use ``python -m claude_squared wait``.
    """
    event = _get_or_create_event(task_id)
    fired = event.wait(timeout_s)
    state = load_task(task_id)
    if state is None:
        return None
    if state.status in ("done", "failed"):
        # Free the event now that the task is observed terminal — don't leak
        # entries indefinitely. Subsequent waiters that arrive AFTER completion
        # will find load_task already returns the terminal state synchronously.
        _drop_event(task_id)
    return state


def find_task_by_prefix(prefix: str) -> list[str]:
    """Resolve a task_id prefix to all matching task IDs on disk.

    Used by ``pair_poll`` to accept the 8-char prefix shown in ``pair_status``
    output (where full UUIDs are truncated for readability). Returns a list:
        []     — no match (prefix doesn't exist)
        [id]   — unique match (caller can use it directly)
        [a,b]  — ambiguous (caller surfaces all candidates so the agent can pick)
    """
    if not prefix:
        return []
    d = async_dir()
    if not d.exists():
        return []
    out: list[str] = []
    for p in d.glob(f"{prefix}*.json"):
        tid = p.stem  # filename without .json
        if tid.startswith(prefix):
            out.append(tid)
    return out


def latest_task_id_for_pair(pair_name: str) -> str | None:
    """Return the most-recently-STARTED task id for a pair, or None if it has none.

    Lets ``pair_poll`` accept a pair NAME instead of a UUID — agents fumble the
    long task id, but they always know the pair's name. "Most recent" = max
    ``started_at`` (the task you most likely just kicked off). Malformed/unparseable
    state files are skipped. This does NOT change the canonical id (a pair has
    many tasks over its life); it's purely an ergonomic resolver.
    """
    d = async_dir()
    if not d.exists():
        return None
    best_id: str | None = None
    best_started = ""
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("pair_name") != pair_name:
            continue
        started = data.get("started_at") or ""
        # ISO-8601 strings sort lexicographically in chronological order, so a
        # plain string max gives the latest without datetime parsing.
        if best_id is None or started > best_started:
            best_id, best_started = data.get("task_id"), started
    return best_id


def _cleanup_orphaned_running_tasks() -> None:
    """Walk the async dir and mark any ``status="running"`` task **owned by this
    process** as failed.

    Called via ``atexit`` so the MCP server's graceful shutdown (including
    the case where Claude Desktop hot-restarts us due to its RPC watchdog)
    doesn't leave tasks in "running" forever. Without this handler the
    agent's next ``pair_poll`` after a restart sees the task as still running
    indefinitely, blocking recovery and producing misleading ``pair_status``
    in-flight task lists.

    **PID-scoped**: only sweeps tasks where ``owner_pid == os.getpid()``.
    Without this scope, a Desktop-install MCP server shutdown would also
    trash tasks that a coexisting CLI-install MCP server is actively
    working on (both share the same ``~/.claude/pairs/async/`` directory
    cross-process — that's by v0.7 design). The startup-sweep function
    ``_sweep_dead_predecessor_orphans`` handles tasks from a previous
    process whose owner is no longer alive.

    We mark "failed" rather than "stopped" because the worker thread didn't
    receive an explicit interrupt — the MCP process just died, possibly
    mid-flight. From the agent's perspective: the underlying claude.exe
    subprocess may have orphaned but completed its work (visible in the
    session JSONL); the next ``pair_send`` will ``--resume`` from there.
    """
    _sweep_running_tasks(
        predicate=lambda data: data.get("owner_pid") == os.getpid(),
        message=(
            "MCP server shutdown while task was in-flight — work may have "
            "completed in the session JSONL; check pair_transcript or send "
            "a follow-up pair_send to resume."
        ),
    )


def _sweep_dead_predecessor_orphans() -> None:
    """At MCP server startup, sweep ``status="running"`` tasks whose owner_pid
    is no longer alive (or missing — legacy tasks from before owner_pid was
    added). Recovers the v0.7.5 "first-install orphan cleanup" property
    without trampling tasks being actively worked on by other live MCP
    processes.

    A task with no ``owner_pid`` field came from a pre-v0.7.9 task state
    file; safe to assume its owner is long-gone (the v0.7.9 deploy implies
    the previous server is no longer the same instance).
    """
    def _predicate(data: dict) -> bool:
        pid = data.get("owner_pid")
        if pid is None:
            return True  # legacy task; sweep it
        try:
            return not _is_pid_alive(int(pid))
        except Exception:
            return True  # malformed PID; treat as orphan

    _sweep_running_tasks(predicate=_predicate, message=_ORPHAN_MESSAGE)


def _sweep_running_tasks(predicate: Callable[[dict], bool], message: str) -> None:
    """Shared sweep impl used by both atexit and startup paths.

    ``predicate(data)`` decides whether to mark a given running task as failed.
    Best-effort: any exception per-task is logged-and-skipped so a single bad
    file can't prevent the sweep (or, for atexit, can't prevent shutdown).
    """
    try:
        d = async_dir()
        if not d.exists():
            return
        now_iso = datetime.utcnow().isoformat()
        for p in d.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") != "running":
                    continue
                if not predicate(data):
                    continue
                data["status"] = "failed"
                data["error"] = message
                data["finished_at"] = now_iso
                tmp = p.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                tmp.replace(p)
            except Exception:
                continue
    except Exception:
        pass


# Register the atexit (own-PID) sweep at module import time.
atexit.register(_cleanup_orphaned_running_tasks)

# Run the dead-predecessor sweep ONCE at module import (i.e. MCP server startup).
# Imports are idempotent so this fires per fresh interpreter; in the same
# process the function is harmless to re-run (the predicate returns False for
# this PID's own running tasks since we're alive).
#
# PID-reuse caveat (also noted in _is_pid_alive): a long-lived "running" task
# whose owner_pid was later recycled by an unrelated process will SURVIVE this
# liveness check and stay marked as running forever. Real-world risk is near
# zero for this MCP's task durations (minutes-to-hours, not days), but if a
# future agent ever sees an "impossibly stuck" task whose owner_pid is "alive"
# according to the OS but no MCP server is actually working on it, this is the
# path to suspect. Recovery: edit the state file manually or call pair_stop
# with drain_queue=True.
_sweep_dead_predecessor_orphans()
