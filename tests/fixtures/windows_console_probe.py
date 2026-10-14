"""Positive-only synthetic console descendants on an unshown desktop.

Every root is SW_HIDE or CREATE_NO_WINDOW. A separate desktop does NOT reliably
contain Windows Terminal delegation, so there are no visible negative controls
here. Do not add/show such controls without independently verified containment.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from hidden_desktop import desktop_name


def snapshot() -> dict:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    window = kernel.GetConsoleWindow()
    name = ctypes.create_unicode_buffer(128)
    user.GetClassNameW(window, name, len(name))
    return {"pid": os.getpid(), "console": int(window or 0),
            "class": name.value, "style": user.GetWindowLongW(window, -16) if window else 0,
            "visible": bool(window and user.IsWindowVisible(window)), "desktop": desktop_name()}


def command(*args) -> list[str]:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return [str(executable), "-B", str(Path(__file__).resolve()), *map(str, args)]


def inspect_window(window: int) -> dict:
    """Inspect from Default, where a delegated console host may own its HWND."""
    from hidden_desktop import desktop_name as current_desktop
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    name = ctypes.create_unicode_buffer(128)
    class_length = user.GetClassNameW(window, name, len(name))
    owner = wintypes.DWORD()
    thread = user.GetWindowThreadProcessId(window, ctypes.byref(owner))
    desktop = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    handle = user.GetThreadDesktop(thread) if thread else None
    got_desktop = handle and user.GetUserObjectInformationW(
        handle, 2, desktop, ctypes.sizeof(desktop), ctypes.byref(needed))
    return {"hwnd": window, "valid": bool(user.IsWindow(window)), "class": name.value,
            "queryable": bool(class_length and thread and got_desktop), "owner_pid": owner.value,
            "desktop": desktop.value, "observer_desktop": current_desktop(),
            "visible": bool(user.IsWindowVisible(window)), "style": user.GetWindowLongW(window, -16)}


def default_single(report: Path) -> None:
    """One bounded hidden root; abort on invalid/pseudo/delegated HWND."""
    from datetime import datetime, timezone
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from claude_squared.adapters import codex as codex
    from claude_squared import process_identity as identity
    from claude_squared.process_spawn import hidden_process_kwargs
    evidence = {"started_utc": datetime.now(timezone.utc).isoformat(), "supervisor_pid": os.getpid()}
    # Validate the observation APIs against an existing visible top-level
    # window. Do not collect its title/text or manipulate it.
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    visible_control = []
    def collect(window, _):
        result = inspect_window(int(window))
        if result["visible"] and result["queryable"] and result["valid"]:
            visible_control.append(True)
        return True
    callback = callback_type(collect)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    assert user.EnumWindows(callback, 0) and visible_control, "visibility APIs lack a positive control"
    evidence["existing_visible_control"] = True
    root = subprocess.Popen(command("node", 0, 1), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **hidden_process_kwargs())
    try:
        evidence["root_pid"] = root.pid
        report.write_text(json.dumps(evidence), encoding="utf-8")
        root.stdin.write(b"default-single")
        root.stdin.close()
        row = json.loads(root.stdout.readline())
        observed = inspect_window(row["console"])
        observed["owner_started_at"] = identity.started_at(observed["owner_pid"])
        evidence["root"] = row
        evidence["window"] = observed
        assert observed["valid"] and observed["queryable"], "INCONCLUSIVE: unqueryable console HWND"
        assert observed["class"] == "ConsoleWindowClass", "INCONCLUSIVE: nonclassic/pseudoconsole HWND"
        assert observed["owner_started_at"], "INCONCLUSIVE: unknown console-host identity"
        assert not observed["visible"] and not observed["style"] & 0x10000000, "visible owned console"
        evidence["result"] = "hidden-classic-console"
    finally:
        codex._tree_kill(root)
        root.stdout.close()
        root.stderr.close()
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def node(depth: int, hold: bool) -> None:
    message = sys.stdin.read()
    time.sleep(.1)  # let console initialization settle before measuring
    row = snapshot()
    row["input"] = message
    print(json.dumps(row), flush=True)
    print("stderr-canary", file=sys.stderr, flush=True)
    if depth:
        # Deliberately plain child, with pipes: the production root's console
        # must be inherited without cooperation from its descendants.
        child = subprocess.Popen(command("node", depth - 1, int(hold)), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if hold:
                child.stdin.write(message.encode())
                child.stdin.close()
                for _ in range(depth):
                    print(child.stdout.readline().decode().strip(), flush=True)
                time.sleep(30)
            else:
                out, err = child.communicate(message.encode(), timeout=10)
                sys.stdout.buffer.write(out)
                sys.stderr.buffer.write(err)
                assert child.returncode == 0
        finally:
            # Normal runs wait; interrupted roots are cleaned via taskkill /T.
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    elif hold:
        time.sleep(30)


def supervisor(directory: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from claude_squared.adapters.codex import popen_codex, _tree_kill
    from claude_squared import process_identity as identity
    child = None
    try:
        with (directory / "stdin").open("rb") as inp, (directory / "stdout").open("wb") as out, \
                (directory / "stderr").open("wb") as err:
            child = popen_codex(command("survivor", directory), stdin=inp, stdout=out, stderr=err)
        (directory / "child.json").write_text(json.dumps({
            "pid": child.pid, "started_at": identity.started_at(child.pid, handle=child._handle)}))
    except BaseException:
        # A failed identity publication must not leave an untracked survivor.
        if child is not None:
            _tree_kill(child)
        raise
    time.sleep(30)


def survivor(directory: Path) -> None:
    row = snapshot()
    row["input"] = sys.stdin.read()
    (directory / "ready").write_text(json.dumps(row))
    while not (directory / "continue").exists():
        time.sleep(.05)
    # Launch another console child AFTER the supervisor exits.
    node(1, False)
    print("survived-owner-death", flush=True)


def suite(report: Path | None = None) -> dict:
    from datetime import datetime, timezone
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from claude_squared.adapters import codex as codex
    from claude_squared.process_spawn import hidden_process_kwargs
    from claude_squared import process_identity as identity

    evidence = {"started_utc": datetime.now(timezone.utc).isoformat(), "supervisor_pid": os.getpid()}
    def checkpoint():
        if report is not None:
            report.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    def assert_hidden(row):
        if desktop_name() == "Default":
            observed = inspect_window(row["console"])
            assert observed["valid"] and observed["queryable"], observed
            assert observed["class"] == "ConsoleWindowClass", observed
            assert not observed["visible"] and not observed["style"] & 0x10000000, observed
    for policy, options in (("hidden_console", hidden_process_kwargs()),):
        result = subprocess.run(command("node", 2, 0), input=b"stdin-canary",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, **options)
        assert result.returncode == 0, result.stderr
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(rows) == 3 and all(row["input"] == "stdin-canary" for row in rows)
        assert all(row["desktop"] == desktop_name() for row in rows)
        assert result.stderr.count(b"stderr-canary") == 3
        assert rows[0]["console"] and len({row["console"] for row in rows}) == 1, rows
        # These accessors can be invalid across desktops. Console identity and
        # sharing are evidence here; visibility needs independent observation.
        evidence[policy] = rows
        for row in rows:
            # The process has exited, so inspect its recorded in-process
            # snapshot instead of a potentially recycled HWND after wait.
            if desktop_name() == "Default":
                assert row["class"] == "ConsoleWindowClass" and not row["visible"], row
                assert not row["style"] & 0x10000000, row
        checkpoint()

    # Short leaf launches never receive a console, even under the hidden root.
    evidence["leaf_pids"] = []
    for _ in range(5):
        leaf = subprocess.run(command("node", 0, 0), input=b"leaf", capture_output=True,
                              timeout=10, **hidden_process_kwargs(console=False))
        row = json.loads(leaf.stdout)
        assert leaf.returncode == 0 and row["console"] == 0 and not row["visible"], row
        evidence["leaf_pids"].append(row["pid"])
    evidence["leaf_launches"] = 5

    # Exercise the production kill implementation with live piped descendants.
    root = codex.popen_codex(command("node", 2, 1), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rows = []
    try:
        root.stdin.write(b"kill-canary")
        root.stdin.close()
        for _ in range(3):
            rows.append(json.loads(root.stdout.readline()))
        evidence["tree_kill_rows"] = rows
        checkpoint()
        for row in rows:
            assert_hidden(row)
        starts = {row["pid"]: identity.started_at(row["pid"]) for row in rows}
        assert all(starts.values()), starts
        assert codex._tree_kill(root) == "tree-killed"
        for pid, started in starts.items():
            with identity.matching_process(pid, started) as alive:
                assert not alive, f"owned descendant {pid} survived taskkill /T"
        evidence["tree_kill"] = len(rows)
    finally:
        if root.poll() is None:
            codex._tree_kill(root)
        root.stdout.close()
        root.stderr.close()

    with tempfile.TemporaryDirectory(prefix="cs-console-survival-") as temporary:
        directory = Path(temporary)
        (directory / "stdin").write_text("file-canary")
        owner = subprocess.Popen(command("supervisor", directory), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 **hidden_process_kwargs())
        child = None
        try:
            deadline = time.monotonic() + 15
            while not ((directory / "ready").exists() and (directory / "child.json").exists()):
                assert owner.poll() is None, owner.stderr.read()
                assert time.monotonic() < deadline, "survivor did not start"
                time.sleep(.05)
            child = json.loads((directory / "child.json").read_text())
            before = json.loads((directory / "ready").read_text())
            evidence["file_owner_pid"] = owner.pid
            evidence["file_child"] = child
            evidence["file_before"] = before
            checkpoint()
            assert_hidden(before)
            assert before["input"] == "file-canary" and before["console"] and not before["visible"]
            # Terminate only the owned supervisor, not its tree.
            owner.kill()
            owner.wait(timeout=5)
            with identity.matching_process(child["pid"], child["started_at"]) as alive:
                assert alive, "file-backed child died with its supervisor"
            (directory / "continue").touch()
            while "survived-owner-death" not in (directory / "stdout").read_text():
                assert time.monotonic() < deadline, "survivor did not finish"
                time.sleep(.05)
            after = [json.loads(line) for line in (directory / "stdout").read_text().splitlines()
                     if line.startswith("{")]
            assert len(after) == 2 and all(row["console"] == before["console"] for row in after)
            assert not any(row["visible"] for row in after)
            evidence["file_after"] = after
            assert (directory / "stderr").read_text().count("stderr-canary") == 2
            while True:
                with identity.matching_process(child["pid"], child["started_at"]) as alive:
                    if not alive:
                        break
                assert time.monotonic() < deadline, "survivor did not exit"
                time.sleep(.05)
            evidence["file_backed_survival"] = True
        finally:
            if owner.poll() is None:
                codex._tree_kill(owner)
            owner.stderr.close()
            if child:
                with identity.matching_process(child["pid"], child["started_at"]) as alive:
                    if alive:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child["pid"])],
                                       capture_output=True, timeout=10,
                                       **hidden_process_kwargs(console=False))
    evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return evidence


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Windows only")
    if not desktop_name().startswith("cs-hidden-") and sys.argv[1] not in ("default-single", "default-extension") \
            and os.environ.get("CS_DEFAULT_POSITIVE_CHILD") != "1":
        raise SystemExit("Refusing console probes outside an isolated cs-hidden desktop")
    mode = sys.argv[1]
    if mode == "node":
        node(int(sys.argv[2]), bool(int(sys.argv[3])))
    elif mode == "supervisor":
        supervisor(Path(sys.argv[2]))
    elif mode == "survivor":
        survivor(Path(sys.argv[2]))
    elif mode == "suite":
        # Import-time orphan recovery is confined before any package imports.
        with tempfile.TemporaryDirectory(prefix="cs-console-home-") as home:
            os.environ["CLAUDE_HOME"] = str(Path(home) / "claude")
            os.environ["CODEX_HOME"] = str(Path(home) / "codex")
            print(json.dumps(suite()), flush=True)
    elif mode in ("default-single", "default-extension"):
        if desktop_name() != "Default" or Path(sys.executable).name.lower() != "pythonw.exe":
            raise SystemExit("Use a hidden pythonw supervisor on Default")
        os.environ["CS_DEFAULT_POSITIVE_CHILD"] = "1"
        with tempfile.TemporaryDirectory(prefix="cs-default-console-home-") as home:
            os.environ["CLAUDE_HOME"] = str(Path(home) / "claude")
            os.environ["CODEX_HOME"] = str(Path(home) / "codex")
            report = Path(sys.argv[2])
            try:
                if mode == "default-single":
                    default_single(report)
                else:
                    suite(report)
            except Exception as exc:
                evidence = json.loads(report.read_text()) if report.exists() else {}
                evidence["error"] = f"{type(exc).__name__}: {exc}"
                report.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
                raise
    else:
        raise SystemExit("Unknown probe mode")
