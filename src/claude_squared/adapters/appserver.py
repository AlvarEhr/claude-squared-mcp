"""Small stdio JSON-RPC client shared by Codex maintenance operations."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable

from claude_squared.errors import CLIError


class AppServerClient:
    """Own one app-server process and retain notifications received during RPCs."""

    def __init__(self, args: list[str], *, cwd: str | None, timeout_seconds: int,
                 kill: Callable, should_stop: Callable[[], bool] | None = None,
                 stop_message: str = "codex app-server operation stopped") -> None:
        self.args = args
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.kill = kill
        self.should_stop = should_stop
        self.stop_message = stop_message
        self.proc: subprocess.Popen | None = None
        self.deadline = 0.0
        self._messages: list[dict] = []
        self._lock = threading.Lock()
        self._stderr: list[str] = []
        self._readers: list[threading.Thread] = []
        self._next_id = 0
        self._last_stop_check = 0.0

    def __enter__(self) -> AppServerClient:
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, cwd=self.cwd)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(self.args, **kwargs)
        except OSError as exc:
            raise CLIError(f"could not start codex app-server ({self.args[0]}): {exc}") from exc
        self.deadline = time.monotonic() + max(30, int(self.timeout_seconds))
        for stream, is_error in ((self.proc.stdout, False), (self.proc.stderr, True)):
            reader = threading.Thread(target=self._read, args=(stream, is_error), daemon=True)
            self._readers.append(reader)
            reader.start()
        return self

    def _read(self, stream, is_error: bool) -> None:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace")
            if is_error:
                self._stderr.append(text)
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                with self._lock:
                    self._messages.append(message)

    def messages(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def check_stop(self) -> None:
        now = time.monotonic()
        if self.should_stop is None or now - self._last_stop_check < 1.0:
            return
        self._last_stop_check = now
        try:
            cancelled = self.should_stop()
        except CLIError:
            raise
        except Exception:
            return
        if cancelled:
            self.fail(self.stop_message)

    def send(self, method: str, params: dict | None = None, *, request_id: int | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if request_id is not None:
            message["id"] = request_id
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as exc:
            self.fail(f"codex app-server {method} write failed: {exc}")

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self.send(method, params, request_id=request_id)
        assert self.proc is not None
        while time.monotonic() < self.deadline:
            self.check_stop()
            # Inspect buffered replies even if the process has just exited.
            for message in self.messages():
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    self.fail(f"codex app-server {method} failed: {json.dumps(message['error'])[:300]}")
                if "result" in message:
                    return message["result"]
            if self.proc.poll() is not None:
                self.fail(f"codex app-server exited (code {self.proc.poll()}) before {method} completed")
            time.sleep(0.2)
        self.fail(f"codex app-server {method} failed: timeout")

    def initialize(self) -> None:
        from claude_squared import __version__
        self.request("initialize", {"clientInfo": {"name": "claude-squared", "version": __version__}})
        self.send("initialized")

    def fail(self, message: str) -> None:
        if self.proc is not None:
            self.kill(self.proc)
        raise CLIError(message, stderr="".join(self._stderr)[-1500:])

    def __exit__(self, *_exc) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.kill(self.proc)
        for reader in self._readers:
            reader.join(timeout=1)
