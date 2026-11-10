"""Main-session-only Haiku check: python tests/smoke_live_host_shutdown.py --live.

Starts two real stdio MCP servers against a private CLAUDE_HOME. Never edits
settings or reads credentials. Claude's normal projects directory is junctioned
as required by CLAUDE.md; evidence is retained, never recursively removed.
Cleanup stops only exact PIDs started by this harness (creation-time checked
for the first server's runtime). Never kill processes by image name.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class Server:
    def __init__(self, scratch, label, env):
        self.output = queue.Queue()
        self.sequence = 0
        self.errors = (scratch / (label + ".stderr")).open("w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-B", "-m", "claude_squared"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.errors, text=True, encoding="utf-8", bufsize=1)
        def read():
            with (scratch / (label + ".jsonl")).open("w", encoding="utf-8") as log:
                for line in self.proc.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        self.output.put(json.loads(line))
                    except ValueError:
                        pass
            self.output.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "host-shutdown-probe", "version": "1"}})
            self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def write(self, frame):
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params, timeout=120):
        self.sequence += 1
        ident = self.sequence
        self.write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            response = self.output.get(timeout=max(.01, until - time.monotonic()))
            if response is None:
                raise AssertionError("MCP server exited before responding; inspect its evidence log")
            if response.get("id") == ident:
                if "error" in response:
                    raise AssertionError(response["error"])
                return response["result"]
        raise AssertionError("MCP request timed out")

    def tool(self, tool_name, **arguments):
        response = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        if response.get("isError"):
            raise AssertionError(response)
        text = "\n".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")
        try:
            envelope = json.loads(text)
            if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                return envelope["result"]
        except ValueError:
            pass
        return text

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()  # only our exact server PID
                self.proc.wait(timeout=5)
        self.reader.join(timeout=2)
        self.proc.stdout.close()
        self.errors.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="spend a few Haiku turns")
    args = parser.parse_args()
    if not args.live:
        parser.error("requires --live; this harness spends Claude usage")
    scratch = ROOT / ".test-homes" / ("live-host-shutdown-" + uuid.uuid4().hex[:12])
    home = scratch / "claude"
    home.mkdir(parents=True)
    os.environ.update(CLAUDE_HOME=str(home), CODEX_HOME=str(scratch / "codex"))
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "projects"), str(projects)],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        (home / "projects").symlink_to(projects, target_is_directory=True)
    # Isolation precedes every package import, including exit/orphan hooks.
    sys.path.insert(0, str(ROOT / "src"))
    from claude_squared import process_identity
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    first = second = None
    receipt = None
    canary = "HOST_SHUTDOWN_DONE_" + uuid.uuid4().hex[:8]
    marker = scratch / "tool-started"
    code = f"from pathlib import Path; import time; Path({marker.as_posix()!r}).write_text('started'); time.sleep(20); print({canary!r})"
    command = shlex.quote(Path(sys.executable).as_posix()) + " -c " + shlex.quote(code)
    try:
        first = Server(scratch, "server-1", env)
        first.tool("pair_create", name="survivor", model="haiku", effort="low", permission_mode="workspace",
                   cwd=str(scratch), allowed_tools=["Bash"])
        ack = first.tool("pair_send_async", name="survivor", message=(
            f"Run this foreground Bash command exactly: {command}\n"
            f"After it finishes, reply exactly {canary}. Do not launch background work."))
        match = re.search(r"(?m)^Async task: ([\w-]+)", ack)
        assert match, ack
        task_id = match[1]
        until = time.monotonic() + 90
        while not marker.exists():
            if time.monotonic() >= until:
                raise AssertionError("pair did not start the sleep tool; inspect the private logs")
            time.sleep(.1)
        receipt_path = home / "pairs" / "handoff" / "survivor.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["server_pid"] == first.proc.pid, receipt
        started = time.perf_counter()
        first.proc.stdin.close()
        first.proc.wait(timeout=.3)
        shutdown_ms = (time.perf_counter() - started) * 1000
        print(f"stdin EOF -> first server exit: {shutdown_ms:.1f} ms", flush=True)
        second = Server(scratch, "server-2", env)
        pending = second.tool("pair_poll", task_id=task_id)
        assert "finishing after a host restart" in pending, pending
        watcher = subprocess.run([sys.executable, str(home / "pairs" / "wait.py"), task_id,
                                  "--ignore-alert", "--timeout", "120"], env=env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=135)
        assert watcher.returncode in (0, 7), watcher.stdout + watcher.stderr
        result = json.loads(second.tool("pair_poll", task_id=task_id, verbose=True))
        assert result["status"] == "done", result
        assert result["result"]["response"].strip() == canary, result
        assert not receipt_path.exists(), "receipt was not retired"
        (scratch / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"PASS: Haiku completed after host shutdown; successor recovered the canary. Evidence: {scratch}")
    finally:
        if receipt is None:
            try:
                receipt = json.loads((home / "pairs" / "handoff" / "survivor.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        for server in (second, first):
            if server is not None:
                server.close()
        if receipt and first and receipt.get("server_pid") == first.proc.pid:
            pid = receipt["runtime_pid"]
            with process_identity.matching_process(pid, receipt["runtime_started_at"]) as ours:
                if ours:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        os.killpg(pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
