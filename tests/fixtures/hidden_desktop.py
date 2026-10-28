"""Run offline Windows probes on a new desktop that is never shown.

Usage: python tests/fixtures/hidden_desktop.py -- <command...>
Only the process launched here is eligible for timeout cancellation. No desktop
switching, window manipulation, real CLI sessions, or image-name kills.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def desktop_name() -> str:
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    handle = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    buffer = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    if not handle or not user.GetUserObjectInformationW(
            handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def run(argv: list[str], *, timeout: int = 120) -> tuple[int, str]:
    if sys.platform != "win32":
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout + result.stderr
    import msvcrt

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.LPCWSTR,
                                     ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "cs-hidden-" + uuid.uuid4().hex
    desktop = user.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with tempfile.TemporaryDirectory(prefix="cs-desktop-") as directory:
            output = Path(directory) / "output.log"
            with output.open("wb") as log, open(os.devnull, "rb") as null:
                out_handle = msvcrt.get_osfhandle(log.fileno())
                in_handle = msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(out_handle, True)
                os.set_handle_inheritable(in_handle, True)
                info = STARTUPINFOW()
                info.cb = ctypes.sizeof(info)
                info.lpDesktop = name
                info.dwFlags = 0x1 | 0x100  # USESHOWWINDOW | USESTDHANDLES
                info.wShowWindow = 0
                info.hStdInput, info.hStdOutput, info.hStdError = in_handle, out_handle, out_handle
                process = PROCESS_INFORMATION()
                command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
                if not kernel.CreateProcessW(None, command, None, None, True, 0x10,
                                             None, None, ctypes.byref(info), ctypes.byref(process)):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    waited = kernel.WaitForSingleObject(process.hProcess, timeout * 1000)
                    if waited == 258:
                        # Retain the original process handle across the exact-PID
                        # tree kill, so the PID cannot be recycled underneath us.
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.dwProcessId)],
                                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                       timeout=10, check=False)
                        kernel.WaitForSingleObject(process.hProcess, 10000)
                        raise TimeoutError(f"owned hidden-desktop PID {process.dwProcessId} timed out")
                    if waited != 0:
                        raise ctypes.WinError(ctypes.get_last_error())
                    code = wintypes.DWORD()
                    if not kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(process.hThread)
                    kernel.CloseHandle(process.hProcess)
            return code.value, output.read_text(encoding="utf-8", errors="replace")
    finally:
        user.CloseDesktop(desktop)


if __name__ == "__main__":
    if sys.argv[1:2] != ["--"] or len(sys.argv) < 3:
        raise SystemExit(__doc__)
    code, output = run(sys.argv[2:], timeout=600)
    print(output, end="")
    raise SystemExit(code)
