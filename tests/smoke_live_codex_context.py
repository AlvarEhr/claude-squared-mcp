"""Live check: Codex live context and the context alert (v0.16.0; costs a
little Codex usage — a creation turn plus one file-reading turn on gpt-6-luna).

    python tests/smoke_live_codex_context.py

Checks that live context comes from the rollout (last_token_usage) and that
`wait.py <pair> --context-alert PCT` exits 8 with the rollout-derived numbers
and the Codex recipe. The alert may arrive just AFTER the turn: Codex writes
the token_count for a tool-issuing call only once that tool finishes
(observed 2026-09-25), so the feed can lag one tool call. Real Codex home
with --ignore-user-config; deletes the thread it created.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-exe")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--model", default="gpt-6-luna")
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
    home = Path(args.codex_home).resolve()
    if not home.is_dir():
        parser.error("Codex home must exist")
    if not Path(args.codex_exe).is_file():
        parser.error("Codex executable must exist")
    artifacts = Path(tempfile.mkdtemp(prefix="cs-live-codex-context-"))
    os.environ.update({"CLAUDE_HOME": str(artifacts / "claude"), "CODEX_HOME": str(home),
                       "CLAUDE_PAIR_CODEX_PATH": str(Path(args.codex_exe).resolve()),
                       "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONIOENCODING": "utf-8"})
    sys.path.insert(0, str(ROOT / "src"))
    from claude_squared import activity as V, async_tasks as A, registry as R, server as S
    from claude_squared.adapters.codex import CodexAdapter, read_last_token_count, rollout_path_for
    from claude_squared.models import PairSpec
    from claude_squared._wait_script import install_wait_script

    big = artifacts / "context-input.txt"
    big.write_text("\n".join(f"row {i:04}: {secrets.token_hex(24)}" for i in range(1000)), encoding="utf-8")
    gate = artifacts / "watcher-ready"
    spec = PairSpec(name="context-probe", session_id="new", backend="codex", model=args.model,
                    effort="low", permission_mode="read-only", cwd=str(artifacts))
    spec.session_id = CodexAdapter().create(spec).session_id
    R.add_pair(spec)
    payload = read_last_token_count(rollout_path_for(spec.session_id)) or {}
    info = payload.get("info") or {}
    baseline = int((info.get("last_token_usage") or {}).get("input_tokens") or 0)
    window = int(info.get("model_context_window") or 0)
    if not baseline or not window:
        raise RuntimeError("creation turn reported no context; inspect retained rollout")
    pct = math.ceil((baseline + min(8000, window * .03)) * 100 / window)
    if not 0 < pct < 80:
        raise RuntimeError(f"unexpected baseline/window: {baseline}/{window}")
    print(f"Artifacts: {artifacts}\nBaseline {baseline:,}/{window:,}; alert at {pct}%", flush=True)
    prompt = (
        "Do exactly these steps, in order. First run one PowerShell command that waits until "
        "the file 'watcher-ready' exists (check every 200 ms; stop waiting after 60 seconds), "
        "then prints ALL of 'context-input.txt' with Get-Content -Raw. Set the shell tool's "
        "max_output_tokens to 40000 so the full file reaches your context. Do not summarize "
        "or filter that output. Next run a SEPARATE shell command: Start-Sleep -Seconds 25. "
        "Finally reply exactly: CONTEXT-PROBE-DONE. Do not write any files."
    )
    runner = S._build_send_runner(spec.name, prompt, hard_timeout_seconds=240,
                                 override_model=None, override_effort=None, override_permission_mode=None)
    task = A.start_task(spec.name, prompt, runner, queued=True)
    waiter = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snap = V.writer(spec.name, "codex").snapshot()
            if (snap.get("turn") or {}).get("task_id") == task.task_id and snap.get("context"):
                break
            if A.load_task(task.task_id).status != "running":
                raise RuntimeError("send ended before activity/context was published")
            time.sleep(.1)
        else:
            raise RuntimeError("no live context within 30 seconds")
        V.writer(spec.name, "codex").flush()
        cmd = [sys.executable, "-B", "-u", str(install_wait_script()), spec.name,
               "--context-alert", str(pct), "--poll", ".5", "--timeout", "180"]
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        waiter = subprocess.Popen(cmd, env=os.environ.copy(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", **flags)
        # The CLI is held at the gate while the actual standalone watcher
        # starts and samples its baseline. No watcher code is modified.
        time.sleep(2)
        if waiter.poll() is not None:
            raise RuntimeError("watcher exited before releasing the file-read gate")
        gate.touch()
        deadline = time.monotonic() + 185
        previous = None
        while waiter.poll() is None and time.monotonic() < deadline:
            context = V.view(spec.name).get("context") or {}
            if context.get("used") != previous:
                previous = context.get("used")
                print(json.dumps(context, indent=2), flush=True)
            time.sleep(.5)
        out, err = waiter.communicate(timeout=5)
        print(out + err, flush=True)
        assert waiter.returncode == 8, f"expected alert exit 8, got {waiter.returncode}"
        match = re.search(r"CONTEXT ALERT:.*?at ([\d,]+) / ([\d,]+) tokens", out)
        assert match, "alert did not report token counts"
        alerted = tuple(int(value.replace(",", "")) for value in match.groups())
        reported = set()
        # A later model call may update context between the alert and this
        # assertion, so compare with history rather than only the newest row.
        with rollout_path_for(spec.session_id).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                pl = event.get("payload") or {}
                if event.get("type") == "event_msg" and pl.get("type") == "token_count":
                    info = pl.get("info") or {}
                    reported.add(((info.get("last_token_usage") or {}).get("input_tokens"),
                                  info.get("model_context_window")))
        assert alerted in reported, f"alert counts {alerted} were not reported in the rollout"
        phase = "mid-turn" if A.load_task(task.task_id).status == "running" else "after the turn"
        status = getattr(S.pair_status, "fn", S.pair_status)(spec.name)
        print(status, flush=True)
        assert "live" in status and "context:" in status
        final = A.wait_for_task(task.task_id, 180)
        assert final.status == "done" and "CONTEXT-PROBE-DONE" in final.result.response, final
        snap = V.writer(spec.name, "codex").snapshot()
        assert snap["turn"] is None and snap["window_turns"][-1]["status"] == "done"
        print(f"PASS: rollout-derived Codex crossing exit 8 ({phase}), final activity done", flush=True)
    finally:
        if waiter is not None:
            if waiter.poll() is None:
                waiter.kill()  # only the watcher this probe spawned
                waiter.wait(timeout=5)
            waiter.stdout.close()
            waiter.stderr.close()
        state = A.load_task(task.task_id)
        if state and state.status == "running":
            getattr(S.pair_stop, "fn", S.pair_stop)(spec.name)  # only this probe's child
            A.wait_for_task(task.task_id, 15)
        V.flush_all()
        try:
            subprocess.run([args.codex_exe, "delete", "--force", spec.session_id], capture_output=True, timeout=60)
            print("deleted probe thread", spec.session_id, flush=True)
        except Exception as exc:
            print("thread cleanup failed:", exc, flush=True)


if __name__ == "__main__":
    main()
