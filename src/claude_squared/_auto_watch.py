#!/usr/bin/env python3
"""PostToolUse asyncRewake hook for claude-squared (stdlib only).

Installed beside wait.py; no package import or model call is needed. The hook
settings own the outer timeout. Keep it above HOOK_TIMEOUT_SECONDS (README).
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import time
import uuid


HOOK_TIMEOUT_SECONDS = 7 * 24 * 3600
WAIT_TIMEOUT_SECONDS = HOOK_TIMEOUT_SECONDS - 60
PAIR_ENV = "CLAUDE_SQUARED_PAIR"
TOOLS = ("pair_send", "pair_send_async", "pair_compact", "pair_create", "pair_invoke", "pair_handoff")
TOOL_PATTERN = r"^mcp__(?:pair|Claude_Squared)__(?:" + "|".join(TOOLS) + r")$"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _responses(value):
    """Unwrap MCP content blocks, FastMCP envelopes and verbose JSON strings.

    Do not recurse into arbitrary result fields: the pair's response itself
    can quote tool results, which must not arm another watcher.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            yield value
        else:
            if isinstance(decoded, (dict, list)):
                yield from _responses(decoded)
            else:
                yield value
    elif isinstance(value, list):
        for item in value:
            yield from _responses(item)
    elif isinstance(value, dict):
        if value.get("isError") or value.get("is_error"):
            return
        yield value
        for key in ("content", "text", "result", "spec", "transparency_msgs"):
            if key in value:
                yield from _responses(value[key])


def _target(payload, pairs):
    if not isinstance(payload, dict) or os.environ.get(PAIR_ENV):
        return None
    if payload.get("hook_event_name") != "PostToolUse":
        return None
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not re.fullmatch(TOOL_PATTERN, tool):
        return None
    session = payload.get("session_id")
    inputs = payload.get("tool_input")
    if not isinstance(session, str) or not session or not isinstance(inputs, dict):
        return None
    name = inputs.get("name")
    # Pair names become wait.py arguments and activity paths. Refuse traversal
    # even if a damaged registry contains it; never interpolate a shell command.
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(c in name for c in "/\\\0") or name.startswith("-")):
        return None
    pending = False
    task_id = None
    for response in _responses(payload.get("tool_response")):
        if isinstance(response, str):
            if response.startswith(f"━━━ pair '{name}' replied ━━━"):
                # The reply can itself quote async handles. Only the server's
                # footer, after the model text, can report background launches.
                response = response.rsplit(f"\n[{name}: ", 1)[-1]
            handle = re.search(r"(?m)^Async task: ([0-9a-f-]+)\s*$", response)
            if handle:
                task_id = handle[1]
            if handle or re.search(r"(?m)^⏳ BACKGROUND WORK LAUNCHED \(", response):
                pending = True
        elif response.get("background_launches"):
            pending = True
    if not pending:
        return None
    if tool.endswith("__pair_handoff"):
        # Handoff's input names the SOURCE; the queued briefing belongs to
        # its newly allocated target (whose name may have a collision suffix).
        task = _read(pairs / "async" / (str(task_id) + ".json"))
        name = task.get("pair_name") if isinstance(task, dict) else None
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(c in name for c in "/\\\0") or name.startswith("-")):
            raise OSError("could not resolve the handoff target from its task handle")
    # Windows readers can briefly miss an atomic registry replacement. A
    # handle already promised a watcher: fail visibly if the retry fails too.
    for attempt in range(3):
        registry = _read(pairs / "registry.json")
        entries = registry.get("pairs") if isinstance(registry, dict) else None
        if isinstance(entries, dict):
            break
        if attempt < 2:
            time.sleep(.05)
    if not isinstance(entries, dict):
        raise OSError("could not read the pair registry; use pair_poll and arm wait.py manually")
    if name not in entries:
        return None
    # Older pair runtimes predate PAIR_ENV, but still have registered sessions.
    if any(isinstance(p, dict) and p.get("session_id") == session for p in entries.values()):
        return None
    return session, name


class _Lock:
    """Kernel-owned lock: process death releases it, with no PID-reuse guess.

    Keep the inode forever. Unlinking after unlock lets two processes lock
    different files at the same path (the classic lock-file removal race).
    """

    def __init__(self, path):
        self.file = os.fdopen(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        self.held = False

    def acquire(self, timeout=0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)

    def release(self):
        if self.held:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.held = False

    def close(self):
        self.release()
        self.file.close()

    def revision(self, value=None):
        self.file.seek(1)
        if value is not None:
            self.file.write(value)
            self.file.truncate()
            self.file.flush()
        else:
            return self.file.read()


def _wait(pairs, name, timeout):
    # Run in THIS process, not a child: a hook killed by its host must not
    # leave an orphan wait.py holding the dedupe lock for the next seven days.
    script = pairs / "wait.py"
    output = io.StringIO()
    argv = sys.argv
    try:
        sys.argv = [str(script), name, "--timeout", str(max(0, timeout))]
        with redirect_stdout(output), redirect_stderr(output):
            watcher = runpy.run_path(str(script))
            code = watcher["main"](sys.argv[1:])
    finally:
        sys.argv = argv
    return code, output.getvalue()


def watch(payload, pairs, timeout=WAIT_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    name = "(unresolved)"
    gate = owner = None
    try:
        target = _target(payload, pairs)
        if target is None:
            return 0
        session, name = target
        directory = pairs / "auto_watch"
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(json.dumps([session, name]).encode("utf-8")).hexdigest()
        gate = _Lock(directory / (key + ".gate"))
        owner = _Lock(directory / (key + ".lock"))
        if not gate.acquire(5):
            raise OSError("could not acquire watcher coordination lock")
        revision = uuid.uuid4().hex.encode("ascii")
        gate.revision(revision)
        if not owner.acquire():
            return 0
        gate.release()
        while True:
            code, output = _wait(pairs, name, deadline - time.monotonic())
            if not gate.acquire(5):
                raise OSError("could not finish watcher coordination")
            # A send can land between wait.py's final idle read and this hook's
            # exit. Its hook records a revision under the short gate lock; the
            # owner checks again before relinquishing ownership. Errors/alerts
            # always wake immediately, even when another send arrived.
            current = gate.revision()
            if code == 0 and current != revision and time.monotonic() < deadline:
                revision = current
                gate.release()
                continue
            print(f"claude-squared auto-watch: pair '{name}', wait.py exit {code}\n{output}",
                  file=sys.stderr)
            if code == 3:
                print("Watcher timed out; the work was NOT stopped. Re-arm using the wait.py "
                      "command above in background Bash.", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"claude-squared auto-watch failed for '{name}': {exc}. "
              f"Check pair_poll('{name}') and re-arm wait.py manually.", file=sys.stderr)
        return 2
    finally:
        # Ownership is released BEFORE the gate, so a racing new request either
        # reaches the existing owner above or starts its own watcher.
        if owner is not None:
            owner.close()
        if gate is not None:
            gate.close()


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    home = os.environ.get("CLAUDE_HOME")
    pairs = Path(home) / "pairs" if home else Path(__file__).resolve().parent
    return watch(payload, pairs)


if __name__ == "__main__":
    sys.exit(main())
