"""Verify the rebuilt extension over stdio in isolated temporary state."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

bundle = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix="cs-bundle-smoke-", ignore_cleanup_errors=True) as tmp:
    root = Path(tmp).resolve()
    unpacked = root / "extension"
    unpacked.mkdir()
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            if not (unpacked / name).resolve().is_relative_to(unpacked):
                raise ValueError("Unsafe archive path")
        archive.extractall(unpacked)
    stderr = root / "server-stderr.txt"
    with stderr.open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-B", str(unpacked / "server" / "main.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8",
            cwd=root, env=dict(os.environ, CLAUDE_HOME=str(root / "claude"), PYTHONDONTWRITEBYTECODE="1"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        messages = queue.Queue()
        def reader():
            for line in process.stdout:
                messages.put(json.loads(line))
            messages.put({"closed": True})
        threading.Thread(target=reader, daemon=True).start()
        def send(value):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **value})+"\n")
            process.stdin.flush()
        def request(identifier, method, params):
            send({"id": identifier, "method": method, "params": params})
            deadline = time.monotonic()+20
            while True:
                result = messages.get(timeout=max(0.01, deadline-time.monotonic()))
                if result.get("closed"):
                    raise RuntimeError("MCP process closed before replying")
                if result.get("id") == identifier:
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    return result["result"]
        try:
            initialized = request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "packaged-mcp-smoke", "version": "1"}})
            send({"method": "notifications/initialized"})
            catalog = request(2, "tools/list", {})
            names = {item["name"] for item in catalog["tools"]}
            assert {"pair_list", "pair_tool_detail", "pair_send_async"} <= names
            listed = request(3, "tools/call", {"name": "pair_list", "arguments": {}})
            assert not listed.get("isError"), listed
            assert "no pairs registered" in json.dumps(listed), listed
            print(json.dumps({"status": "PASS", "tool_count": len(names),
                              "server": initialized["serverInfo"], "pair_state": "isolated and empty"}))
        except Exception:
            print(stderr.read_text(encoding="utf-8")[-3000:])
            raise
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
