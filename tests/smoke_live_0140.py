"""Live lifecycle checks for v0.14.0 on a REAL Claude pair (costs usage).

Runs in a temporary CLAUDE_HOME (registry / logs / async state isolated) but
uses the real ``claude`` CLI and auth: a handful of short Opus turns at low
effort. Covers what the offline suites cannot — the persistent runtime's
interrupt path end to end, queued-send preservation versus ``drain_queue``,
the terminal ``stop`` marker path, and a cross-process ``pair_stop`` (the
per-task cancel file). ``--codex`` adds a Luna pair: ``pair_tool_detail`` on a
real turn and a cross-process stop of a Codex turn.

    python tests/smoke_live_0140.py [--model opus] [--codex]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_REAL_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
_TMP_HOME = Path(tempfile.mkdtemp(prefix="cs-live-0140-"))
os.environ["CLAUDE_HOME"] = str(_TMP_HOME)  # before importing the package
# CLAUDE_HOME only relocates THIS package's state (registry / logs / async);
# the claude CLI keeps writing session transcripts under the real
# ~/.claude/projects. Link the temp home's projects dir to the real one so the
# adapter finds the sessions it creates, while everything else stays isolated.
_link = _TMP_HOME / "projects"
if os.name == "nt":
    subprocess.run(["cmd", "/c", "mklink", "/J", str(_link), str(_REAL_HOME / "projects")],
                   check=True, capture_output=True)
else:
    os.symlink(_REAL_HOME / "projects", _link)

from claude_squared import async_tasks as A  # noqa: E402
from claude_squared import runtime as runtime_mod  # noqa: E402
from claude_squared import server as S  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="opus")
ap.add_argument("--codex", action="store_true", help="also run the Codex checks (Luna)")
args = ap.parse_args()

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    PASSED += ok
    FAILED += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail and not ok else ''}", flush=True)


def tool(value):
    return getattr(value, "fn", value)


def send_async(name: str, message: str) -> str:
    out = tool(S.pair_send_async)(name, message)
    match = re.search(r"Async task: ([0-9a-f-]{36})", out)
    assert match, out
    return match.group(1)


def wait(task_id: str, timeout: float):
    return A.wait_for_task(task_id, timeout)


def reply_of(state) -> str:
    return (state.result.response if state is not None and state.result is not None else "") or ""


def let_it_run(name: str, task_id: str, seconds: float = 5.0) -> bool:
    """Give the turn time to be visibly executing; False if it already finished."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = A.load_task(task_id)
        if state is None or state.status != "running":
            return False
        time.sleep(0.5)
    state = A.load_task(task_id)
    return state is not None and state.status == "running" and not state.queued


LONG = ("Write the numbers from one to eighty as English words, one per line, and after each "
        "one add a short sentence about that number. Do not use any tools. Do not stop early.")

PY = sys.executable
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
WS = _TMP_HOME / "ws"
WS.mkdir()

print(f"(temp CLAUDE_HOME {_TMP_HOME}; model {args.model})")

# ---------------------------------------------------------------- Claude
print("\n=== create a persistent Claude pair ===")
out = tool(S.pair_create)(name="lc", purpose="v0.14.0 live lifecycle check", model=args.model,
                          effort="low", permission_mode="read-only", cwd=str(WS), persistent=True)
check("pair_create", "lc" in out, out[:200])

print("\n=== 1. pair_stop mid-turn: interrupt acked, runtime alive, resume works ===")
t1 = send_async("lc", LONG)
running = let_it_run("lc", t1)
check("turn is executing before the stop", running)
out = tool(S.pair_stop)("lc")
print("   ", out)
check("in-band interrupt acked", "sent in-band interrupt" in out, out)
check("no tree-kill", "tree-killed" not in out, out)
s1 = wait(t1, 60)
check("task reported stopped", s1 is not None and s1.status == "stopped", str(s1 and s1.status))
t1b = send_async("lc", "Reply with exactly: RESUMED")
s1b = wait(t1b, 180)
check("resume on the same runtime", "RESUMED" in reply_of(s1b), reply_of(s1b)[:120] or str(s1b and s1b.error))
rt = runtime_mod.registry().get_or_none("lc")
check("runtime still warm", rt is not None and rt.is_alive())

print("\n=== 2. default stop preserves the queued send ===")
ta = send_async("lc", LONG)
let_it_run("lc", ta, 4)
tb = send_async("lc", "Reply with exactly: SECOND")
time.sleep(1.0)
poll_b = tool(S.pair_poll)(tb)
check("second send is queued", "queued for" in poll_b, poll_b[:200])
out = tool(S.pair_stop)("lc")
print("   ", out)
check("only the executing task is cancelled", ta[:8] in out and tb[:8] not in out, out)
sa = wait(ta, 60)
check("executing task stopped", sa is not None and sa.status == "stopped", str(sa and sa.status))
sb = wait(tb, 180)
check("queued task ran afterwards", "SECOND" in reply_of(sb), reply_of(sb)[:120] or str(sb and sb.error))

print("\n=== 3. drain_queue cancels the queued send before it reaches the backend ===")
ta = send_async("lc", LONG)
let_it_run("lc", ta, 4)
tc = send_async("lc", "Reply with exactly: THIRD")
time.sleep(1.0)
out = tool(S.pair_stop)("lc", drain_queue=True)
print("   ", out)
check("both tasks cancelled", ta[:8] in out and tc[:8] in out, out)
sa = wait(ta, 60)
sc = wait(tc, 60)
check("executing task stopped", sa is not None and sa.status == "stopped", str(sa and sa.status))
check("queued task stopped without running", sc is not None and sc.status == "stopped" and sc.result is None,
      f"{sc and sc.status} {sc and sc.error}")
td = send_async("lc", "Reply with exactly: FOURTH")
sd = wait(td, 180)
check("pair usable after drain", "FOURTH" in reply_of(sd), reply_of(sd)[:120] or str(sd and sd.error))

print("\n=== 4. terminal `stop -y` (per-pair marker path) ===")
ta = send_async("lc", LONG)
let_it_run("lc", ta, 4)
proc = subprocess.run([PY, "-m", "claude_squared", "stop", "lc", "-y"], env=ENV, capture_output=True, text=True,
                      cwd=str(ROOT), timeout=60)
print("   ", (proc.stdout or proc.stderr).strip()[:300])
sa = wait(ta, 60)
check("terminal stop cancelled the turn", sa is not None and sa.status == "stopped", str(sa and sa.status))
te = send_async("lc", "Reply with exactly: FIFTH")
se = wait(te, 180)
check("pair usable after terminal stop", "FIFTH" in reply_of(se), reply_of(se)[:120] or str(se and se.error))

print("\n=== 5. cross-process pair_stop (per-task cancel file + compatibility marker) ===")
ta = send_async("lc", LONG)
let_it_run("lc", ta, 4)
code = ("from claude_squared.server import pair_stop; f = getattr(pair_stop, 'fn', pair_stop); "
        "print(f('lc'))")
proc = subprocess.run([PY, "-c", code], env=ENV, capture_output=True, text=True, cwd=str(ROOT), timeout=60)
foreign_out = (proc.stdout or "") + (proc.stderr or "")
print("   ", foreign_out.strip()[:300])
check("foreign process requested cancellation", "cancellation requested for executing task" in foreign_out, foreign_out)
check("foreign process wrote the compatibility marker", "stop marker written" in foreign_out, foreign_out)
sa = wait(ta, 60)
check("owner stopped the turn on the cancel file", sa is not None and sa.status == "stopped", str(sa and sa.status))
check("cancel file cleaned up", not (A.async_dir() / f"{ta}.cancel").exists())
tf = send_async("lc", "Reply with exactly: SIXTH")
sf = wait(tf, 180)
check("pair usable after cross-process stop", "SIXTH" in reply_of(sf), reply_of(sf)[:120] or str(sf and sf.error))

# ---------------------------------------------------------------- Codex (optional)
if args.codex:
    print("\n=== 6. Codex: pair_tool_detail on a real turn ===")
    out = tool(S.pair_create)(name="lx", purpose="v0.14.0 live codex check", model="luna", effort="low",
                              permission_mode="workspace", cwd=str(WS))
    check("codex pair_create", "lx" in out, out[:200])
    tx = send_async("lx", "Run the shell command `echo TOOLDETAIL-OK` and reply with exactly its output.")
    sx = wait(tx, 240)
    check("codex turn done", sx is not None and sx.status == "done", str(sx and (sx.error or sx.status)))
    try:
        detail = tool(S.pair_tool_detail)("lx", "T-1")
    except Exception as exc:  # noqa: BLE001
        detail = f"ERROR {exc}"
    check("pair_tool_detail returns the full item", "TOOLDETAIL-OK" in detail and "Result (completed event)" in detail,
          detail[:300])

    print("\n=== 7. Codex: cross-process pair_stop tree-kills the owner's turn ===")
    ty = send_async("lx", "Run a shell command that sleeps for 60 seconds (PowerShell: Start-Sleep -Seconds 60), "
                          "then reply with exactly: SLEPT")
    let_it_run("lx", ty, 8)
    code = ("from claude_squared.server import pair_stop; f = getattr(pair_stop, 'fn', pair_stop); "
            "print(f('lx'))")
    proc = subprocess.run([PY, "-c", code], env=ENV, capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    foreign_out = (proc.stdout or "") + (proc.stderr or "")
    print("   ", foreign_out.strip()[:300])
    check("foreign process requested cancellation", "cancellation requested for executing task" in foreign_out,
          foreign_out)
    sy = wait(ty, 90)
    check("codex turn stopped by the owner", sy is not None and sy.status == "stopped", str(sy and sy.status))
    tz = send_async("lx", "Reply with exactly: BACK")
    sz = wait(tz, 240)
    check("codex thread resumes after the kill", "BACK" in reply_of(sz), reply_of(sz)[:120] or str(sz and sz.error))
    print("   ", tool(S.pair_forget)("lx", archive=False))

print("\n=== cleanup ===")
print("   ", tool(S.pair_forget)("lc", archive=False))
runtime_mod.registry().stop_all()
print(f"  (temp CLAUDE_HOME was {_TMP_HOME})")
print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
