"""Opt-in live Haiku probe; run by the manager, never by the offline runner.

Question: does the session JSONL's mtime move between turns while background
work (a background Agent and a background Bash) is still running? The warm
runtime's stale check (PairRuntime.is_stale) treats any mtime move after the
last result as "another process wrote the pair's session" and respawns, which
kills that background work.

Records every stdout event and every mtime change (with what was appended) to
a fresh directory under .test-homes. Stops only the process it started.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".test-homes"
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bg-jsonl-probe-", dir=scratch))
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    cli = os.environ.get("CLAUDE_PAIR_CLI_PATH") or shutil.which("claude")
    if not cli:
        raise SystemExit("claude not on PATH")
    args = [cli, "--print", "--verbose", "--model", "haiku", "--effort", "low",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--allowed-tools", "Bash(sleep *)", "Agent"]
    events: queue.Queue = queue.Queue()
    t0 = time.monotonic()
    out = (work / "probe.jsonl").open("w", encoding="utf-8")

    def rec(**kw):
        out.write(json.dumps({"s": round(time.monotonic() - t0, 3), **kw}) + "\n")
        out.flush()

    proc = subprocess.Popen(args, cwd=work, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=(work / "stderr.log").open("wb"),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def read():
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                pass
        events.put(None)
    threading.Thread(target=read, daemon=True).start()
    proc.stdin.write((json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "message": {
        "role": "user", "content":
            "Do exactly this, then end your turn immediately without waiting: "
            "(1) start a Bash command `sleep 45` with run_in_background=true; "
            "(2) start an Agent with run_in_background=true whose prompt is: "
            "'Run the Bash command sleep 20, then run sleep 15, then reply DONE.' "
            "Then reply STARTED and stop. Do not wait for either."}}) + "\n").encode())
    proc.stdin.flush()

    jsonl: Path | None = None
    last_mtime = None
    last_size = 0
    results = 0
    first_result_at = None
    try:
        while time.monotonic() - t0 < 150:
            try:
                ev = events.get(timeout=0.2)
            except queue.Empty:
                ev = "tick"
            if ev is None:
                rec(note="stdout closed")
                break
            if isinstance(ev, dict):
                brief = {k: ev.get(k) for k in ("type", "subtype", "session_id", "parent_tool_use_id")}
                if ev.get("type") == "assistant":
                    brief["content"] = str((ev.get("message") or {}).get("content"))[:160]
                if ev.get("type") == "system":
                    brief["detail"] = str({k: v for k, v in ev.items() if k not in ("tools", "mcp_servers", "slash_commands", "agents", "skills", "plugins")})[:300]
                rec(event=brief)
                if ev.get("type") == "system" and ev.get("subtype") == "init" and jsonl is None:
                    enc = re.sub(r"[^A-Za-z0-9]", "-", str(work))
                    jsonl = Path.home() / ".claude" / "projects" / enc / f"{ev['session_id']}.jsonl"
                    rec(jsonl=str(jsonl))
                if ev.get("type") == "result":
                    results += 1
                    if jsonl and jsonl.exists():
                        last_mtime = jsonl.stat().st_mtime
                        last_size = jsonl.stat().st_size
                    if first_result_at is None:
                        first_result_at = time.monotonic()
                    rec(note=f"result #{results}: watermark mtime={last_mtime} size={last_size}")
            if jsonl is not None and last_mtime is not None and jsonl.exists():
                st = jsonl.stat()
                if st.st_mtime > last_mtime + 0.001:
                    with jsonl.open("rb") as f:
                        f.seek(last_size)
                        added = f.read().decode("utf-8", "replace")
                    kinds = []
                    for line in added.splitlines():
                        try:
                            row = json.loads(line)
                        except ValueError:
                            kinds.append("partial")
                            continue
                        kinds.append(f"{row.get('type')}/{row.get('operation') or row.get('subtype') or ''}"
                                     f"{' side' if row.get('isSidechain') else ''}")
                    rec(mtime_moved=round(st.st_mtime - last_mtime, 3), size_delta=st.st_size - last_size,
                        appended=kinds)
                    last_mtime, last_size = st.st_mtime, st.st_size
    finally:
        if proc.poll() is None:
            proc.kill()
        out.close()
    print(work / "probe.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
