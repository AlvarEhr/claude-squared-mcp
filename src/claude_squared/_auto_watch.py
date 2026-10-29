#!/usr/bin/env python3
"""PostToolUse asyncRewake hook for claude-squared (stdlib only).

Installed beside wait.py; no package import or model call is needed. The hook
settings own the outer timeout. Keep it above HOOK_TIMEOUT_SECONDS (README).
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import runpy
import sys
import threading
import time
from types import SimpleNamespace
import uuid


HOOK_TIMEOUT_SECONDS = 7 * 24 * 3600
WAIT_TIMEOUT_SECONDS = HOOK_TIMEOUT_SECONDS - 60
PAIR_ENV = "CLAUDE_SQUARED_PAIR"
TOOLS = ("pair_send", "pair_send_async", "pair_compact", "pair_create", "pair_handoff")
TOOL_PATTERN = r"^mcp__(?:pair|Claude_Squared)__(?:" + "|".join(TOOLS) + r"|pair_poll)$"
START_SOURCES = ("startup", "resume", "clear", "compact", "fork")
_RECOVERY_LOAD_LOCK = threading.Lock()


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _registry_entries(pairs):
    for attempt in range(3):
        registry = _read(pairs / "registry.json")
        entries = registry.get("pairs") if isinstance(registry, dict) else None
        if isinstance(entries, dict):
            return entries
        if attempt < 2:
            time.sleep(.05)
    return None


def _expired_record(path, cleanup=False):
    try:
        expired = path.stat().st_mtime < time.time() - HOOK_TIMEOUT_SECONDS
    except OSError:
        return False
    if expired and cleanup:
        gate = None
        try:
            directory = path.parent.parent / "auto_watch"
            directory.mkdir(parents=True, exist_ok=True)
            gate = _Lock(directory / (path.stem + ".gate"))
            if gate.acquire():
                # A sender may have replaced this path since the stat-only
                # scan. Serialize cleanup with publication and recheck mtime.
                expired = path.stat().st_mtime < time.time() - HOOK_TIMEOUT_SECONDS
                if expired:
                    path.unlink()
                    path.with_suffix(".done").unlink()
        except OSError:
            pass
        finally:
            if gate is not None:
                gate.close()
    return expired


class _HostGone(BaseException):
    pass


def _process_parent(pid):
    """Process identity metadata only; no command lines or process control."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                        ("exe", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            present = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while present:
                if entry.pid == pid:
                    return entry.parent, entry.exe
                present = kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(handle)
    elif sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            return int(text[text.rfind(")") + 2:].split()[1]), Path(f"/proc/{pid}/comm").read_text().strip()
        except (OSError, ValueError, IndexError):
            pass
    else:
        import subprocess
        try:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "comm="],
                                    capture_output=True, text=True, timeout=2)
            parent, executable = result.stdout.strip().split(None, 1)
            return int(parent), Path(executable).name
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return None


def _host_identity(reader):
    pid = os.getppid()
    seen = set()
    for _ in range(16):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        info = _process_parent(pid)
        if info is None:
            break
        parent, executable = info
        if executable.lower() in ("claude", "claude.exe"):
            alive, born = reader["_av_proc"](pid)
            if alive is True and born is not None:
                return {"host_pid": pid, "host_started_at": born}
            break
        pid = parent
    return {}


def _host_dead(record, reader):
    pid, expected = record.get("host_pid"), record.get("host_started_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(expected, (int, float)):
        return False  # old records and unidentifiable ancestry retain PID-only recovery
    alive, born = reader["_av_proc"](pid)
    return alive is False or (born is not None and abs(born - expected) > .01)


def _host_check(host, reader):
    if host and _host_dead(host, reader):
        raise _HostGone()


def _without_reply(text):
    # A handoff embeds the target's formatted reply after "Probe reply:",
    # unlike pair_send's top-level reply. Strip either form before looking
    # for server-authored handles (quench round 2, 2026-10-26).
    header = re.search(r"(?m)^━━━ pair '([^\r\n]+)' replied ━━━\r?$", text)
    if header is None:
        return text
    footer = text.rfind(f"\n[{header[1]}: ", header.end())
    return text[:header.start()] + (text[footer:] if footer >= 0 else "")


def _responses(value):
    """Unwrap MCP content blocks, FastMCP envelopes and verbose JSON strings.

    Do not recurse into arbitrary result fields: the pair's response itself
    can quote tool results, which must not arm another watcher.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            yield value
        else:
            if isinstance(decoded, (dict, list)):
                yield from _responses(decoded)
            else:
                yield value
    elif isinstance(value, list):
        for item in value:
            yield from _responses(item)
    elif isinstance(value, dict):
        if value.get("isError") or value.get("is_error"):
            return
        yield value
        for key in ("content", "text", "result", "spec", "transparency_msgs"):
            if key in value:
                yield from _responses(value[key])


def _target(payload, pairs):
    if not isinstance(payload, dict) or os.environ.get(PAIR_ENV):
        return None
    if payload.get("hook_event_name") != "PostToolUse":
        return None
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not re.fullmatch(TOOL_PATTERN, tool):
        return None
    if tool.endswith("__pair_poll"):
        return None  # recovery only; never interpret a polled reply as a new send
    session = payload.get("session_id")
    inputs = payload.get("tool_input")
    if not isinstance(session, str) or not session or not isinstance(inputs, dict):
        return None
    name = inputs.get("name")
    # Pair names become wait.py arguments and activity paths. Refuse traversal
    # even if a damaged registry contains it; never interpolate a shell command.
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(c in name for c in "/\\\0") or name.startswith("-")):
        return None
    pending = False
    task_id = None
    for response in _responses(payload.get("tool_response")):
        if isinstance(response, str):
            response = _without_reply(response)
            handle = re.search(r"(?m)^Async task: ([0-9a-f-]+)\s*$", response)
            if handle:
                task_id = handle[1]
            if handle or re.search(r"(?m)^⏳ BACKGROUND WORK LAUNCHED \(", response):
                pending = True
        elif response.get("background_launches"):
            pending = True
    if not pending:
        return None
    if tool.endswith("__pair_handoff"):
        # Handoff's input names the SOURCE; the queued briefing belongs to
        # its newly allocated target (whose name may have a collision suffix).
        task = _read(pairs / "async" / (str(task_id) + ".json"))
        name = task.get("pair_name") if isinstance(task, dict) else None
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(c in name for c in "/\\\0") or name.startswith("-")):
            raise OSError("could not resolve the handoff target from its task handle")
    # Windows readers can briefly miss an atomic registry replacement. A
    # handle already promised a watcher: fail visibly if the retry fails too.
    entries = _registry_entries(pairs)
    if not isinstance(entries, dict):
        raise OSError("could not read the pair registry; use pair_poll and arm wait.py manually")
    # Older pair runtimes predate PAIR_ENV, but still have registered sessions.
    if any(isinstance(p, dict) and p.get("session_id") == session for p in entries.values()):
        return None
    if name not in entries:
        raise OSError(f"pair '{name}' is no longer in the registry (it may have been forgotten "
                      "after the send); no watcher was started")
    return session, name


class _Lock:
    """Kernel-owned lock: process death releases it, with no PID-reuse guess.

    Keep the inode forever. Unlinking after unlock lets two processes lock
    different files at the same path (the classic lock-file removal race).
    """

    def __init__(self, path):
        self.file = os.fdopen(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        self.held = False

    def acquire(self, timeout=0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)

    def release(self):
        if self.held:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.held = False

    def close(self):
        self.release()
        self.file.close()

    def revision(self, value=None):
        self.file.seek(1)
        if value is not None:
            self.file.write(value)
            self.file.truncate()
            self.file.flush()
        else:
            return self.file.read()


def _wait(pairs, name, timeout, host=None, reader=None):
    # Run in THIS process, not a child: a hook killed by its host must not
    # leave an orphan wait.py holding the dedupe lock for the next seven days.
    pairs = pairs.resolve()
    script = pairs / "wait.py"
    output = io.StringIO()
    argv = sys.argv
    home = os.environ.get("CLAUDE_HOME")
    try:
        # The hook can discover a custom home from its installed path while
        # only the MCP server inherited CLAUDE_HOME. wait.py must read that
        # same home's registry/activity, not a same-named default-home pair
        # (quench review, 2026-10-26).
        os.environ["CLAUDE_HOME"] = str(pairs.parent)
        sys.argv = [str(script), name, "--timeout", str(max(0, timeout))]
        with redirect_stdout(output), redirect_stderr(output):
            watcher = runpy.run_path(str(script))
            if host:
                def sleep(seconds):
                    until = time.monotonic() + seconds
                    while time.monotonic() < until:
                        _host_check(host, reader)
                        time.sleep(min(.25, max(0, until - time.monotonic())))
                watcher["main"].__globals__["time"] = SimpleNamespace(**{**vars(time), "sleep": sleep})
                _host_check(host, reader)
            code = watcher["main"](sys.argv[1:])
    finally:
        sys.argv = argv
        if home is None:
            os.environ.pop("CLAUDE_HOME", None)
        else:
            os.environ["CLAUDE_HOME"] = home
    return code, output.getvalue()


def _publish_watch(pairs, key, session, name, payload, host=None):
    """Best-effort pane discovery, written only by the dedupe lock's owner.

    Hashing the session/pair also keeps arbitrary session names out of paths.
    The pane checks the PID (and its birth against started_at), so a killed
    hook's leftover record never establishes that a watcher is still alive.
    """
    target = pairs / "watches" / (key + ".json")
    tmp = target.with_name(key + "." + uuid.uuid4().hex + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1, "pair": name, "session_id": session,
            "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            "started_at": datetime.now(timezone.utc).isoformat(), "hook_pid": os.getpid(),
            **(host or {}),
        }
        for attempt in range(3):
            try:
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(target)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.05)
        return target, data
    except OSError:
        return None  # a display failure must not lose the completion wake
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _watch_key(session, name):
    return hashlib.sha256(json.dumps([session, name]).encode("utf-8")).hexdigest()


def _completed_record(path, record):
    """A separate receipt survives a Windows handle that denies watch-file deletion."""
    done = _read(path.with_suffix(".done"))
    return (isinstance(done, dict) and done.get("state") == "done"
            and done.get("hook_pid") == record.get("hook_pid")
            and done.get("started_at") == record.get("started_at"))


def _retire_watch(path, record):
    # Write completion BEFORE attempting unlink. Replacing the watch itself
    # also fails when a Windows reader denies delete sharing (quench, 2026-10-26).
    receipt = path.with_suffix(".done")
    tmp = receipt.with_name(receipt.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump({"state": "done", "hook_pid": record.get("hook_pid"),
                       "started_at": record.get("started_at")}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                tmp.replace(receipt)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(.02 * (attempt + 1))
        try:
            path.unlink()
        except OSError:
            return  # the receipt prevents recovery even after this PID exits
        try:
            receipt.unlink()
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _dead_record(record, reader, path):
    if not isinstance(record, dict) or record.get("version") != 1:
        return False
    if record.get("state") == "done" or _completed_record(path, record):
        return False
    started = reader["_av_parse"](record.get("started_at"))
    pid = record.get("hook_pid")
    return (started is not None and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and (not reader["_av_task_owner_alive"](pid, started) or _host_dead(record, reader)))


class _RecoveryCancelled(BaseException):
    pass


def _recovery_wait(pairs, name, timeout, cancel, host=None, reader=None):
    """A separate wait.py namespace per recovered pair, within this hook PID.

    No child watcher survives a killed hook. Thread-local output, argv and
    home avoid redirect_stdout/os.environ races between concurrent waits.
    Cancellation interrupts only these readers; it never stops pair work.
    """
    script = pairs.resolve() / "wait.py"
    with _RECOVERY_LOAD_LOCK:
        watcher = runpy.run_path(str(script))
    namespace = watcher["main"].__globals__
    output = io.StringIO()
    argv = [str(script), name, "--timeout", str(max(0, timeout))]

    def sleep(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if cancel.wait(min(.25, max(0, until - time.monotonic()))):
                raise _RecoveryCancelled()
            _host_check(host, reader)

    def emit(*args, **kwargs):
        kwargs["file"] = output
        print(*args, **kwargs)

    # runpy loads each namespace before it runs a wait. Its short argv swap
    # does not affect waits already running: those use their own sys proxy.
    namespace.update({
        "print": emit,
        "sys": SimpleNamespace(**{**vars(sys), "argv": argv, "stdout": output, "stderr": output}),
        "os": SimpleNamespace(**{**vars(os), "environ": {**os.environ, "CLAUDE_HOME": str(script.parent.parent)}}),
        "time": SimpleNamespace(**{**vars(time), "sleep": sleep}),
    })
    if cancel.is_set():
        raise _RecoveryCancelled()
    _host_check(host, reader)
    return watcher["main"](argv[1:]), output.getvalue()


def watch(payload, pairs, timeout=WAIT_TIMEOUT_SECONDS, *, recover=None, reader=None, cancel=None, output=None):
    deadline = time.monotonic() + timeout
    name = "(unresolved)"
    gate = owner = None
    record = None
    record_data = None
    finished = False
    output = output if output is not None else sys.stderr
    try:
        target = (recover["session_id"], recover["pair"]) if recover else _target(payload, pairs)
        if target is None:
            return 0
        session, name = target
        if reader is None:
            reader = runpy.run_path(str(pairs / "wait.py")) if (pairs / "wait.py").is_file() else {}
        try:
            host = _host_identity(reader) if "_av_proc" in reader else {}
        except Exception:
            host = {}  # process metadata can be unavailable on restricted hosts
        _host_check(host, reader)
        directory = pairs / "auto_watch"
        directory.mkdir(parents=True, exist_ok=True)
        key = _watch_key(session, name)
        gate = _Lock(directory / (key + ".gate"))
        owner = _Lock(directory / (key + ".lock"))
        if not gate.acquire(5):
            raise OSError("could not acquire watcher coordination lock")
        if recover:
            # Recheck under the same gate used by PostToolUse. Another restart
            # or send may already have taken ownership since the initial scan.
            previous = pairs / "watches" / (key + ".json")
            if _expired_record(previous, cleanup=True):
                return 0
            current = _read(previous)
            if current != recover or not _dead_record(current, reader, previous):
                return 0
        if not owner.acquire(3 if recover and _host_dead(recover, reader) else 0):
            if recover and _host_dead(recover, reader):
                raise OSError("the old hook's host is gone but its owner lock has not released; "
                              "recovery record retained for retry")
            if not recover:
                gate.revision(uuid.uuid4().hex.encode("ascii"))
            return 0
        revision = uuid.uuid4().hex.encode("ascii")
        gate.revision(revision)
        if recover:
            entries = _registry_entries(pairs)
            if not isinstance(entries, dict):
                raise OSError("registry unavailable; recovery record retained for retry")
            if name not in entries:
                record = previous
                record_data = recover
                finished = True
                print(f"claude-squared auto-watch recovery: pair '{name}' is gone from the registry "
                      "(possibly forgotten while the session was down); clearing its stale watch record.", file=output)
                return 2
        published = _publish_watch(pairs, key, session, name, payload, host)
        if published:
            record, record_data = published
        elif recover:
            record, record_data = previous, recover
        gate.release()
        while True:
            code, message = (_recovery_wait(pairs, name, deadline - time.monotonic(), cancel, host, reader)
                             if recover else _wait(pairs, name, deadline - time.monotonic(), host, reader))
            _host_check(host, reader)
            if not gate.acquire(5):
                raise OSError("could not finish watcher coordination")
            # A send can land between wait.py's final idle read and this hook's
            # exit. Its hook records a revision under the short gate lock; the
            # owner checks again before relinquishing ownership. Errors/alerts
            # always wake immediately, even when another send arrived.
            current = gate.revision()
            if code == 0 and current != revision and time.monotonic() < deadline:
                revision = current
                gate.release()
                continue
            print(f"claude-squared auto-watch: pair '{name}', wait.py exit {code}\n{message}", file=output)
            if code == 3:
                print("Watcher timed out; the work was NOT stopped. Re-arm using the wait.py "
                      "command above in background Bash.", file=output)
            finished = True
            return 2
    except _HostGone:
        return 0  # retain the record, release the owner, and never wake a new host
    except Exception as exc:
        target_hint = f" for '{name}'" if name != "(unresolved)" else ""
        recovery = (f"Check pair_poll('{name}') and re-arm wait.py manually."
                    if target_hint else "Check the original tool response and current pair registry.")
        print(f"claude-squared auto-watch failed{target_hint}: {exc}. {recovery}", file=output)
        finished = True
        return 2
    finally:
        # Retire before releasing ownership: another watcher may reuse the path.
        if record is not None and finished:
            try:
                _retire_watch(record, record_data)
            except OSError as exc:
                print(f"Could not persist watch completion: {exc}; a later restart may repeat this outcome.", file=output)
        # Ownership is released BEFORE the gate, so a racing new request either
        # reaches the existing owner above or starts its own watcher.
        if owner is not None:
            owner.close()
        if gate is not None:
            gate.close()


def resume_watches(payload, pairs, timeout=WAIT_TIMEOUT_SECONDS):
    if not isinstance(payload, dict) or os.environ.get(PAIR_ENV):
        return 0
    event = payload.get("hook_event_name")
    poll = payload.get("tool_name")
    if event == "SessionStart":
        if payload.get("source") not in START_SOURCES:
            return 0
    elif not (event == "PostToolUse" and isinstance(poll, str)
              and re.fullmatch(TOOL_PATTERN, poll) and poll.endswith("__pair_poll")):
        return 0
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return 0
    entries = _registry_entries(pairs)
    if entries is None:
        print("claude-squared auto-watch recovery: registry unavailable after three reads; "
              "records retained for retry.", file=sys.stderr)
        return 2
    if isinstance(entries, dict) and any(isinstance(p, dict) and p.get("session_id") == session for p in entries.values()):
        return 0
    candidates = []
    reader = None
    try:
        for path in (pairs / "watches").glob("*.json"):
            if _expired_record(path, cleanup=True):
                continue
            record = _read(path)
            if not isinstance(record, dict) or record.get("session_id") != session:
                continue
            name = record.get("pair")
            if (not isinstance(name, str) or not name or name in (".", "..")
                    or any(c in name for c in "/\\:\0") or name.startswith("-")):
                continue
            if path.name != _watch_key(session, name) + ".json":
                continue
            if reader is None:
                reader = runpy.run_path(str(pairs / "wait.py"))
            if _dead_record(record, reader, path):
                candidates.append(record)
    except Exception as exc:
        print(f"claude-squared auto-watch recovery could not read its state: {exc}. "
              "Records retained; inspect pair_poll and retry recovery.", file=sys.stderr)
        return 2
    if not candidates:
        return 0
    cancel = threading.Event()
    results = queue.Queue()

    def run(record):
        output = io.StringIO()
        code = 2
        unwatched = False
        try:
            code = watch(payload, pairs, timeout, recover=record, reader=reader, cancel=cancel, output=output)
        except _RecoveryCancelled:
            code = 0  # its record remains, to be re-armed by the next pair_poll
            unwatched = True
        except BaseException as exc:
            code = 2
            output.write(f"Recovery watcher failed for '{record['pair']}': {type(exc).__name__}: {str(exc)[:1500]}; inspect pair_poll.\n")
        finally:
            try:
                message = output.getvalue()
            except BaseException:
                message = f"Recovery watcher failed for '{record['pair']}'; output unavailable.\n"
            results.put((record["pair"], code, message, unwatched))

    threads = [threading.Thread(target=run, args=(record,), daemon=True) for record in candidates]
    for thread in threads:
        thread.start()
    remaining = len(threads)
    messages = []
    unwatched_names = set()
    while remaining:
        name, code, message, unwatched = results.get()
        if unwatched:
            unwatched_names.add(name)
        remaining -= 1
        if code == 2:
            messages.append(message)
            break
    cancel.set()
    joined_by = time.monotonic() + 5
    for record, thread in zip(candidates, threads):
        thread.join(timeout=max(0, joined_by - time.monotonic()))
        if thread.is_alive():
            unwatched_names.add(record["pair"])
    while not results.empty():
        name, code, message, unwatched = results.get_nowait()
        if unwatched:
            unwatched_names.add(name)
        if code == 2:
            messages.append(message)
    if messages:
        print("Recovered auto-watch outcome(s):\n" + "\n".join(messages), file=sys.stderr)
        if unwatched_names:
            print("Still-unwatched recovered pairs: " + ", ".join(sorted(unwatched_names)) + ".", file=sys.stderr)
        print("Read the outcome with pair_poll. Any unfinished recovered waits will re-arm "
              "automatically after that poll; do not re-send the original work.", file=sys.stderr)
        return 2
    return 0


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    home = os.environ.get("CLAUDE_HOME")
    pairs = Path(home) / "pairs" if home else Path(__file__).resolve().parent
    if sys.argv[1:] == ["--resume"] or (payload.get("hook_event_name") == "PostToolUse"
            and str(payload.get("tool_name", "")).endswith("__pair_poll")):
        return resume_watches(payload, pairs)
    return watch(payload, pairs)


if __name__ == "__main__":
    sys.exit(main())
