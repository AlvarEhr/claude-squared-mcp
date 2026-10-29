"""Opt-in live Haiku probe; run by the manager, never by the offline runner.

Writes raw events to a fresh directory under .test-homes. No package imports,
pair registry access, credential reads or process-name kills. The CLI's login
is inherited; CLAUDE_HOME and CODEX_HOME are isolated. Transcripts use the CLI's
normal location (a unique scratch cwd/session), as in our other live harnesses.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interrupt", action="store_true",
                        help="Interrupt after writing the steer; observe whether it runs later")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".test-homes"
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="steer-probe-", dir=scratch))
    env = {**os.environ, "CLAUDE_HOME": str(work / "claude"),
           "CODEX_HOME": str(work / "codex"), "TEMP": str(work), "TMP": str(work)}
    env.pop("CLAUDECODE", None)
    cli = shutil.which("claude")
    if not cli:
        raise SystemExit("claude not on PATH")
    args = [cli, "--print", "--verbose", "--model", "haiku", "--effort", "low",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-chrome", "--allowed-tools", "Bash(sleep *)"]
    events: queue.Queue = queue.Queue()
    started = time.monotonic()
    with (work / "stderr.log").open("wb") as err, (work / "events.jsonl").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(args, cwd=work, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read():
            for line in proc.stdout:
                events.put(json.loads(line))
            events.put(None)
        threading.Thread(target=read, daemon=True).start()
        def write_frame(frame):
            log.write(json.dumps({"seconds": round(time.monotonic() - started, 3), "sent": frame}) + "\n")
            log.flush()
            proc.stdin.write((json.dumps(frame) + "\n").encode())
            proc.stdin.flush()
        def send(message):
            frame = {"type": "user", "uuid": str(uuid.uuid4()),
                     "message": {"role": "user", "content": message}}
            write_frame(frame)
            return frame["uuid"]
        results = []
        steered = False
        steer_id = None
        interrupted = False
        deadline = started + 150
        try:
            send("Use Bash to run sleep 12 in the foreground, then reply ORIGINAL. Do not run background work.")
            while time.monotonic() < deadline:
                try:
                    ev = events.get(timeout=1)
                except queue.Empty:
                    continue
                if ev is None:
                    break
                log.write(json.dumps({"seconds": round(time.monotonic() - started, 3), "event": ev}) + "\n")
                log.flush()
                if ev.get("type") == "assistant" and not ev.get("parent_tool_use_id") and not steered:
                    blocks = (ev.get("message") or {}).get("content") or []
                    if any(b.get("type") == "tool_use" and b.get("name") == "Bash"
                           and "sleep 12" in (b.get("input") or {}).get("command", "")
                           and not (b.get("input") or {}).get("run_in_background") for b in blocks):
                        time.sleep(.25)
                        steer_id = send("Change of plan: after the running tool finishes, reply STEER_CANARY instead of ORIGINAL.")
                        steered = True
                        print("Wrote follow-up during tool execution", flush=True)
                        if options.interrupt:
                            write_frame({"type": "control_request",
                                         "request_id": "req_int_" + uuid.uuid4().hex,
                                         "request": {"subtype": "interrupt"}})
                            interrupted = True
                            print("Wrote interrupt after follow-up", flush=True)
                if ev.get("type") == "result":
                    results.append(ev)
                    print(json.dumps({"result_number": len(results), "event": ev}), flush=True)
                    if len(results) >= 2 or not steered:
                        break
                    # Leave stdin open to expose any queued second result.
                    deadline = min(deadline, time.monotonic() + (40 if options.interrupt else 25))
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.terminate()  # only the PID this probe spawned
                proc.wait(timeout=10)
    print(f"Evidence: {work}; steered={steered}; results={len(results)}", flush=True)
    if options.interrupt:
        print(json.dumps({"interrupt_written": interrupted, "steer_uuid": steer_id,
                          "results": [{"subtype": ev.get("subtype"),
                                       "user_message_uuids": ev.get("user_message_uuids"),
                                       "consumed_steer": steer_id in (ev.get("user_message_uuids") or []),
                                       "canary_in_reply": "STEER_CANARY" in str(ev.get("result"))}
                                      for ev in results]}), flush=True)
        # Success means evidence was captured, not that interrupt consumed or
        # discarded the follow-up. The manager decides from the raw frames.
        return 0 if interrupted and results else 1
    return 0 if steered and any("STEER_CANARY" in str(ev.get("result")) for ev in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
