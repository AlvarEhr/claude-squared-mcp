"""Opt-in Haiku probe, adapted from probes/driver.py (2026-10-26).

Run by the main session, outside the implementer's offline-only sandbox:
    python tests/smoke_live_auto_watch.py --live

Uses a private pair registry and a stream-json manager, with explicit settings
and MCP config files. Never edits user settings or reads credentials. Keeps
evidence under .test-homes. Stop only exact PIDs started here, never image names.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="spend a few short Haiku turns")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not args.live:
        parser.error("requires --live; this probe spends Claude usage")
    scratch = ROOT / ".test-homes" / ("live-auto-watch-" + uuid.uuid4().hex[:12])
    home = scratch / "claude"
    home.mkdir(parents=True)
    os.environ["CLAUDE_HOME"] = str(home)
    os.environ["CODEX_HOME"] = str(scratch / "codex")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ.pop("CLAUDE_SQUARED_PAIR", None)
    # The Claude CLI uses its normal projects directory despite CLAUDE_HOME;
    # the isolated pair registry must be able to find those session files.
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "projects"), str(projects)],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        (home / "projects").symlink_to(projects, target_is_directory=True)
    sys.path.insert(0, str(ROOT / "src"))
    from claude_squared._auto_watch import HOOK_TIMEOUT_SECONDS, TOOL_PATTERN
    from claude_squared._auto_watch_script import install_auto_watch_script
    from claude_squared._wait_script import install_wait_script
    from claude_squared.process_spawn import hidden_process_kwargs

    pairs = home / "pairs"
    install_wait_script(pairs / "wait.py")
    script = install_auto_watch_script(pairs / "auto_watch.py")
    config = home / "settings.json"
    config.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": TOOL_PATTERN, "hooks": [{
        "type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"',
        "asyncRewake": True, "timeout": HOOK_TIMEOUT_SECONDS}]}]}}), encoding="utf-8")
    mcp = scratch / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"pair": {"type": "stdio", "command": sys.executable,
        "args": ["-B", "-m", "claude_squared"], "env": {
            "PYTHONPATH": str(ROOT / "src"), "CLAUDE_HOME": str(home),
            "CODEX_HOME": str(scratch / "codex")}}}}), encoding="utf-8")
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError("claude is not on PATH")
    token = "AUTO_WATCH_DONE_" + uuid.uuid4().hex[:8]
    py = shlex.quote(Path(sys.executable).as_posix())
    command = f'{py} -c "import time; time.sleep(20); print(\'{token}\')"'
    prompt = (
        "This is a small hook integration probe. Use ONLY the pair MCP tools. "
        "Call pair_create exactly once with name='rewake-probe', model='haiku', "
        f"cwd={str(scratch)!r}, allowed_tools=['Bash'], "
        f"initial_message={('Run this command in foreground Bash: ' + command + '. Then reply exactly ' + token)!r}. "
        "When create returns its async handle, reply exactly ARMED and end your turn. "
        "Do not start a watcher or poll yet. On a subsequent auto-watch hook notification, "
        "call pair_poll('rewake-probe', with_turn_log=True) once. "
        f"Only if the task is done and its reply contains {token}, reply exactly WOKEN."
    )
    events = queue.Queue()
    logfile = scratch / "manager.jsonl"
    argv = [cli, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", "haiku", "--no-chrome", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(config),
            "--allowedTools", "mcp__pair__pair_create", "mcp__pair__pair_poll"]
    with (scratch / "manager.stderr").open("w", encoding="utf-8") as errors:
        child = subprocess.Popen(argv, **hidden_process_kwargs({"cwd": scratch,
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": errors,
            "text": True, "encoding": "utf-8"}))
        def read():
            with logfile.open("w", encoding="utf-8") as log:
                for line in child.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        pass
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        results, calls = [], []
        started = time.monotonic()
        try:
            child.stdin.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": prompt}}) + "\n")
            child.stdin.flush()
            while time.monotonic() - started < args.timeout and len(results) < 2:
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    if child.poll() is not None:
                        break
                    continue
                if event.get("type") == "assistant":
                    for item in event.get("message", {}).get("content", []):
                        if item.get("type") == "tool_use":
                            calls.append((len(results), item.get("name")))
                if event.get("type") == "result":
                    results.append(event)
                    print(f"{time.monotonic() - started:.1f}s result {len(results)}: "
                          f"{event.get('result')!r}", flush=True)
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()  # exact PID spawned above, never by image name
                child.wait(timeout=10)
            reader.join(timeout=5)
    print(f"Evidence: {scratch}")
    assert len(results) == 2, "manager did not wake; inspect manager.jsonl and manager.stderr"
    assert str(results[0].get("result")).strip() == "ARMED", results[0]
    assert str(results[1].get("result")).strip() == "WOKEN", results[1]
    assert calls == [(0, "mcp__pair__pair_create"), (1, "mcp__pair__pair_poll")], calls
    states = [json.loads(p.read_text(encoding="utf-8")) for p in (pairs / "async").glob("*.json")]
    assert any(s.get("status") == "done" and token in json.dumps(s.get("result")) for s in states)
    print("PASS: real Haiku pair finished; idle stream-json manager woke and polled without Bash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
