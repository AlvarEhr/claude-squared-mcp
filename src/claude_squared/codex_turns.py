"""Durable Codex exec receipts; all mutations run under the pair send lock.

Only stdin/stdout/stderr are inherited by the child. Receipt files belong to
the supervisor and can be atomically replaced without disturbing its writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid


def receipt_path(events: Path) -> Path:
    return events.with_suffix(".receipt.json")


def save_receipt(events: Path, data: dict) -> None:
    path = receipt_path(events)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(4):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def read_receipt(events: Path) -> dict:
    return json.loads(receipt_path(events).read_text(encoding="utf-8"))


def complete_lines(stream, *, max_lines: int | None = None):
    """Tail a binary file without consuming an unfinished line or UTF-8 codepoint."""
    count = 0
    while max_lines is None or count < max_lines:
        offset = stream.tell()
        raw = stream.readline()
        if not raw.endswith(b"\n"):
            stream.seek(offset)
            return
        count += 1
        yield stream.tell(), raw.decode("utf-8", errors="replace")


def read_events(path: Path) -> list[tuple[int, dict]]:
    out = []
    with open(path, "rb") as stream:
        for offset, line in complete_lines(stream):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                out.append((offset, event))
    return out


def finish_pending_log(events: Path, receipt: dict) -> None:
    pending = receipt.get("pending_log")
    if pending is None:
        return
    path = Path(receipt["log_path"])
    payload = pending["text"].encode("utf-8")
    # Write-ahead receipt closes the append/checkpoint crash window. A second
    # recovering owner completes a partial append instead of duplicating it.
    with open(path, "a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() < pending["offset"]:
            raise OSError("Codex log truncated during pending append; refusing duplicate replay")
        stream.seek(pending["offset"])
        existing = stream.read(len(payload))
        if not payload.startswith(existing):
            raise OSError("Codex log changed during pending append; refusing duplicate replay")
        stream.seek(0, os.SEEK_END)
        stream.write(payload[len(existing):])
        stream.flush()
    receipt["logged_offset"] = pending["events_offset"]
    receipt.pop("pending_log")
    save_receipt(events, receipt)


def append_log(events: Path, receipt: dict, lines: list[str], offset: int) -> None:
    path = Path(receipt["log_path"])
    text = "".join(line.replace("\r", "").replace("\n", " / ") + "\n" for line in lines)
    receipt["pending_log"] = {
        "offset": path.stat().st_size if path.exists() else 0,
        "text": text, "events_offset": offset,
    }
    save_receipt(events, receipt)
    finish_pending_log(events, receipt)


def prune_turns(directory: Path, *, keep_days: int = 30) -> None:
    """Retain unresolved tasks indefinitely; age out completed/untracked runs.

    Called under the pair lock before spawning. Never unlink a child writer's
    files, even after a server restart or a week-long detached turn.
    """
    from claude_squared import async_tasks
    from claude_squared import process_identity

    cutoff = time.time() - keep_days * 86400
    for events in directory.glob("*.events.jsonl"):
        try:
            if events.stat().st_mtime >= cutoff:
                continue
            receipt = read_receipt(events)
            task_id = receipt.get("task_id")
            state = async_tasks.load_task(task_id) if task_id else None
            if state and state.status == "running":
                continue
            pid = receipt.get("child_pid")
            if (pid and async_tasks._is_pid_alive(pid)
                    and process_identity.matches(pid, receipt.get("child_started_at"))):
                continue
            for suffix in ("events.jsonl", "events.receipt.json", "stderr.log", "stdin.txt"):
                (directory / f"{receipt['run_id']}.{suffix}").unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            continue


def bookkeep_send(events: Path, result, **extra) -> None:
    """Publish an absolute target before registry RMW, closing its crash gap.

    The pair lock excludes successors until recovery finishes. Retrying a
    target is safe if the owner died after registry save but before task save.
    """
    from datetime import datetime
    from claude_squared import registry

    receipt = read_receipt(events)
    with registry.locked_registry() as reg:
        spec = reg.pairs.get(result.name)
        if spec is None:
            result.notes.append("pair missing or quarantined; registry bookkeeping skipped")
            return
        if spec.session_id != result.session_id:
            result.notes.append("recovered turn belongs to an earlier session; registry totals left unchanged")
            return
        target = receipt.get("bookkeeping")
        if target is None:
            target = dict(last_active_at=datetime.utcnow().isoformat(),
                          turn_count=spec.turn_count + 1,
                          total_cost_usd=spec.total_cost_usd + float(result.cost_usd or 0.0), **extra)
            receipt["bookkeeping"] = target
            save_receipt(events, receipt)
        # Never decrease totals if an unrelated operation updated the registry.
        fields = {**target, "turn_count": max(spec.turn_count, target["turn_count"]),
                  "total_cost_usd": max(spec.total_cost_usd, target["total_cost_usd"])}
        fields["last_active_at"] = max(spec.last_active_at or datetime.min,
                                        datetime.fromisoformat(target["last_active_at"]))
        reg.pairs[result.name] = spec.model_copy(update=fields)
