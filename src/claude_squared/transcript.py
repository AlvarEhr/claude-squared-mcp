"""Tail recent turns from a pair's session JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def tail_turns(jsonl_path: Path, last_n: int = 10) -> list[dict[str, Any]]:
    """Return the last N user/assistant turns from a Claude Code session JSONL.

    Each entry: {role, content, timestamp, tool_uses?: [...]}.
    Skips system events, tool_results, and partial messages.
    """
    if not jsonl_path.exists():
        return []

    raw_lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    turns: list[dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Conversation turns have type "user" or contain a "message" with role
        msg = obj.get("message")
        if obj.get("type") == "user" and isinstance(msg, dict):
            content = _stringify_content(msg.get("content"))
            if content:
                turns.append({
                    "role": "user",
                    "content": content,
                    "timestamp": obj.get("timestamp"),
                })
        elif obj.get("type") == "assistant" and isinstance(msg, dict):
            content = _stringify_content(msg.get("content"))
            tool_uses = _extract_tool_uses(msg.get("content"))
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": content,
                "timestamp": obj.get("timestamp"),
            }
            if tool_uses:
                entry["tool_uses"] = tool_uses
            turns.append(entry)

    return turns[-last_n:] if last_n > 0 else turns


def _stringify_content(content: Any) -> str:
    """Extract human-readable text from a message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "thinking":
                parts.append(f"[thinking] {block.get('thinking', '')[:200]}")
        return "\n".join(p for p in parts if p)
    return ""


_FILE_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}


def _is_genuine_user_message(ev: dict) -> bool:
    """True if this is a real user prompt turn — NOT a tool_result, NOT a
    non-user event. Used as the rewind/fork cut-point predicate.

    A user event whose content is a non-empty string, or a list containing a
    text block with no tool_result block, is a genuine user message. tool_result
    user events (the function-call returns) are excluded — cutting there would
    leave a dangling tool_use and the API would reject the next turn.
    """
    if ev.get("type") != "user":
        return False
    msg = ev.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip() != ""
    if isinstance(c, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in c)
        has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
        return has_text and not has_tool_result
    return False


def _user_preview(ev: dict, limit: int = 100) -> str:
    msg = ev.get("message") or {}
    c = msg.get("content")
    text = c if isinstance(c, str) else _stringify_content(c)
    text = (text or "").strip().replace("\n", " ")
    return text[:limit]


def _files_written_in(events: list[dict]) -> list[str]:
    """Distinct file paths touched by Write/Edit/NotebookEdit tool_uses in the events."""
    files: list[str] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _FILE_WRITE_TOOLS:
                inp = b.get("input") or {}
                fp = inp.get("file_path") or inp.get("notebook_path")
                if fp and fp not in files:
                    files.append(fp)
    return files


def parse_events(jsonl_path: Path) -> list[tuple[int, dict]]:
    """Return [(raw_line_index, event_dict)] for every non-empty parseable line.
    raw_line_index is the 0-based index into ``splitlines()`` — the unit for
    truncation (keep lines[0:cut])."""
    if not jsonl_path.exists():
        return []
    out: list[tuple[int, dict]] = []
    for i, line in enumerate(jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()):
        s = line.strip()
        if not s:
            continue
        try:
            out.append((i, json.loads(s)))
        except json.JSONDecodeError:
            continue
    return out


def list_user_turn_points(jsonl_path: Path) -> list[dict[str, Any]]:
    """Enumerate genuine user-message boundaries as rewind/fork cut points.

    Each point: {point (1-based), raw_line_index, timestamp, preview,
    after_assistant_turns, after_tool_calls, after_files}. The after_* fields
    describe what the pair DID in response to that user message (up to the next
    user message) — so the agent can tell "rewind past a trivial exchange" from
    "rewind past an hour of work" WITHOUT reading the transcript.
    """
    parsed = parse_events(jsonl_path)
    user_pos = [idx for idx, (_, ev) in enumerate(parsed) if _is_genuine_user_message(ev)]
    points: list[dict[str, Any]] = []
    for n, pos in enumerate(user_pos):
        raw_line_index, ev = parsed[pos]
        next_pos = user_pos[n + 1] if n + 1 < len(user_pos) else len(parsed)
        span = [e for (_, e) in parsed[pos + 1:next_pos]]
        asst_turns = sum(
            1 for e in span if e.get("type") == "assistant"
            and any(isinstance(b, dict) and b.get("type") == "text"
                    for b in (e.get("message") or {}).get("content") or [])
        )
        tool_calls = sum(
            1 for e in span if e.get("type") == "assistant"
            for b in (e.get("message") or {}).get("content") or []
            if isinstance(b, dict) and b.get("type") == "tool_use"
        )
        points.append({
            "point": n + 1,
            "raw_line_index": raw_line_index,
            "timestamp": ev.get("timestamp"),
            "preview": _user_preview(ev),
            "after_assistant_turns": asst_turns,
            "after_tool_calls": tool_calls,
            "after_files": _files_written_in(span),
        })
    return points


def find_sentinel_line(jsonl_path: Path, sentinel: str) -> "int | None":
    """Return the raw_line_index of the LAST user message whose content contains
    ``sentinel`` — used by pair_fork to truncate the auto-injected fork turn.
    Searches from the end (the sentinel turn is at the tip)."""
    parsed = parse_events(jsonl_path)
    for raw_line_index, ev in reversed(parsed):
        if _is_genuine_user_message(ev) and sentinel in _user_preview(ev, limit=10_000):
            return raw_line_index
    return None


def truncate_jsonl_before_line(jsonl_path: Path, raw_line_index: int) -> list[dict]:
    """Keep raw lines [0, raw_line_index); drop the rest. Returns the parsed
    events that were DROPPED (for caller analysis, e.g. files-written report).
    Writes atomically via a .tmp + replace. Caller archives the original first
    if desired."""
    import os as _os
    lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    dropped: list[dict] = []
    for s in lines[raw_line_index:]:
        s = s.strip()
        if not s:
            continue
        try:
            dropped.append(json.loads(s))
        except json.JSONDecodeError:
            continue
    kept = lines[:raw_line_index]
    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    _os.replace(tmp, jsonl_path)
    return dropped


def files_written_in_events(events: list[dict]) -> list[str]:
    """Public wrapper: distinct files touched by Write/Edit/NotebookEdit in events."""
    return _files_written_in(events)


def _extract_tool_uses(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append({
                "name": block.get("name"),
                "input": block.get("input"),
                "id": block.get("id"),
            })
    return out
