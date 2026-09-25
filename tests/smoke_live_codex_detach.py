"""Live check: a Codex turn survives the death of the MCP process that launched
it, and its result is recovered exactly once (v0.16.0; costs a little Codex
usage — two short gpt-6-luna turns at low effort).

    python tests/smoke_live_codex_detach.py

A stand-in supervisor process starts a real send through the server code,
this script kills ONLY that supervisor (by its PID) once the turn is running
a command, then checks: pair_poll shows DETACHED, the result is recovered
once ("recovered after owner death", turn counted), and a following send
resumes the thread. Uses the real Codex home with --ignore-user-config (like
the other live harnesses) and deletes the thread it created; the Claude
side runs in a temp CLAUDE_HOME. Also reports any VISIBLE console window
under the detached child (Windows).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def visible_windows_under(root_pid):
    """Visible top-level windows owned by root_pid or any descendant (Windows)."""
    import ctypes
    from ctypes import wintypes
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"],
                         capture_output=True, text=True, timeout=60).stdout
    parent, names = {}, {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            parent[int(parts[0])] = int(parts[1]); names[int(parts[0])] = parts[2]
    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in tree and pid not in tree:
                tree.add(pid); changed = True
    found = []
    user32 = ctypes.windll.user32
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in tree and user32.IsWindowVisible(hwnd):
            found.append((pid.value, names.get(pid.value)))
        return True
    user32.EnumWindows(cb, 0)
    return {"tree": sorted((p, names.get(p)) for p in tree), "visible": found}


def worker(args):
    # The coordinator sets isolation BEFORE spawning/importing this process.
    from claude_squared import async_tasks as A, registry as R, server as S
    spec = R.get_pair("detach-probe")
    prompt = ("Run one PowerShell command that sleeps for 25 seconds, then prints SURVIVED. "
              "After that command completes, reply with exactly: DETACHED-SURVIVED")
    runner = S._build_send_runner(spec.name, prompt, hard_timeout_seconds=180,
                                 override_model=None, override_effort=None,
                                 override_permission_mode=None)
    task = A.start_task(spec.name, prompt, runner, queued=True)
    Path(args.artifacts, "task-id.txt").write_text(task.task_id, encoding="utf-8")
    while A.load_task(task.task_id).status == "running":
        time.sleep(.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-exe")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--artifacts")
    args = parser.parse_args()
    if not args.codex_exe:
        # Resolve in a CHILD with a throwaway CLAUDE_HOME: importing the
        # package runs async_tasks' import-time orphan sweep, which must
        # never touch the real ~/.claude/pairs from a test.
        scratch = tempfile.mkdtemp(prefix="cs-live-resolve-")
        args.codex_exe = subprocess.run(
            [sys.executable, "-c", "from claude_squared.adapters.codex import codex_executable; "
                                   "print(codex_executable())"],
            capture_output=True, text=True, check=True, timeout=60,
            env=dict(os.environ, CLAUDE_HOME=scratch, PYTHONPATH=str(ROOT / "src")),
        ).stdout.strip()
    codex_dir = Path(args.codex_home).resolve()
    if not codex_dir.is_dir() or not Path(args.codex_exe).is_file():
        parser.error("Codex executable/home must exist")
    if not args.artifacts:
        args.artifacts = tempfile.mkdtemp(prefix="cs-live-detach-")
    artifacts = Path(args.artifacts).resolve()
    os.environ.update({"CLAUDE_HOME": str(artifacts / "claude"), "CODEX_HOME": str(codex_dir),
                       "CLAUDE_PAIR_CODEX_PATH": args.codex_exe,
                       "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    sys.path.insert(0, str(ROOT / "src"))
    if args.worker:
        worker(args)
        return
    from claude_squared import async_tasks as A, registry as R, server as S
    from claude_squared.adapters.codex import CodexAdapter
    from claude_squared.models import PairSpec

    print(f"Artifacts: {artifacts}", flush=True)
    spec = PairSpec(name="detach-probe", backend="codex", session_id="new",
                    model=args.model, effort="low", permission_mode="workspace", cwd=str(artifacts))
    created = CodexAdapter().create(spec)
    spec.session_id = created.session_id
    R.add_pair(spec)
    argv = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
            "--codex-exe", args.codex_exe, "--codex-home", str(codex_dir),
            "--model", args.model, "--artifacts", str(artifacts)]
    with open(artifacts / "supervisor.log", "wb") as log:
        proc = subprocess.Popen(argv, env=os.environ.copy(), stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 120
        state = None
        while time.monotonic() < deadline:
            task_file = artifacts / "task-id.txt"
            if task_file.exists():
                state = A.load_task(task_file.read_text().strip())
            if state and state.events_path:
                events = Path(state.events_path).read_text(encoding="utf-8", errors="replace")
                if any('"command_execution"' in line and '"item.started"' in line for line in events.splitlines()):
                    break
            if proc.poll() is not None:
                raise RuntimeError("supervisor exited before command start; inspect supervisor.log")
            time.sleep(.2)
        else:
            raise RuntimeError("no command-start event within 120s")
        print(f"Killing ONLY spawned supervisor PID {proc.pid}; child PID {state.child_pid}", flush=True)
        proc.kill()  # no /T: the point is to leave this probe's Codex child alive
        proc.wait(timeout=10)
        time.sleep(3)
        print("VISIBLE WINDOWS under codex child:", visible_windows_under(state.child_pid), flush=True)
        poll = getattr(S.pair_poll, "fn", S.pair_poll)
        print(poll(state.task_id), flush=True)
        assert A.load_task(state.task_id).status == "running", "expected detached child"
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = A.reap_orphan(state.task_id)
            if result.status != "running":
                break
            time.sleep(1)
        else:
            raise RuntimeError("detached child did not finish; probe artifacts retained")
        print(poll(state.task_id), flush=True)
        assert result.status == "done" and result.recovered_at
        assert "DETACHED-SURVIVED" in result.result.response
        assert R.get_pair(spec.name).turn_count == 1
        runner = S._build_send_runner(spec.name, "Reply exactly: RESUMED", hard_timeout_seconds=120,
                                     override_model=None, override_effort=None, override_permission_mode=None)
        resumed = A.start_task(spec.name, "resume probe", runner, queued=True)
        resumed = A.wait_for_task(resumed.task_id, 150)
        assert resumed.status == "done" and not resumed.result.safety_signal, resumed
        assert "RESUMED" in resumed.result.response
        print("PASS: child survived, result recovered once, subsequent send resumed.", flush=True)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        try:
            subprocess.run([args.codex_exe, "delete", "--force", spec.session_id], capture_output=True, timeout=60)
            print("deleted probe thread", spec.session_id, flush=True)
        except Exception as exc:
            print("thread cleanup failed:", exc, flush=True)
        # Never stop any other pair. On a failure, retained artifacts identify
        # this probe's child for the main session's explicit cleanup decision.


if __name__ == "__main__":
    main()
