"""Optional Codex manager completion notices; no effects when disabled.

The dispatcher belongs to the MCP process, not a terminal/model turn. Results
remain in the existing task store. This module never starts or kills processes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import threading
import time
import uuid

from filelock import FileLock, Timeout
from datetime import timezone

from claude_squared import desktop_bridge as bridge
from claude_squared.registry import claude_home

logger = logging.getLogger(__name__)
# Fence older dispatchers whose path/build preflight could reject a valid
# manager or impose shared retry_at backoff. Existing work remains readable.
VERSION = 6
MAX_WAIT = 6 * 3600
MAX_CONTROL_RECORD = 8 * 1024 * 1024
# Codex's canonical JSONL envelope lets us skip huge messages/tool outputs
# without materializing them. Small noncanonical records still parse normally.
_ENVELOPE = re.compile(rb'^\s*\{(?:\s*"(?:timestamp|ordinal)"\s*:\s*(?:"[^"\\]*"|[0-9]+)\s*,)*\s*"type"\s*:\s*"([^"]+)"\s*,\s*"payload"\s*:\s*\{')
_PAYLOAD_TYPE = re.compile(rb'\s*"type"\s*:\s*"([^"]+)"')
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TERMINAL = {"delivered", "canceled", "blocked"}
_stop = threading.Event()
_wake = threading.Event()
_thread = None
_thread_guard = threading.Lock()
_terminal_seen = {}


class Refused(ValueError):
    pass


class Deferred(Refused):
    """Current manager context has not reached the rollout yet."""


def enabled():
    return os.environ.get("CLAUDE_PAIR_CODEX_COMPLETION", "").lower() == "desktop"


def directory():
    # Do not call pairs_dir(): inspection/disabled mode must not create paths.
    return claude_home() / "pairs" / "completion-v1"


def _read(path):
    try:
        if path.stat().st_size > 128 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") not in (1, 2, 3, 4, 5, VERSION)
                or not UUID.fullmatch(str(data.get("id", "")))
                or path.stem != data.get("id")
                or not UUID.fullmatch(str(data.get("thread", "")))
                or not UUID.fullmatch(str(data.get("turn", "")))
                or not isinstance(data.get("tasks"), list) or not data["tasks"]
                or len(data["tasks"]) > 64
                or any(not isinstance(t, str) or not UUID.fullmatch(t) for t in data["tasks"])
                or not isinstance(data.get("pair"), str)
                or not isinstance(data.get("session"), str)
                or not isinstance(data.get("cwd"), str) or not os.path.isabs(data["cwd"])
                or not isinstance(data.get("attempts"), int) or data["attempts"] < 0
                or not isinstance(data.get("created"), (int, float))
                or data.get("status") not in TERMINAL | {"watching", "sending", "uncertain"}):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _save(path, data):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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


def caller_ids(ctx):
    """Mirror installed app-tools metadata shapes, rejecting conflicting IDs."""
    try:
        meta = ctx.request_context.meta
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump()
    except (AttributeError, RuntimeError):
        meta = None
    if not isinstance(meta, dict):
        raise Refused("caller metadata is missing")
    if any(meta.get(k) not in (None, "local") for k in ("hostId", "host_id")):
        raise Refused("only local Desktop callers are supported")
    nested = meta.get("x-codex-turn-metadata", {})
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError as exc:
            raise Refused("invalid Codex turn metadata") from exc
    if not isinstance(nested, dict):
        raise Refused("invalid Codex turn metadata")
    if nested.get("sandbox_mode") not in (None, "danger-full-access"):
        raise Refused("caller request reports a restricted sandbox")
    if nested.get("auto_review_enabled") not in (None, False):
        raise Refused("caller request requires automatic approval review")
    result = []
    for kind in ("thread", "turn"):
        names = ("openai/" + kind + "Id", "openai/" + kind + "_id",
                 "codex" + kind.title() + "Id", "codex_" + kind + "_id",
                 kind + "Id", kind + "_id")
        values = [meta[k] for k in names if k in meta]
        if kind + "_id" in nested:
            values.append(nested[kind + "_id"])
        if kind in meta:
            obj = meta[kind]
            if not isinstance(obj, dict) or "id" not in obj:
                raise Refused("invalid nested caller identity")
            values.append(obj["id"])
        if (not values or any(not isinstance(v, str) or not UUID.fullmatch(v) for v in values)
                or len(set(values)) != 1):
            raise Refused("missing, invalid, or conflicting caller %s ID" % kind)
        result.append(values[0])
    return tuple(result)


def comparison_path(path):
    """Compare resolved Windows paths equally with/without the extended prefix.

    Keep the original path for I/O, including paths longer than MAX_PATH.
    Callers resolve paths first so junctions cannot hide an outside location.
    """
    if isinstance(path, PureWindowsPath):
        text = str(path)
        if text[:8].lower() == "\\\\?\\unc\\":
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        return PureWindowsPath(text)
    return path


def manager_records(stream, marker=None):
    """Bound memory per control record, not by the manager's lifetime log size.

    Read only the EOF snapshot taken at entry. Never parse an incomplete final
    record or chase concurrent appends. Irrelevant canonical rows are drained
    in chunks; they can be arbitrarily large without allocating a whole line.
    """
    stream.seek(0, os.SEEK_END)
    remaining = stream.tell()
    stream.seek(0)
    while remaining:
        head = stream.readline(min(4096, remaining))
        if not head:
            break
        remaining -= len(head)
        envelope = _ENVELOPE.match(head)
        relevant, receipt_row = True, False
        if envelope:
            kind = envelope[1]
            subtype = _PAYLOAD_TYPE.match(head, envelope.end())
            if kind == b"event_msg":
                relevant = subtype is None or subtype[1] in (b"task_started", b"thread_rolled_back")
            elif kind == b"response_item":
                relevant = bool(marker) and (subtype is None or subtype[1] == b"function_call_output")
                receipt_row = relevant
            else:
                relevant = kind in (b"session_meta", b"turn_context")
        needle = marker.encode("utf-8") if receipt_row else b""
        found_marker, overlap = bool(needle and needle in head), head[-len(needle):] if needle else b""
        chunks, size, complete = ([head] if relevant else []), len(head), head.endswith(b"\n")
        while not complete and remaining:
            chunk = stream.readline(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            size += len(chunk)
            complete = chunk.endswith(b"\n")
            if needle:
                found_marker = found_marker or needle in overlap + chunk
                overlap = chunk[-len(needle):]
            if relevant:
                if size > MAX_CONTROL_RECORD:
                    if not receipt_row:
                        raise Refused("manager control record exceeds the bounded inspection limit")
                    chunks.clear()  # a generic giant tool output is not a receipt
                elif chunks:
                    chunks.append(chunk)
        if not relevant or not complete:
            continue
        if receipt_row:
            if not found_marker:
                continue
            if size > MAX_CONTROL_RECORD:
                raise Refused("manager callback receipt exceeds the bounded inspection limit")
        try:
            row = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("payload"), dict):
            yield row


def caller_snapshot(thread, turn, marker=None):
    from claude_squared.adapters.codex import codex_home, rollout_path_for, is_compressed_rollout

    path = rollout_path_for(thread)
    if path is None:
        raise Refused("manager rollout is unavailable or archived")
    if is_compressed_rollout(path):
        raise Deferred("manager rollout is compressed; resume the manager chat to restore it")
    path = path.resolve()
    if not comparison_path(path).is_relative_to(comparison_path((codex_home() / "sessions").resolve())):
        raise Refused("manager is not an active local session")
    meta, context, receipt = None, None, False
    latest_turn, source_present = None, False
    with path.open("rb") as f:
        for row in manager_records(f, marker):
            payload = row["payload"]
            if row.get("type") == "session_meta":
                meta = payload
            elif row.get("type") == "turn_context":
                if payload.get("turn_id") == latest_turn:
                    context = payload
            elif row.get("type") == "event_msg" and payload.get("type") == "task_started":
                tid = payload.get("turn_id")
                if isinstance(tid, str) and tid != latest_turn:
                    latest_turn, context = tid, None
                if tid == turn:
                    source_present = True
            elif row.get("type") == "event_msg" and payload.get("type") == "thread_rolled_back":
                # Codex rolls back USER boundaries, not task_started counts.
                # Callback-only turns/steering make that distinction material.
                # First release refuses any registration spanning a rollback.
                if source_present:
                    raise Refused("manager rolled back after this send; notification is stale")
                latest_turn, context = None, None
            elif (marker and row.get("type") == "response_item"
                  and payload.get("type") == "function_call_output"
                  and payload.get("namespace") == "codex_app"
                  and payload.get("name") == "send_message_to_thread"
                  and marker in str(payload.get("output", ""))):
                receipt = True
    if (not meta or meta.get("id", meta.get("session_id")) != thread
            or meta.get("originator") not in ("Codex Desktop", "ChatGPT Desktop")
            or meta.get("source") != "vscode"
            or meta.get("thread_source") not in ("user", "agent_created_thread")):
        raise Refused("caller is not a user-owned Desktop manager (CLI/pair callers are excluded)")
    if not source_present:
        raise Refused("source turn is missing or was rewound")
    if context is None:
        raise Deferred("manager's latest turn context has not been published")
    return {"context": context, "receipt": receipt, "path": str(path)}


def full_access(snapshot):
    c = snapshot["context"]
    if (c.get("sandbox_policy") != {"type": "danger-full-access"}
            or c.get("approval_policy") != "never"
            or (c.get("collaboration_mode") is not None
                and (not isinstance(c["collaboration_mode"], dict)
                     or c["collaboration_mode"].get("mode") != "default"))
            or c.get("permissions") is not None or c.get("permission_profile_id") is not None
            or c.get("permission_profile") not in (None, {"type": "disabled"})
            or c.get("active_permission_profile") not in (None, {"id": ":danger-full-access"})
            or not isinstance(c.get("cwd"), str) or not os.path.isabs(c["cwd"])):
        raise Refused("completion wake-up requires a manager already using full access / never approval")


def _loss_key(loss):
    return hashlib.sha256(json.dumps(loss, sort_keys=True).encode()).hexdigest()


def background_followup(task):
    """Does the inline reply still need a background completion notice?

    Launch history alone is insufficient. A fast implicit final turn may have
    already finished, while the caller only receives the earlier interim reply.
    """
    from claude_squared import activity
    view = activity.view(task.pair_name)
    started = task.started_at
    since = started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started
    entries = [(e, "started_at") for e in view.get("background", [])]
    entries += [(e, "since") for e in view.get("wake_pending", [])]
    entries += [(e, "started_at") for e in view.get("turns", []) if e.get("kind") == "self_woken"]
    entries += [(e, "started_at") for e in view.get("window_turns", [])
                if e.get("kind") == "self_woken" and e.get("task_id") != task.task_id]
    for entry, field in entries:
        at = activity._av_parse(entry.get(field))
        if at is not None and at >= since:
            return True
    return False


def prepare(ctx, spec):
    """Capture caller, pair generation and losses BEFORE launching the worker."""
    if not enabled():
        try:
            caller_ids(ctx)
        except Refused:
            return {"hint": None}  # other harnesses retain their background-Bash hint
        return {"hint": "Codex completion wake-up is disabled. Inspect this task with pair_poll; a terminal watcher does not wake an idle Codex chat."}
    try:
        thread, turn = caller_ids(ctx)
        bridge.desktop_ancestor()  # no pipe scan in the RPC path
        snapshot = caller_snapshot(thread, turn)
        full_access(snapshot)
        from claude_squared import activity
        if spec.session_id == thread:
            raise Refused("a pair cannot register a callback to its own thread")
        view = activity.view(spec.name)
        return {"thread": thread, "turn": turn, "session": spec.session_id,
                "cwd": snapshot["context"]["cwd"],
                "loss_baseline": [_loss_key(x) for x in view.get("losses", [])]}
    except Exception as exc:
        return {"hint": "Codex completion notification not armed: %s. Read this task with pair_poll; work continues." % exc}


def arm(task, ctx, prepared=None):
    """Persist the captured registration after task creation, including fast completions."""
    try:
        if prepared is None:
            from claude_squared import registry
            prepared = prepare(ctx, registry.get_pair(task.pair_name))
        if "hint" in prepared:
            return prepared["hint"]
        thread, turn, session = prepared["thread"], prepared["turn"], prepared["session"]
        root = directory()
        root.mkdir(parents=True, exist_ok=True)
        group = hashlib.sha256((thread + "\0" + task.pair_name + "\0" + session).encode()).hexdigest()
        with FileLock(str(root / (group + ".group.lock")), timeout=1):
            existing = [p for p in root.glob("*.json") if (r := _read(p))
                        and r["thread"] == thread and r["pair"] == task.pair_name
                        and r["session"] == session and r["status"] == "watching"
                        and r["cwd"] == prepared["cwd"]]
            valid = []
            for candidate in existing:
                old = _read(candidate)
                try:
                    caller_snapshot(old["thread"], old["turn"])
                except (Refused, OSError):
                    continue
                valid.append(candidate)
            path = valid[0] if valid else root / (str(uuid.uuid4()) + ".json")
            with FileLock(str(path) + ".lock", timeout=1):
                r = _read(path)
                if r and r["status"] != "watching":
                    path = root / (str(uuid.uuid4()) + ".json")
                    r = None  # old dispatcher completed while we waited
                if r is None:
                    r = {"version": VERSION, "id": path.stem, "thread": thread, "turn": turn,
                         "pair": task.pair_name, "session": session, "tasks": [], "cwd": prepared["cwd"],
                         "created": time.time(), "status": "watching", "attempts": 0,
                         "loss_baseline": prepared["loss_baseline"]}
                if r["version"] != VERSION:
                    r["retry_at"] = 0  # discard an old bridge's build-refusal backoff
                r["version"] = VERSION
                if task.task_id not in r["tasks"]:
                    if len(r["tasks"]) >= 64:
                        raise Refused("notification window already has 64 tasks")
                    r["tasks"].append(task.task_id)
                _save(path, r)
        start()
        _wake.set()
        return "Codex completion notification armed (" + r["id"][:8] + "); no terminal watcher needed."
    except Exception as exc:
        # Notification failure must NEVER cancel or hide pair work.
        logger.warning("Completion notification not armed for %s: %s", task.task_id, exc)
        return "Codex completion notification not armed: %s. Read this task with pair_poll; work continues." % exc


def records():
    return [r for p in directory().glob("*.json") if (r := _read(p))]


def status(task_id):
    matches = [r for r in records() if task_id in r["tasks"]]
    if not matches:
        return ""
    r = max(matches, key=lambda x: x["created"])
    return "\n[Codex notification %s: %s%s]" % (r["id"][:8], r["status"],
                                                "; " + str(r["detail"]) if r.get("detail") else "")


def _finish(path, r, state, detail=""):
    r.update(status=state, detail=detail, updated=time.time())
    _save(path, r)


def _notice(r, states, view, attention):
    ids = list(r["tasks"])
    for t in view.get("window_turns", []):
        tid = t.get("task_id")
        if isinstance(tid, str) and UUID.fullmatch(tid) and tid not in ids:
            ids.append(tid)
    ids = ids[:64]
    return ("CLAUDE_PAIR_COMPLETION:" + r["id"] + "\n"
            + "Pair " + json.dumps(r["pair"]) + (" needs attention: " + attention if attention else " is idle.")
            + "\nInitiating task states: " + ", ".join(t.task_id + "=" + t.status for t in states)
            + "\nRead the existing replies with " + "; ".join("pair_poll(" + json.dumps(i) + ")" for i in ids)
            + ". Do not re-send the original work. This is a completion event, not a new user request."
            + " If this notification ID was already handled, do not handle it again.")


def process_one(path):
    """One bounded pass. Only the notification lock is held during transport."""
    if not enabled():
        return
    try:
        with FileLock(str(path) + ".lock", timeout=0):
            r = _read(path)
            if not r or r["status"] in TERMINAL:
                return
            if r["version"] != VERSION:
                # Claim legacy work only if THIS dispatcher can use the current
                # Desktop. Upgrade under the same kernel lock before any I/O;
                # old readers then skip it. Uncertain intent remains uncertain.
                try:
                    bridge.desktop_ancestor()
                except bridge.Unavailable:
                    return
                r["version"] = VERSION
                r["retry_at"] = 0
                _save(path, r)
            if r.get("retry_at", 0) > time.time():
                return
            marker = "CLAUDE_PAIR_COMPLETION:" + r["id"]
            if r["status"] in ("sending", "uncertain"):
                # A previous sender could have died after writing. Receipt checks
                # continue; absence is NOT evidence that re-sending is safe.
                try:
                    snapshot = caller_snapshot(r["thread"], r["turn"], marker)
                    if snapshot["receipt"]:
                        _finish(path, r, "delivered", "confirmed in manager tool output")
                        return
                except Deferred as exc:
                    r.update(detail=str(exc), retry_at=time.time() + 60)
                    _save(path, r)
                    return
                except (Refused, OSError) as exc:
                    _finish(path, r, "blocked", str(exc))
                    return
                r.update(status="uncertain", detail="no receipt yet; automatic resend suppressed",
                         retry_at=time.time() + 60)
                _save(path, r)
                return
            from claude_squared import activity, async_tasks, registry
            try:
                spec = registry.get_pair(r["pair"])
            except Exception as exc:
                _finish(path, r, "canceled", "pair unavailable: " + str(exc))
                return
            if spec.session_id != r["session"]:
                _finish(path, r, "canceled", "pair session changed")
                return
            states = [async_tasks.reap_orphan(t) for t in r["tasks"]]
            if any(t is None or t.pair_name != r["pair"] for t in states):
                _finish(path, r, "blocked", "initiating task is missing or belongs to another pair")
                return
            view = activity.view(r["pair"])
            losses = [x for x in view.get("losses", []) if _loss_key(x) not in r.get("loss_baseline", [])]
            expired = time.time() - r["created"] >= MAX_WAIT
            attention = ""
            if losses:
                attention = "background work was lost; check what landed"
            elif expired and (any(t.status == "running" for t in states) or view["verdict"] != "idle"):
                attention = "completion wait expired; work may still be running"
            elif any(t.status == "running" for t in states) or view["verdict"] in ("busy", "settling"):
                return
            elif view["verdict"] == "unknown":
                # Give the activity flusher a chance to publish its watermark.
                if "unknown_since" not in r:
                    r["unknown_since"] = time.time()
                    _save(path, r)
                    return
                if time.time() - r["unknown_since"] < 120:
                    return
                attention = "activity is unresolved: " + "; ".join(view.get("reasons", []))[:500]
            elif view["verdict"] != "idle":
                return
            try:
                snapshot = caller_snapshot(r["thread"], r["turn"], marker)
                if snapshot["receipt"]:
                    _finish(path, r, "delivered", "confirmed in manager tool output")
                    return
                full_access(snapshot)
                if snapshot["context"].get("cwd") != r.get("cwd"):
                    raise Refused("manager workspace changed since registration")
            except Deferred as exc:
                r.update(detail=str(exc), retry_at=time.time() + 1)
                _save(path, r)
                return
            except (Refused, OSError) as exc:
                _finish(path, r, "blocked", str(exc))
                return
            # Build/policy preflight and pipe discovery happen BEFORE marking a
            # send attempted. Outbox writes are durable before any callback bytes.
            try:
                with bridge.endpoint() as connection:
                    latest = caller_snapshot(r["thread"], r["turn"], marker)
                    if latest["receipt"]:
                        _finish(path, r, "delivered", "confirmed in manager tool output")
                        return
                    full_access(latest)
                    if latest["context"].get("cwd") != r.get("cwd"):
                        raise Refused("manager workspace changed during preflight")
                    # Discovery can take seconds. Re-check generation AND idle
                    # evidence after it, without taking a pair/runtime lock over I/O.
                    try:
                        current = registry.get_pair(r["pair"])
                    except Exception as exc:
                        _finish(path, r, "canceled", "pair unavailable during preflight: " + str(exc))
                        return
                    if current.session_id != r["session"]:
                        _finish(path, r, "canceled", "pair session changed during preflight")
                        return
                    refreshed = activity.view(r["pair"])
                    if any(_loss_key(x) not in r.get("loss_baseline", []) for x in refreshed.get("losses", [])):
                        attention = "background work was lost; check what landed"
                    if not attention and refreshed["verdict"] != "idle":
                        return
                    view = refreshed
                    if not attention and any(t.status != "done" for t in states):
                        attention = "one or more initiating tasks failed or were stopped; inspect their results"
                    payload = _notice(r, states, view, attention)
                    r.update(status="sending", detail="delivery intent persisted", updated=time.time())
                    _save(path, r)
                    result = connection.request("tools/call", {
                        "callerSource": "codex", "hostId": "local", "namespace": "codex_app",
                        "tool": "send_message_to_thread", "threadId": r["thread"], "turnId": r["turn"],
                        "callId": marker, "arguments": {"threadId": r["thread"], "prompt": payload},
                    }, delivery=True)
                    bridge.validate_delivery(result, r["thread"])
                    _finish(path, r, "delivered", "Desktop acknowledged delivery")
            except Deferred as exc:
                r.update(detail=str(exc), retry_at=time.time() + 1)
                _save(path, r)
            except Refused as exc:
                _finish(path, r, "blocked", str(exc))
            except bridge.Unavailable as exc:
                r.update(detail=str(exc), attempts=r["attempts"] + 1,
                         retry_at=time.time() + min(60, 2 ** min(r["attempts"], 6)))
                _save(path, r)
            except Exception as exc:
                # Includes failure to save the ack: intent on disk still prevents
                # the next process from blindly transmitting a duplicate.
                if r["status"] == "sending":
                    r.update(status="uncertain", detail=str(exc)[:300], retry_at=time.time() + 60)
                    _save(path, r)
                else:
                    raise
    except Timeout:
        pass


def dispatch_once():
    if not enabled():
        return
    for path in directory().glob("*.json"):
        if _stop.is_set():
            return
        try:
            stat = path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if _terminal_seen.get(str(path)) == fingerprint:
                continue
            process_one(path)
            record = _read(path)
            if record and record["status"] in TERMINAL:
                stat = path.stat()
                _terminal_seen[str(path)] = (stat.st_mtime_ns, stat.st_size)
        except Exception as exc:
            logger.warning("Completion dispatcher could not process %s: %s", path.name, exc)


def start():
    global _thread
    if not enabled():
        return
    with _thread_guard:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        def loop():
            while not _stop.is_set():
                dispatch_once()
                _wake.wait(1)
                _wake.clear()
        _thread = threading.Thread(target=loop, daemon=True, name="codex-completion-dispatcher")
        _thread.start()


def shutdown():
    _stop.set()
    _wake.set()
    # No process kills or long join: sending intent survives an interrupted exit.
