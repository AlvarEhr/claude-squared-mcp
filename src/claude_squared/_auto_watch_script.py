"""Install the opt-in hook and recognize the documented user settings snippet."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import tempfile

from claude_squared._auto_watch import HOOK_TIMEOUT_SECONDS, PAIR_ENV


def install_auto_watch_script(target: Path) -> Path:
    source = Path(__file__).with_name("_auto_watch.py").read_text(encoding="utf-8")
    try:
        if target.read_text(encoding="utf-8") == source:
            return target.resolve()
    except (OSError, UnicodeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    # Several MCP servers can start together. Each writer needs its own temp
    # path; sharing a .tmp name races another startup's replace.
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix="auto-watch-", suffix=".tmp", delete=False) as f:
            scratch = Path(f.name)
            f.write(source)
        scratch.replace(target)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
    return target.resolve()


def auto_watch_configured(script: Path | None, tool: str) -> bool:
    """Conservative best effort, NOT a receipt that the host started a hook.

    Only user settings are visible to this MCP. Project/managed settings and
    host support may differ, so the README calls out that limitation.
    """
    if script is None or not script.is_file() or os.environ.get(PAIR_ENV):
        return False
    from claude_squared.registry import claude_home
    settings_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or claude_home())
    try:
        settings = json.loads((settings_home / "settings.json").read_text(encoding="utf-8"))
        if not isinstance(settings, dict) or settings.get("disableAllHooks"):
            return False
        for group in settings.get("hooks", {}).get("PostToolUse", []):
            matcher = group.get("matcher", "")
            # Require both supported aliases: the server cannot tell which
            # tool namespace the calling host exposed.
            if not all(re.search(matcher, f"mcp__{alias}__{tool}")
                       for alias in ("pair", "Claude_Squared")):
                continue
            for hook in group.get("hooks", []):
                if (hook.get("type") != "command" or hook.get("asyncRewake") is not True
                        or hook.get("timeout", 0) < HOOK_TIMEOUT_SECONDS):
                    continue
                command = shlex.split(hook.get("command", ""))
                # The documented command is one interpreter and one script,
                # with forward slashes on Windows. No shell wrapper guessing.
                if (len(command) == 2 and re.fullmatch(
                        r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", Path(command[0]).name.lower())
                        and Path(command[1]).expanduser().resolve() == script.resolve()):
                    return True
    except (OSError, ValueError, TypeError, AttributeError, re.error):
        pass
    return False
