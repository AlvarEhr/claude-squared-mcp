"""Stdlib process creation times for persisted Codex child identities."""

from contextlib import contextmanager
import os
from pathlib import Path
import sys


def _windows_kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    return kernel


def _windows_started(kernel, handle) -> float | None:
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return (ticks - 116444736000000000) / 10_000_000


def started_at(pid: int, *, handle=None) -> float | None:
    """UTC epoch seconds; None means identity is unavailable, never a match."""
    try:
        if sys.platform == "win32":
            kernel = _windows_kernel()
            owned = handle is None
            if owned:
                handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                return _windows_started(kernel, handle)
            finally:
                if owned:
                    kernel.CloseHandle(handle)
        if sys.platform.startswith("linux"):
            # comm (field 2) can contain spaces/parentheses; field 22 follows
            # the LAST closing parenthesis, not whitespace token number 22.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            boot = next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines()
                        if line.startswith("btime "))
            return int(boot) + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return None


def matches(pid: int, expected: float | None) -> bool:
    actual = started_at(pid)
    return expected is not None and actual is not None and abs(actual - expected) < .001


@contextmanager
def matching_process(pid: int, expected: float | None):
    """Pin the Windows process object while a PID-targeted tree kill runs.

    Keeping this handle open prevents reuse between identity check and taskkill
    (2026-09-25: exited Windows process objects remain openable until released).
    """
    if sys.platform == "win32":
        kernel = _windows_kernel()
        handle = kernel.OpenProcess(0x00101000, False, pid)  # query + synchronize
        try:
            actual = _windows_started(kernel, handle) if handle else None
            yield bool(handle and expected is not None and actual is not None
                       and abs(actual - expected) < .001
                       and kernel.WaitForSingleObject(handle, 0) == 258)
        finally:
            if handle:
                kernel.CloseHandle(handle)
    else:
        yield matches(pid, expected)
