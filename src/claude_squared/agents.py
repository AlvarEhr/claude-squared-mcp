"""Custom agent definitions written to ~/.claude/agents/ — visible to all Claude sessions."""

from __future__ import annotations

import re
from pathlib import Path

from claude_squared.registry import agents_dir


SAFE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def _validate_name(name: str) -> None:
    if not SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid agent name '{name}'. Must start with a letter, "
            f"contain only [a-zA-Z0-9_-], and be ≤64 chars."
        )


def define_agent(name: str, description: str, prompt: str,
                 tools: list[str] | None = None, model: str | None = None) -> Path:
    """Write/replace an agent definition. Returns the file path."""
    _validate_name(name)
    if not description.strip():
        raise ValueError("description must not be empty")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")

    front: list[str] = ["---", f"name: {name}", f"description: {description.strip()}"]
    if tools:
        front.append(f"tools: {', '.join(tools)}")
    if model:
        front.append(f"model: {model}")
    front.append("---")
    front.append("")
    front.append(prompt.strip())

    path = agents_dir() / f"{name}.md"
    path.write_text("\n".join(front) + "\n", encoding="utf-8")
    return path


def list_agents() -> list[dict]:
    """List user-defined agents from ~/.claude/agents/."""
    out: list[dict] = []
    for p in sorted(agents_dir().glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        info = _parse_frontmatter(text)
        info["filename"] = p.name
        info["path"] = str(p)
        out.append(info)
    return out


def delete_agent(name: str) -> bool:
    """Remove an agent definition. Returns True if it existed."""
    _validate_name(name)
    path = agents_dir() / f"{name}.md"
    if path.exists():
        path.unlink()
        return True
    return False


def _parse_frontmatter(text: str) -> dict:
    info: dict = {}
    if not text.startswith("---"):
        return info
    end = text.find("\n---", 3)
    if end == -1:
        return info
    fm = text[3:end].strip()
    for line in fm.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info
