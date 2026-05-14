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
