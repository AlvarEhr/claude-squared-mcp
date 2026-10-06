"""Exercise the branch MCP over stdio in isolated pair state.

Default is offline: no Desktop callbacks or inference. --live requires an
explicit disposable full-access Desktop manager and sends only to that chat.
Never installs/restarts the user's MCP or kills any process by image name.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--live", action="store_true")
parser.add_argument("--manager")
parser.add_argument("--source-turn")
parser.add_argument("--state-dir", required=True)
parser.add_argument("--restart", action="store_true", help="restart only this isolated MCP while its notification is pending")
args = parser.parse_args()
if args.live and not (args.manager and args.source_turn):
    parser.error("--live requires an explicitly owned disposable manager and source turn")
state = Path(args.state_dir).resolve()
state.mkdir(parents=True, exist_ok=True)
manager, turn, session = args.manager or str(uuid.uuid4()), args.source_turn or str(uuid.uuid4()), str(uuid.uuid4())
env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), CLAUDE_HOME=str(state / "claude"),
           CLAUDE_PAIR_CODEX_COMPLETION="desktop", PYTHONIOENCODING="utf-8",
           COMPLETION_SMOKE_STATE=str(state), COMPLETION_SMOKE_SESSION=session)
if args.restart:
    env["COMPLETION_SMOKE_HOLD"] = "1"
if args.live:
    # Use the real CLI's auth/cache only; all pair/task/activity state is isolated.
    env["CODEX_HOME"] = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    command = [sys.executable, "-B", "-m", "claude_squared"]
else:
    env["CODEX_HOME"] = str(state / "codex")
    rollout = state / "codex/sessions/2026/10/06" / ("rollout-fixture-" + manager + ".jsonl")
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "session_meta", "payload": {"id": manager, "originator": "Codex Desktop", "source": "vscode", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
            {"type": "turn_context", "payload": {"turn_id": turn, "model": "fixture", "cwd": str(state),
                                                  "approval_policy": "never", "sandbox_policy": {"type": "danger-full-access"}}}]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    command = [sys.executable, "-B", str(ROOT / "tests/completion_mcp_fixture.py")]

messages = queue.Queue()
with (state / "mcp-stderr.txt").open("w", encoding="utf-8") as stderr:
    process = subprocess.Popen(command, env=env, cwd=state, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=stderr, text=True, encoding="utf-8",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    def reader(stream, target):
        for line in stream:
            try: target.put(json.loads(line))
            except ValueError: target.put({"protocol_error": line})
        target.put({"closed": True})
    reader_thread = threading.Thread(target=reader, args=(process.stdout, messages), daemon=True)
    reader_thread.start()
    identifier = 0
    def send(message):
        process.stdin.write(json.dumps(dict(jsonrpc="2.0", **message)) + "\n")
        process.stdin.flush()
    def request(method, params):
        global identifier
        identifier += 1
        send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if result.get("closed") or "protocol_error" in result:
                raise RuntimeError(result)
            if result.get("id") == identifier:
                if "error" in result: raise RuntimeError(result["error"])
                return result["result"]
        raise TimeoutError(method)
    worker = "callback-smoke-" + uuid.uuid4().hex[:10] if args.live else "offline-child"
    task_id = None
    try:
        request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "isolated-completion-smoke", "version": "1"}})
        send({"method": "notifications/initialized"})
        catalog = request("tools/list", {})
        for tool in catalog["tools"]:
            assert "ctx" not in tool["inputSchema"].get("properties", {}), tool
        if args.live:
            created = request("tools/call", {"name": "pair_create", "arguments": {
                "name": worker, "backend": "codex", "model": "luna", "effort": "low",
                "permission_mode": "read-only", "cwd": str(state), "verbose": True,
                "purpose": "Disposable branch completion adapter smoke; owned by this harness only"}})
            assert not created.get("isError"), created
            session = json.loads(created["content"][0]["text"])["spec"]["session_id"]
        message = ("This is an isolated completion test. Use one PowerShell command Start-Sleep -Seconds 12; "
                   "then reply exactly PAIR_WORK_DONE. Do not read or write files, spawn agents, or start other "
                   "work. Never kill by image/name. Stop only a specific PID you personally launched, if ever needed.")
        result = request("tools/call", {"name": "pair_send_async", "arguments": {"name": worker, "message": message},
                                      "_meta": {"x-codex-turn-metadata": {"thread_id": manager, "turn_id": turn}}})
        text = result["content"][0]["text"]
        task_id = text.split("Async task: ", 1)[1].splitlines()[0]
        assert "notification armed" in text, result
        assert "Bash(run_in_background" not in text, result
        if args.restart:
            task_path = state / "claude/pairs/async" / (task_id + ".json")
            phase_deadline = time.monotonic() + 25
            while time.monotonic() < phase_deadline:
                task = json.loads(task_path.read_text(encoding="utf-8"))
                ready = (task.get("child_pid") and task.get("events_path") and task["status"] == "running") if args.live else task["status"] == "done"
                if ready: break
                time.sleep(.1)
            assert ready, "isolated worker did not reach the restart checkpoint"
            old_pid = process.pid
            process.stdin.close(); process.wait(timeout=10); reader_thread.join(timeout=2)
            if reader_thread.is_alive(): raise RuntimeError("owned stdio reader did not end")
            (state / "allow_delivery").write_text("owned restart checkpoint", encoding="utf-8")
            messages = queue.Queue()
            process = subprocess.Popen(command, env=env, cwd=state, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=stderr, text=True, encoding="utf-8",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            reader_thread = threading.Thread(target=reader, args=(process.stdout, messages), daemon=True)
            reader_thread.start()
            request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "completion-smoke-restarted", "version": "1"}})
            send({"method": "notifications/initialized"})
            (state / "restart.json").write_text(json.dumps({"old_pid": old_pid, "new_pid": process.pid,
                "worker_child_pid": task.get("child_pid"), "pending_notification_preserved": True}), encoding="utf-8")
        # No repeated pair_poll calls. Wait for this isolated dispatcher's own
        # durable delivery record, after its tools/call RPC has already returned.
        deadline, delivered = time.monotonic() + 100, None
        while time.monotonic() < deadline:
            for path in (state / "claude/pairs/completion-v1").glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if task_id in record["tasks"] and record["status"] in ("delivered", "blocked", "uncertain", "canceled"):
                    delivered = record
            if delivered: break
            time.sleep(.25)
        assert delivered and delivered["status"] == "delivered", delivered
        reply = request("tools/call", {"name": "pair_poll", "arguments": {"task_id": task_id}})
        assert "PAIR_WORK_DONE" in str(reply) if args.live else "OFFLINE_WORK_DONE" in str(reply), reply
        summary = {"status": "PASS", "mode": "live" if args.live else "offline", "restarted": args.restart, "tool_count": len(catalog["tools"]),
                   "context_injection_hidden": True, "manager": manager, "source_turn": turn,
                   "worker": worker, "worker_session": session, "task": task_id,
                   "registration": delivered, "reply": reply}
        (state / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in summary.items() if k not in ("reply", "registration")}), flush=True)
    finally:
        # Forget ONLY this harness's worker, and only if its own task ended.
        if task_id:
            path = state / "claude/pairs/async" / (task_id + ".json")
            try: terminal = json.loads(path.read_text())["status"] != "running"
            except (OSError, ValueError): terminal = False
            if terminal and process.poll() is None:
                request("tools/call", {"name": "pair_forget", "arguments": {"name": worker}})
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Specific owned server PID only. Never image-name/process-tree kills.
            process.terminate(); process.wait(timeout=5)
