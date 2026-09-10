"""Connector inventories, explicit selection, and headless permission policy."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

from claude_squared.errors import PairError
from claude_squared.models import normalize_permission


INVENTORY_TTL_SECONDS = 600
_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class Connector:
    name: str
    definition: dict[str, Any] | None = None
    kind: str = "local"  # cloud/plugin servers must be inherited by Claude
    enabled: bool = True


def tool_prefix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _key(name: str) -> str:
    return tool_prefix(name).casefold()


def validate_selection(names: list[str] | None) -> None:
    """Refuse a NEW selection naming the pair MCP (create/update only)."""
    for name in names or []:
        if _key(name) in ("pair", "claude_squared"):
            raise PairError(f"connector {name!r} cannot be selected: it is the pair MCP itself; "
                            "loading it would allow recursive pair creation")


def connector_access(level: str, backend: str) -> str | None:
    """User-confirmed headless connector policy, isolated from inventory logic.

    Claude 'allow' means a server-level --allowedTools rule. None adds none.
    Codex returns default_tools_approval_mode; auto retains --approve-for-me.
    MCP server processes themselves are not sandboxed by either backend.
    """
    level = normalize_permission(level)
    if backend == "claude":
        return "allow" if level == "auto" else None
    if backend == "codex":
        return "approve" if level == "unrestricted" else "writes"
    raise ValueError(f"unknown connector backend {backend!r}")


def claude_config_path() -> Path:
    return Path.home() / ".claude.json"


def _json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _list_output(backend: str, cwd: str | None) -> str:
    if backend == "claude":
        from claude_squared.adapters.claude import _claude_executable
        exe = _claude_executable()
    else:
        from claude_squared.adapters.codex import codex_executable
        exe = codex_executable()
    working_dir = os.path.abspath(cwd or os.getcwd())
    key = (backend, exe, working_dir)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < INVENTORY_TTL_SECONDS:
            return cached[1]
    try:
        args = [exe, "mcp", "list"] + (["--json"] if backend == "codex" else [])
        result = subprocess.run(args, cwd=working_dir, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20)
        output = result.stdout or ""  # Claude can list unhealthy servers with a nonzero exit.
    except (OSError, subprocess.SubprocessError):
        output = ""
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), output)
    return output


def claude_inventory(cwd: str | None = None) -> list[Connector]:
    cwd = os.path.abspath(cwd or os.getcwd())
    config = _json_file(claude_config_path())
    definitions: dict[str, dict] = {}
    projects = config.get("projects") or {}
    local = next((value for path, value in projects.items()
                  if os.path.normcase(os.path.abspath(path)) == os.path.normcase(cwd)), {}) \
        if isinstance(projects, dict) else {}
    # Claude precedence: local > project > user.
    for scope in (config, _json_file(Path(cwd) / ".mcp.json"), local):
        servers = scope.get("mcpServers") if isinstance(scope, dict) else None
        if isinstance(servers, dict):
            definitions.update({name: value for name, value in servers.items() if isinstance(value, dict)})
    found = {name: Connector(name, definition) for name, definition in definitions.items()}
    for raw in _list_output("claude", cwd).splitlines():
        line = _ANSI.sub("", raw).strip()
        if ": " not in line:
            continue
        name, _detail = line.split(": ", 1)
        # Only names without a local definition need the inherited config path.
        kind = ("plugin" if name.casefold().startswith("plugin:") else
                "cloud" if name.casefold().startswith(("claude.ai ", "claude_ai_")) else None)
        if name and name not in found and kind is not None:
            found[name] = Connector(name, kind=kind)
    return list(found.values())


def codex_inventory(cwd: str | None = None) -> list[Connector]:
    try:
        rows = json.loads(_list_output("codex", cwd))
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        transport = row.get("transport") or {}
        if not isinstance(transport, dict):
            continue
        # Transport tags/auth status are listing metadata, not config keys.
        definition = {key: value for key, value in transport.items()
                      if key in ("command", "args", "env", "env_vars", "cwd", "url",
                                 "bearer_token_env_var", "http_headers", "env_http_headers") and value is not None}
        for key in ("startup_timeout_sec", "tool_timeout_sec", "enabled_tools", "disabled_tools"):
            if row.get(key) is not None:
                definition[key] = row[key]
        if not definition.get("command") and not definition.get("url"):
            continue
        found.append(Connector(row["name"], definition, enabled=row.get("enabled", True) is not False))
    return found


def inventory(backend: str, cwd: str | None = None) -> list[Connector]:
    return claude_inventory(cwd) if backend == "claude" else codex_inventory(cwd)


def match(name: str, available: list[Connector]) -> Connector | None:
    exact = [entry for entry in available if entry.name.casefold() == name.casefold()]
    candidates = exact or [entry for entry in available if _key(entry.name) == _key(name)]
    return candidates[0] if len(candidates) == 1 and candidates[0].enabled else None


def _is_recursion(name: str) -> bool:
    return _key(name) in ("pair", "claude_squared")


def select(names: list[str] | None, backend: str, cwd: str | None = None,
           *, available: list[Connector] | None = None) -> tuple[list[Connector], list[str]]:
    """Resolve a stored selection. Never raises for a recursion name: a whitelist
    saved before 0.15.0 may contain 'pair' (it was silently ignored then), and
    failing every spawn over it would brick the pair. It is skipped with a note;
    NEW selections are refused up front by validate_selection (create/update)."""
    if not names:
        return [], []  # No opt-in: no subprocess, config reads, or extra notes.
    notes = [f"connector {name} is this pair MCP itself and is never loaded (recursion)"
             for name in names if _is_recursion(name)]
    names = [name for name in names if not _is_recursion(name)]
    if not names:
        return [], notes
    available = inventory(backend, cwd) if available is None else available
    selected = []
    for name in names:
        entry = match(name, available)
        if entry is None:
            notes.append(f"connector {name} is not available and not activated")
        elif entry not in selected:
            selected.append(entry)
    return selected, notes


def claude_args(names: list[str] | None, level: str, cwd: str | None = None,
                allowed_tools: list[str] | None = None) -> list[str]:
    # Spawn path: a stored recursion name is skipped by select(), never fatal.
    names = [name for name in names or [] if not _is_recursion(name)] or None
    available = inventory("claude", cwd) if names else []
    selected, _notes = select(names, "claude", cwd, available=available)
    inherited = any(entry.kind != "local" for entry in selected)
    definitions = {entry.name: entry.definition for entry in selected if entry.kind == "local"}
    args = ([] if inherited else ["--strict-mcp-config"]) + ["--mcp-config", json.dumps({"mcpServers": definitions})]
    denied = ([f"mcp__{tool_prefix(entry.name)}__*" for entry in available if entry not in selected]
              if inherited else ([] if any(tool_prefix(entry.name).startswith("claude_ai_")
                                           for entry in selected) else ["mcp__claude_ai_*"]))
    denied += ["mcp__pair__*", "mcp__Claude_Squared__*"]
    # Also remove actual spellings of recursion servers (case can differ).
    denied += [f"mcp__{tool_prefix(entry.name)}__*" for entry in available
               if _key(entry.name) in ("pair", "claude_squared")]
    for pattern in dict.fromkeys(denied):
        args += ["--disallowed-tools", pattern]
    if names:
        rules = list(allowed_tools or [])
        if connector_access(level, "claude") == "allow":
            rules += [f"mcp__{tool_prefix(entry.name)}" for entry in selected]
        if rules:
            # One variadic option, so later options cannot replace earlier rules.
            args += ["--allowedTools", *dict.fromkeys(rules)]
    return args


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items() if v is not None) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def codex_args(names: list[str] | None, level: str, cwd: str | None = None) -> list[str]:
    selected, _notes = select(names, "codex", cwd)
    definitions = {entry.name: dict(entry.definition or {},
                                  default_tools_approval_mode=connector_access(level, "codex"))
                   for entry in selected}
    if not definitions:
        return []
    # A root inline table preserves literal server names containing dots/spaces;
    # the CLI's dotted -c path parser would split such names into nested keys.
    return ["-c", "mcp_servers=" + _toml(definitions)]


def claude_init_notes(names: list[str] | None, servers: list[dict] | None) -> list[str]:
    statuses = {_key(row["name"]): row.get("status") for row in servers or []
                if isinstance(row, dict) and isinstance(row.get("name"), str)}
    return [f"connector {name} is not connected ({statuses.get(_key(name)) or 'missing from system/init'})"
            for name in names or [] if statuses.get(_key(name)) != "connected"]


def codex_startup_notes(names: list[str] | None, cwd: str | None, stderr: str,
                        events: list[dict] | None = None) -> list[str]:
    selected, notes = select(names, "codex", cwd)
    # exec emits `mcp: NAME failed: ...` and `mcp startup: ...; failed: NAME`.
    # Also accept structured error messages from builds that put them on stdout.
    lines = _ANSI.sub("", stderr).splitlines()
    lines += [str(event.get("message") or "") for event in events or [] if event.get("type") == "error"]
    for entry in selected:
        for line in lines:
            if not re.search(r"\bmcp\b", line, re.I):
                continue
            if not re.search(r"\bmcp:|startup|client|start|connect|initializ|handshake|transport", line, re.I):
                continue  # A failed tool call is not a failed server startup.
            summary = re.search(r"\bmcp startup:.*?\bfailed:\s*(.*)", line, re.I)
            names_part = summary[1] if summary else line
            named = re.search(r"(?<![\w-])" + re.escape(entry.name) + r"(?![\w-])", names_part, re.I)
            failed = re.search(r"failed|failure|timed?\s*out|timeout|could not|unable to", line, re.I)
            if named and failed:
                notes.append(f"connector {entry.name} failed to start: {line.strip()[:300]}")
                break
    return notes
