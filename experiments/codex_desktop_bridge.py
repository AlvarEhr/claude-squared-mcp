"""Opt-in Windows Desktop bridge experiment. Not imported by the MCP server.

Use --pipe with a known app-tools pipe for a read-only catalog request.
--send-smoke explicitly sends one self-addressed completion to a disposable
test thread. The private protocol is version-specific; see the maintenance doc.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import struct
import sys
import time
import uuid

if sys.platform != "win32":
    raise SystemExit("This prototype requires Windows named pipes.")

k = C.WinDLL("kernel32", use_last_error=True)


class Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", W.DWORD), ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
k.CreateFileW.restype = W.HANDLE
k.CreateEventW.argtypes = [W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR]
k.CreateEventW.restype = W.HANDLE
k.CloseHandle.argtypes = [W.HANDLE]
k.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), C.POINTER(Overlapped)]
k.WriteFile.argtypes = k.ReadFile.argtypes
k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
k.GetOverlappedResult.argtypes = [W.HANDLE, C.POINTER(Overlapped), C.POINTER(W.DWORD), W.BOOL]
k.CancelIoEx.argtypes = [W.HANDLE, C.POINTER(Overlapped)]
k.GetNamedPipeServerProcessId.argtypes = [W.HANDLE, C.POINTER(W.ULONG)]
k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
k.OpenProcess.restype = W.HANDLE
k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]


def io(handle, count, data=None):
    buffer = C.create_string_buffer(data, len(data)) if data is not None else C.create_string_buffer(count)
    ov = Overlapped(hEvent=k.CreateEventW(None, True, False, None))
    n = W.DWORD()
    try:
        success = (k.WriteFile if data is not None else k.ReadFile)(handle, buffer, count, C.byref(n), C.byref(ov))
        if not success and C.get_last_error() != 997:
            raise C.WinError(C.get_last_error())
        if not success:
            if k.WaitForSingleObject(ov.hEvent, 30000) != 0:
                k.CancelIoEx(handle, C.byref(ov))
                k.GetOverlappedResult(handle, C.byref(ov), C.byref(n), True)
                raise TimeoutError("pipe operation timed out")
            if not k.GetOverlappedResult(handle, C.byref(ov), C.byref(n), False):
                raise C.WinError(C.get_last_error())
        return buffer.raw[:n.value]
    finally:
        k.CloseHandle(ov.hEvent)


def read_exact(handle, count):
    result = b""
    while len(result) < count:
        chunk = io(handle, count-len(result))
        if not chunk:
            raise EOFError("pipe closed")
        result += chunk
    return result


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pipe", required=True)
parser.add_argument("--send-smoke", action="store_true")
parser.add_argument("--thread-id")
parser.add_argument("--completed-turn-id")
parser.add_argument("--delay", type=float, default=0)
parser.add_argument("--token", default="DESKTOP_CALLBACK_" + uuid.uuid4().hex[:12])
options = parser.parse_args()
if not options.pipe.startswith("\\\\.\\pipe\\codex-browser-use-"):
    parser.error("--pipe must name an explicitly identified Codex Desktop pipe")
if options.send_smoke and not (options.thread_id and options.completed_turn_id):
    parser.error("--send-smoke requires a disposable --thread-id and --completed-turn-id")
if not 0 <= options.delay <= 120:
    parser.error("--delay must be between 0 and 120 seconds")
time.sleep(options.delay)
for pipe in [options.pipe]:
    handle = k.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle == C.c_void_p(-1).value:
        print(json.dumps({"pipe": pipe, "open_error": C.get_last_error()}), flush=True)
        sys.exit(1)
    try:
        pid = W.ULONG()
        if not k.GetNamedPipeServerProcessId(handle, C.byref(pid)):
            raise C.WinError(C.get_last_error())
        process = k.OpenProcess(0x1000, False, pid.value)
        path = C.create_unicode_buffer(32768)
        size = W.DWORD(len(path))
        try:
            if not process or not k.QueryFullProcessImageNameW(process, 0, path, C.byref(size)):
                raise C.WinError(C.get_last_error())
        finally:
            if process:
                k.CloseHandle(process)
        # Don't send protocol data to unrelated or unidentified applications.
        if "openai.codex" not in path.value.lower() and "\\openai\\codex\\" not in path.value.lower():
            print(json.dumps({"pipe": pipe, "pid": pid.value, "image": path.value, "skipped": True}), flush=True)
            continue
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                   "params": {"threadStartKind": "all"}}
        if options.send_smoke:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "namespace": "codex_app", "tool": "send_message_to_thread",
                "threadId": options.thread_id, "turnId": options.completed_turn_id,
                "callId": "callback-smoke-" + uuid.uuid4().hex,
                "arguments": {"threadId": options.thread_id,
                    "prompt": "Completion callback smoke test. Reply with exactly: " + options.token +
                              ". Do not use tools, access files, or continue earlier tasks."}}}
        payload = json.dumps(request).encode()
        io(handle, len(payload)+4, struct.pack("<I", len(payload))+payload)
        length = struct.unpack("<I", read_exact(handle, 4))[0]
        if length > 4 * 1024 * 1024:
            raise ValueError("oversized response")
        response = json.loads(read_exact(handle, length))
        tools = response.get("result", {}).get("tools", [])
        if options.send_smoke:
            print(json.dumps({"thread_id": options.thread_id, "expected_reply": options.token,
                              "response": response}), flush=True)
            if "error" in response or response.get("result", {}).get("success") is False:
                sys.exit(1)
            continue
        print(json.dumps({"pipe": pipe, "pid": pid.value, "image": path.value,
                          "tools": [{"name": t.get("name"), "namespace": t.get("namespace")} for t in tools],
                          "error": response.get("error")}), flush=True)
    except Exception as error:
        print(json.dumps({"pipe": pipe, "error": str(error)}), flush=True)
        sys.exit(1)
    finally:
        k.CloseHandle(handle)
