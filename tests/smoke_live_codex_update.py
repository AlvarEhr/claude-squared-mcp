"""Live check: a long-lived MCP process follows a Codex app update (v0.16.2;
costs a little Codex usage — a create probe plus three one-line gpt-6-luna
turns at low effort).

    python tests/smoke_live_codex_update.py

The Codex app's updater installs a new ``bin/<hash>/``, rewrites
CODEX_CLI_PATH in config.toml and deletes the old folder. Until 0.16.2 an MCP
process cached the first codex path it resolved, so every Codex spawn after
an update failed with WinError 2 until the session restarted. This script
plays the updater against ONE process:

1. hard-links the current ``bin/<hash>/`` folder to a temp "old" folder and
   points CLAUDE_PAIR_CODEX_PATH at it; creates a Luna pair and sends a turn
   through the server's send runner (main.log names the old binary);
2. deletes the old folder and sends again: resolution falls through to
   CODEX_CLI_PATH in the real config.toml (main.log names the current binary);
3. makes a second "old" folder and deletes it between resolution and spawn:
   the send retries once with a fresh lookup.

Real Codex home (auth) with --ignore-user-config like the other live
harnesses; a temp CLAUDE_HOME; the pair runs read-only in this checkout (no
files written, no new trust entry); the thread is deleted at the end.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)


def link_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True)
    for item in src.iterdir():
        # codex + its helpers (the Windows sandbox/command runners live beside it);
        # never a whole shared bin directory when codex came from PATH.
        if item.is_file() and item.name.lower().startswith("codex"):
            try:
                os.link(item, dst / item.name)  # same volume: instant, no copy
            except OSError:
                shutil.copy2(item, dst / item.name)


def remove_tree(path: Path) -> None:
    for _ in range(50):  # antivirus may hold a fresh file briefly
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
    shutil.rmtree(path)


def main() -> int:
    scratch = tempfile.mkdtemp(prefix="cs-live-update-")
    # Resolve in a CHILD with a throwaway CLAUDE_HOME: importing the package
    # runs async_tasks' import-time orphan sweep (never on the real pairs).
    current = subprocess.run(
        [sys.executable, "-c", "from claude_squared.adapters.codex import codex_executable; "
                               "print(codex_executable())"],
        capture_output=True, text=True, check=True, timeout=60,
        env={k: v for k, v in dict(os.environ, CLAUDE_HOME=str(Path(scratch) / "resolve"),
                                   PYTHONPATH=str(ROOT / "src")).items()
             if k != "CLAUDE_PAIR_CODEX_PATH"},
    ).stdout.strip()
    if not Path(current).is_file():
        print(f"no Codex binary resolved ({current!r})")
        return 2
    print(f"Artifacts: {scratch}\nCurrent Codex binary: {current}", flush=True)
    old = Path(scratch) / "bin" / "old" / "codex.exe"
    link_tree(Path(current).parent, old.parent)
    os.environ.update({"CLAUDE_HOME": str(Path(scratch) / "claude"), "CLAUDE_PAIR_CODEX_PATH": str(old),
                       "PYTHONDONTWRITEBYTECODE": "1"})
    os.environ.pop("CODEX_HOME", None)  # the real one: auth + the app's config.toml
    sys.path.insert(0, str(ROOT / "src"))

    from claude_squared import async_tasks as A, registry as R, server as S
    from claude_squared.adapters import codex as C
    from claude_squared.models import PairSpec

    spec = PairSpec(name="update-probe", backend="codex", session_id="new", model="gpt-6-luna",
                    effort="low", permission_mode="read-only", cwd=str(ROOT))
    log = R.logs_dir() / spec.name / "main.log"

    def send(word: str):
        runner = S._build_send_runner(spec.name, f"Reply with exactly: {word}", hard_timeout_seconds=180,
                                     override_model=None, override_effort=None, override_permission_mode=None)
        task = A.start_task(spec.name, word, runner, queued=True)
        state = A.wait_for_task(task.task_id, 240)
        return state

    def last_start_line() -> str:
        lines = [l for l in log.read_text(encoding="utf-8").splitlines() if "CODEX TURN START" in l]
        return lines[-1] if lines else ""

    thread = None
    try:
        created = C.CodexAdapter().create(spec)
        thread = spec.session_id = created.session_id
        R.add_pair(spec)
        print("1. the 'old' binary (before the update)", flush=True)
        state = send("ONE")
        check("turn through the old binary succeeds", state.status == "done" and "ONE" in (state.result.response or ""),
              f"{state.status}: {state.error}")
        check("main.log names the old binary", f"exe={old})" in last_start_line(), last_start_line())

        print("2. the updater deletes the old folder", flush=True)
        remove_tree(old.parent)
        state = send("TWO")
        check("next turn succeeds without a restart", state.status == "done" and "TWO" in (state.result.response or ""),
              f"{state.status}: {state.error}")
        check("main.log names the current binary", f"exe={current})" in last_start_line(), last_start_line())

        print("3. the binary disappears between resolution and spawn", flush=True)
        racy = Path(scratch) / "bin" / "racy" / "codex.exe"
        link_tree(Path(current).parent, racy.parent)
        os.environ["CLAUDE_PAIR_CODEX_PATH"] = str(racy)
        resolve = C.codex_executable
        calls: list[bool] = []

        def resolve_then_delete(*, refresh: bool = False) -> str:
            path = resolve(refresh=refresh)
            calls.append(refresh)
            if path == str(racy):
                remove_tree(racy.parent)
            return path

        C.codex_executable = resolve_then_delete
        try:
            state = send("THREE")
        finally:
            C.codex_executable = resolve
        check("the spawn retried with a fresh lookup", True in calls, f"calls={calls}")
        check("the turn succeeds", state.status == "done" and "THREE" in (state.result.response or ""),
              f"{state.status}: {state.error}")
        check("main.log names the binary that ran", f"exe={current})" in last_start_line(), last_start_line())
        leftovers = [p.name for p in (R.logs_dir() / spec.name / "turns").iterdir() if p.stat().st_size == 0
                     and p.name.endswith(".events.jsonl")]
        check("no empty run files left behind", not leftovers, str(leftovers))
    finally:
        if thread:
            try:
                C.delete_thread(thread)
            except Exception as exc:
                print(f"  NOTE: could not delete Codex thread {thread}: {exc}")
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
