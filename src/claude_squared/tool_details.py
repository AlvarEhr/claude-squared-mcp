"""Durable full Codex item events associated with the existing T-N log IDs."""
from __future__ import annotations

import json
import re
from pathlib import Path

from claude_squared.errors import PairError


def _tag(value: str) -> str:
    match = re.fullmatch(r"(?:T-)?(\d+)", value, re.IGNORECASE)
    if not match:
        raise PairError("tool_id must be a T-N tag or its numeric part")
    return f"T-{int(match[1])}"


def save_codex_item(log_dir: Path, tag: str, item: dict, *, completed: bool, run_id: str | None) -> None:
    directory = log_dir / "codex-tool-items"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_tag(tag)}.json"
    record = {}
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    if record.get("run_id") != run_id:
        record = {}
    record.update(tag=tag, run_id=run_id, item_id=item.get("id"), tool_name=item.get("type"))
    record["completed" if completed else "started"] = item
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def codex_tool_detail(log_dir: Path, tool_id: str, *, max_chars: int = 50_000) -> str:
    tag = _tag(tool_id)
    if max_chars < 1:
        raise PairError("max_chars must be positive")
    path = log_dir / "codex-tool-items" / f"{tag}.json"
    if not path.exists():
        raise PairError(f"No full Codex item recorded for {tag}. Older turns only have log previews; "
                        "new turns capture full tool details without clearing the pair.")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise PairError(f"Cannot read Codex detail for {tag}: {exc}") from exc
    lines = [f"=== {tag} (Codex) ===", f"Tool: {record.get('tool_name', '?')}",
             f"Task/run: {record.get('run_id', '?')}", f"Item: {record.get('item_id', '?')}"]
    for key, label in (("started", "Input (started event)"), ("completed", "Result (completed event)")):
        lines.append("\n" + label + ":")
        event = record.get(key)
        if event is None:
            lines.append("(no event captured; it may be incomplete, interrupted, or completion-only)")
        else:
            text = json.dumps(event, indent=2, ensure_ascii=False)
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... [truncated; full event is {len(text)} chars at {path}]"
            lines.append(text)
    return "\n".join(lines)
