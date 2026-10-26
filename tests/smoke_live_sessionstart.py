"""Opt-in SessionStart asyncRewake/resume probe (three small Haiku turns).

Main session only: python tests/smoke_live_sessionstart.py --live
No user settings edits, credential reads or pair registry access. Own session
transcripts are created by the official CLI; evidence stays in .test-homes.
Never stop processes by image name: only the exact child PID started here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--claude", default="claude", help="CLI executable; may name Desktop's bundled build")
    parser.add_argument("--delay", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not args.live:
        parser.error("requires --live; this spends three small Haiku turns")
    if args.delay < 1 or args.timeout < args.delay + 30:
        parser.error("--delay must be positive and --timeout must leave at least 30 seconds beyond it")
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".test-homes" / ("sessionstart-live-" + uuid.uuid4().hex[:10])
    scratch.mkdir(parents=True)
    sid = str(uuid.uuid4())
    marker = "SESSIONSTART-REWAKE-" + uuid.uuid4().hex[:8]
    hook = scratch / "hook.py"
    hook.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "p=Path(__file__).parent\n"
        "event=json.load(sys.stdin)\n"
        "with (p/'hook-events.jsonl').open('a',encoding='utf8') as f: "
        "f.write(json.dumps({'at':time.time(),'event':event})+'\\n')\n"
        "if event.get('source') != 'resume': sys.exit(0)\n"
        f"time.sleep({args.delay!r})\n"
        "(p/'hook-finished.json').write_text(json.dumps({'at':time.time(),"
        "'session_id':event.get('session_id')}),encoding='utf8')\n"
        f"print({marker!r},file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8")
    settings = scratch / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "startup|resume", "hooks": [{
        "type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{hook.as_posix()}"',
        "asyncRewake": True, "timeout": max(300, args.delay + 60)}]}]}}), encoding="utf-8")
    cli = shutil.which(args.claude)
    if not cli:
        raise RuntimeError("Claude CLI not found")
    env = {**os.environ, "CLAUDE_HOME": str(scratch / "claude"), "CODEX_HOME": str(scratch / "codex"),
           "PYTHONIOENCODING": "utf-8"}
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_SQUARED_PAIR", None)
    base = [cli, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", "haiku", "--no-chrome", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", str(settings), "--system-prompt",
            f"You are a hook probe. Never call tools. Reply READY to a user prompt. "
            f"If a later hook message contains {marker}, reply exactly WOKEN."]

    def run(label, flags, expected):
        events = queue.Queue()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with (scratch / f"{label}.stderr").open("w", encoding="utf-8") as errors:
            child = subprocess.Popen(base + flags, cwd=scratch, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", **kwargs)
            def read():
                with (scratch / f"{label}.jsonl").open("w", encoding="utf-8") as log:
                    for line in child.stdout:
                        log.write(line)
                        log.flush()
                        try:
                            events.put((time.time(), json.loads(line)))
                        except ValueError:
                            pass
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            results = []
            try:
                child.stdin.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": "Reply READY, without tools, and end your turn."}}) + "\n")
                child.stdin.flush()
                deadline = time.monotonic() + args.timeout
                while len(results) < expected and time.monotonic() < deadline:
                    try:
                        at, event = events.get(timeout=1)
                    except queue.Empty:
                        if child.poll() is not None:
                            break
                        continue
                    if event.get("type") == "result":
                        results.append((at, event))
                        print(label, len(results), repr(event.get("result")), flush=True)
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()  # only this exact child; never an image-name kill
                    child.wait(timeout=10)
                reader.join(timeout=5)
            return results

    print(f"Evidence: {scratch}\nRequested session_id: {sid}", flush=True)
    seed = run("seed", ["--session-id", sid], 1)
    assert len(seed) == 1 and seed[0][1].get("result", "").strip() == "READY", seed
    resumed = run("resume", ["--resume", sid], 2)
    hooks = [json.loads(line) for line in (scratch / "hook-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["event"].get("source") == "resume" and row["event"].get("session_id") == sid for row in hooks), hooks
    assert len(resumed) == 2, "no SessionStart rewake; inspect the saved raw stream and stderr"
    assert [event.get("result", "").strip() for _, event in resumed] == ["READY", "WOKEN"], resumed
    finished = json.loads((scratch / "hook-finished.json").read_text(encoding="utf-8"))
    assert resumed[0][0] < finished["at"] <= resumed[1][0], "hook did not fire while the manager was idle"
    assert all(event.get("session_id") == sid for _, event in seed + resumed)
    print("PASS: --resume reused the session ID; SessionStart asyncRewake woke the idle stream-json manager.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
