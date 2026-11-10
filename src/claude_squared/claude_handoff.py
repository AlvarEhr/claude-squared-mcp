"""Claude turn survival across an owner exit. Receipt precedes stdin delivery.

The shutdown path never needs a registry/control lock or a transcript scan.
Recovery runs under the pair lock, after the recorded process has exited.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from filelock import FileLock, Timeout

from claude_squared import activity, registry
from claude_squared.cli_paths import encode_cwd_for_project
from claude_squared.errors import PairError, TaskStopped

FINISHING = "previous runtime still finishing its turn after a host restart"


def path_for(name: str) -> Path:
    if not name or name in (".", "..") or any(c in name for c in "/\\:\0"):
        raise PairError("Invalid pair name for a Claude handoff receipt")
    return registry.pairs_dir() / "handoff" / (name + ".json")


def view(name: str) -> dict | None:
    return activity.claude_handoff_view(registry.pairs_dir(), name)


def publish(record: dict) -> Path:
    path = path_for(record["pair"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        # A self-woken turn can publish while the previous task finalizes.
        # Share this short lock with discard's compare-and-delete; never take
        # a pair/control lock while holding it. Shutdown never takes this lock.
        with FileLock(str(path.with_suffix(".lock")), timeout=5):
            tmp.write_text(json.dumps(record), encoding="utf-8")
            for attempt in range(4):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)
    return path


def discard(name: str, task_id: str | None) -> None:
    path = path_for(name)
    if not path.parent.exists():
        return
    with FileLock(str(path.with_suffix(".lock")), timeout=5):
        record = view(name)
        if record and not record.get("invalid") and record.get("task_id") == task_id:
            for attempt in range(4):
                try:
                    path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(.02 * (attempt + 1))


def final_reply(record: dict):
    """Only a top-level final assistant message after THIS turn's boundary.
    A tool-use/thinking/interim text block is not evidence of a completed turn.
    """
    path = (registry.claude_home() / "projects" / encode_cwd_for_project(record["cwd"])
            / (record["session_id"] + ".jsonl"))
    started = activity._av_parse(record["turn_started_at"])
    candidate = None
    with path.open(encoding="utf-8") as stream:
        for raw in stream:
            try:
                event = json.loads(raw)
            except ValueError:
                if raw.strip():
                    candidate = None  # a torn later event cannot prove completion
                continue
            if not isinstance(event, dict) or event.get("parent_tool_use_id") or event.get("isSidechain"):
                continue
            at = activity._av_parse(event.get("timestamp"))
            if at is None or started is None or at < started:
                continue
            if event.get("type") == "user":
                # Tool results belong to the same assistant turn. A later real
                # user message invalidates any earlier final candidate.
                content = (event.get("message") or {}).get("content")
                if isinstance(content, str) or (isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text" for b in content)):
                    candidate = None
            if event.get("type") != "assistant":
                continue
            msg = event.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            text = "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str))
            # Some JSONL writers omit stop_reason; the final text-only assistant
            # block is then the best available completion evidence. A later
            # tool-use assistant invalidates it again.
            final = msg.get("stop_reason") in (None, "end_turn", "stop_sequence")
            final = final and not any(b.get("type") == "tool_use" for b in content if isinstance(b, dict))
            candidate = (text, msg.get("model")) if final and text.strip() else None
    return candidate


def reconcile(name: str, *, pair_locked: bool = False):
    """Settle exactly once; publication must succeed before deleting the receipt."""
    from claude_squared import async_tasks
    from claude_squared.models import SendResult
    from datetime import datetime

    if not pair_locked:
        lock = registry.pairs_dir() / "locks" / (name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(lock), timeout=0):
                return reconcile(name, pair_locked=True)
        except Timeout:
            return None
    record = view(name)
    if not record or record.get("invalid") or record.get("alive") or not record.get("detached"):
        return None
    tid = record.get("task_id")
    receipt_task_id = tid
    if tid is None:
        candidates = [async_tasks.load_task(t) for t in async_tasks.list_running_task_ids_for_pair(name)]
        matches = [s for s in candidates if s is not None and activity.claude_handoff_matches(
            record, s.model_dump(mode="json"))]
        if matches:
            tid = max(matches, key=lambda s: s.started_at).task_id
    state = async_tasks.load_task(tid) if tid else None
    if state is not None and state.status == "running" and state.owner_pid == record.get("server_pid"):
        try:
            reply = final_reply(record)
        except (FileNotFoundError, UnicodeError):
            reply = None
        if reply:
            text, model = reply
            state.result = SendResult(name=name, session_id=record["session_id"], response=text,
                                      model_used=model or record.get("model") or "unknown", duration_ms=0,
                                      cost_usd=None, notes=["Recovered from session JSONL after a host restart; "
                                                            "live log and cost accounting may be incomplete."])
            state.status, state.error = "done", None
        else:
            state.status = "failed"
            state.error = async_tasks._orphan_message(state) + " No final assistant text was found for the handed-off turn."
        if async_tasks._was_stopped(tid):
            state.status, state.error = "stopped", "stopped by pair_stop"
        state.recovered_at = datetime.utcnow()
        state.recovered_by_pid = os.getpid()
        state.owner_note = "Recovered after a host restart from " + record["path"]
        async_tasks._finish_task(state, async_tasks._get_or_create_event(tid))
        # _finish_task retains an in-memory fallback on a failed save. Another
        # process cannot see that fallback, so retain the receipt until durable.
        if tid in async_tasks._unsaved_results:
            return state
    if state is not None:
        owner_path = registry.pairs_dir() / "activity" / name / (str(record["server_pid"]) + ".json")
        try:
            data = json.loads(owner_path.read_text(encoding="utf-8"))
            data = activity._av_dead_owner_state(data, record["server_pid"])
            for turn in data.get("window_turns") or []:
                if turn.get("task_id") == tid:
                    turn.update(status=state.status, ended_at=state.finished_at.isoformat(),
                                preview=(state.result.response[:240] if state.result else ""),
                                note="Recovered after a host restart")
            data["state"] = "exited"
            activity._atomic_write(owner_path, json.dumps(data))
        except (OSError, ValueError, AttributeError):
            pass
    discard(name, receipt_task_id)
    return state


def guard(name: str, *, pair_locked: bool = False, wait: bool = False, should_stop=None) -> None:
    while True:
        record = view(name)
        if not record:
            return
        if record.get("invalid"):
            raise PairError(f"Unreadable Claude handoff receipt: {record['path']}; inspect it before respawning '{name}'.")
        if not record.get("detached"):
            return  # ordinary owner: existing pair/background locks govern it
        if not record.get("alive"):
            reconcile(name, pair_locked=pair_locked)
            if view(name):
                raise PairError(f"Claude handoff recovery pending for '{name}'; retry pair_poll before respawning.")
            return
        if not wait:
            raise PairError(f"Pair '{name}': {FINISHING}; receipt {record['path']}")
        if should_stop and should_stop():
            raise TaskStopped("stopped while waiting for the previous Claude runtime")
        time.sleep(.1)
