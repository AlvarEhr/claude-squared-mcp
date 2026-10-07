"""Opt-in, build-gated Windows Desktop bridge. No app-server is launched here.

Private protocol, smoke-verified 2026-10-06. Unknown builds fail closed.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import json
import os
import re
import struct
import sys
import time

# Idle/active delivery and unchanged manager policy verified 2026-10-06/07.
SUPPORTED_BUILDS = frozenset({"26.930.7945.0", "26.1002.6548.0"})
PIPE_PATTERN = re.compile(r"^\\\\\.\\pipe\\codex-browser-use-[0-9a-f-]{36}$")
MAX_FRAME = 1024 * 1024


class Unavailable(RuntimeError):
    """No callback was sent; discovery/preflight can be retried."""


class Uncertain(RuntimeError):
    """A callback may have reached Desktop. Never blindly retry it."""


def kernel():
    if sys.platform != "win32":
        raise Unavailable("Desktop completion callbacks currently require Windows")
    k = C.WinDLL("kernel32", use_last_error=True)
    for name in ("CreateFileW", "CreateEventW", "OpenProcess", "CreateToolhelp32Snapshot"):
        getattr(k, name).restype = W.HANDLE
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateEventW.argtypes = [W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR]
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CloseHandle.argtypes = [W.HANDLE]
    k.GetNamedPipeServerProcessId.argtypes = [W.HANDLE, C.POINTER(W.ULONG)]
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    return k


def process_image(k, pid):
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        raise Unavailable("cannot verify Desktop process identity")
    try:
        path, size = C.create_unicode_buffer(32768), W.DWORD(32768)
        if not k.QueryFullProcessImageNameW(h, 0, path, C.byref(size)):
            raise Unavailable("cannot read Desktop executable path")
        return path.value
    finally:
        k.CloseHandle(h)


def desktop_ancestor():
    """Identify THIS MCP's app instance, not the foreground window."""
    from claude_squared.process_identity import started_at

    k = kernel()
    class Entry(C.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("ppid", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("exe", W.WCHAR * 260)]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    h = k.CreateToolhelp32Snapshot(2, 0)
    if h == C.c_void_p(-1).value:
        raise Unavailable("cannot inspect Desktop ancestry")
    parents = {}
    try:
        entry = Entry(dwSize=C.sizeof(Entry))
        more = k.Process32FirstW(h, C.byref(entry))
        while more:
            parents[entry.pid] = (entry.ppid, entry.exe)
            more = k.Process32NextW(h, C.byref(entry))
    finally:
        k.CloseHandle(h)
    pid, seen, candidate = os.getpid(), set(), None
    for _ in range(32):
        if pid in seen or pid not in parents:
            break
        seen.add(pid)
        parent, exe = parents[pid]
        if exe.lower() in ("chatgpt.exe", "codex.exe"):
            image = process_image(k, pid)
            match = re.search(r"[\\/]OpenAI\.Codex_([\d.]+)_.*[\\/]app[\\/](?:ChatGPT|Codex)\.exe$",
                              image, re.I)
            if match:
                if match[1] not in SUPPORTED_BUILDS:
                    raise Unavailable("Desktop build %s has not passed callback compatibility tests" % match[1])
                birth = started_at(pid)
                own_birth = started_at(os.getpid())
                if birth is None or own_birth is None or birth > own_birth:
                    raise Unavailable("Desktop creation time is unavailable")
                candidate = {"pid": pid, "started_at": birth, "image": image, "build": match[1]}
        pid = parent
    if candidate is not None:
        return candidate
    raise Unavailable("this process is not descended from a supported Codex Desktop")


class Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", W.DWORD), ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


class Connection:
    def __init__(self, pipe, app, timeout=2.0):
        from claude_squared.process_identity import started_at
        if not PIPE_PATTERN.fullmatch(pipe):
            raise Unavailable("not an app-tools pipe path")
        self.k, self.deadline = kernel(), time.monotonic() + timeout
        self.h = self.k.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0x40000000, None)
        if self.h == C.c_void_p(-1).value:
            raise Unavailable("app-tools pipe is not available")
        try:
            pid = W.ULONG()
            if not self.k.GetNamedPipeServerProcessId(self.h, C.byref(pid)):
                raise Unavailable("cannot identify pipe owner")
            birth = started_at(pid.value)
            if (pid.value != app["pid"] or birth is None
                    or abs(birth - app["started_at"]) > .001
                    or process_image(self.k, pid.value) != app["image"]):
                raise Unavailable("pipe belongs to a different Desktop process")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.h is not None:
            self.k.CloseHandle(self.h)
            self.h = None

    def io(self, count, data=None):
        k = self.k
        k.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), C.POINTER(Overlapped)]
        k.WriteFile.argtypes = k.ReadFile.argtypes
        k.GetOverlappedResult.argtypes = [W.HANDLE, C.POINTER(Overlapped), C.POINTER(W.DWORD), W.BOOL]
        k.CancelIoEx.argtypes = [W.HANDLE, C.POINTER(Overlapped)]
        ms = int((self.deadline - time.monotonic()) * 1000)
        if ms <= 0:
            raise TimeoutError("app-tools I/O deadline expired")
        buf = C.create_string_buffer(data, len(data)) if data is not None else C.create_string_buffer(count)
        ov, n = Overlapped(hEvent=k.CreateEventW(None, True, False, None)), W.DWORD()
        if not ov.hEvent:
            raise OSError("cannot allocate pipe I/O event")
        try:
            ok = (k.WriteFile if data is not None else k.ReadFile)(self.h, buf, count, C.byref(n), C.byref(ov))
            if not ok and C.get_last_error() != 997:
                raise C.WinError(C.get_last_error())
            if not ok:
                if k.WaitForSingleObject(ov.hEvent, ms) != 0:
                    k.CancelIoEx(self.h, C.byref(ov))
                    k.GetOverlappedResult(self.h, C.byref(ov), C.byref(n), True)
                    raise TimeoutError("app-tools I/O timed out")
                if not k.GetOverlappedResult(self.h, C.byref(ov), C.byref(n), False):
                    raise C.WinError(C.get_last_error())
            return buf.raw[:n.value]
        finally:
            k.CloseHandle(ov.hEvent)

    def read_exact(self, count):
        result = bytearray()
        while len(result) < count:
            chunk = self.io(count - len(result))
            if not chunk:
                raise EOFError("app-tools pipe closed")
            result.extend(chunk)
        return bytes(result)

    def request(self, method, params, *, delivery=False):
        frame = encode_frame({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        try:
            sent = 0
            while sent < len(frame):
                n = len(self.io(len(frame) - sent, frame[sent:]))
                if not n:
                    raise EOFError("app-tools write closed")
                sent += n
            size = struct.unpack("<I", self.read_exact(4))[0]
            if not 0 < size <= MAX_FRAME:
                raise ValueError("invalid app-tools frame size")
            reply = json.loads(self.read_exact(size))
            return decode_response(reply)
        except Exception as exc:
            raise (Uncertain if delivery else Unavailable)(str(exc)) from exc


def encode_frame(message):
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise Unavailable("app-tools request exceeds the frame limit")
    return struct.pack("<I", len(data)) + data


def decode_response(reply):
    if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or reply.get("id") != 1:
        raise ValueError("invalid app-tools response")
    if "error" in reply or "result" not in reply:
        raise ValueError("app-tools request rejected: %s" % str(reply.get("error"))[:200])
    return reply["result"]


def validate_catalog(result):
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise Unavailable("invalid app-tools catalog")
    tools = [t for t in result["tools"] if isinstance(t, dict)
             and t.get("namespace") == "codex_app" and t.get("name") == "send_message_to_thread"]
    if len(tools) != 1:
        raise Unavailable("callback route is absent or ambiguous")
    schema = tools[0].get("inputSchema", {})
    if not isinstance(schema, dict):
        raise Unavailable("invalid callback schema")
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if (not isinstance(props, dict) or not isinstance(required, list)
            or any(not isinstance(k, str) for k in required)
            or set(required) != {"threadId", "prompt"}
            or any(not isinstance(props.get(k), dict) or props[k].get("type") != "string"
                   for k in ("threadId", "prompt"))):
        raise Unavailable("callback schema changed")


def validate_delivery(result, thread):
    """The tested route acknowledges the explicit destination in text JSON."""
    if (not isinstance(result, dict) or result.get("success") is not True
            or not isinstance(result.get("contentItems"), list)):
        raise Uncertain("Desktop did not acknowledge callback delivery")
    for item in result["contentItems"]:
        if isinstance(item, dict) and item.get("type") == "inputText" and isinstance(item.get("text"), str):
            try:
                ack = json.loads(item["text"])
            except ValueError:
                continue
            if isinstance(ack, dict) and ack.get("threadId") == thread:
                return
    raise Uncertain("Desktop did not acknowledge the intended destination")


@contextmanager
def endpoint():
    app = desktop_ancestor()
    explicit = os.environ.get("CLAUDE_PAIR_CODEX_PIPE") or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    paths = [explicit] if explicit else ["\\\\.\\pipe\\" + n for n in os.listdir("\\\\.\\pipe\\")
                                       if PIPE_PATTERN.fullmatch("\\\\.\\pipe\\" + n)]
    if len(paths) > 32:
        raise Unavailable("too many app-tools candidates to identify safely")
    valid = []
    try:
        for path in paths:
            connection = None
            try:
                connection = Connection(path, app, timeout=1.0)
                validate_catalog(connection.request("tools/list", {"threadStartKind": "all"}))
                valid.append(connection)
            except (Unavailable, OSError):
                if connection:
                    connection.close()
            except Exception:
                if connection:
                    connection.close()
                raise
        if len(valid) != 1:
            raise Unavailable("no unique verified app-tools endpoint (%d found)" % len(valid))
        valid[0].deadline = time.monotonic() + 8.0
        yield valid[0]
    finally:
        for connection in valid:
            connection.close()


def send(thread, turn, marker, prompt):
    with endpoint() as connection:
        result = connection.request("tools/call", {
            "callerSource": "codex", "hostId": "local", "namespace": "codex_app",
            "tool": "send_message_to_thread", "threadId": thread, "turnId": turn,
            "callId": marker, "arguments": {"threadId": thread, "prompt": prompt},
        }, delivery=True)
        validate_delivery(result, thread)
