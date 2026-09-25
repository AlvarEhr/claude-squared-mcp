"""Live checks for v0.16.0 on the Claude side (costs a little usage: Haiku only).

Drives the real server functions in-process against real Haiku pairs, and the
INSTALLED stdlib watcher (``wait.py``, written into the temp home by the
server at import) as a subprocess — exactly what an agent runs:

  1. until-idle: ``wait.py <pair>`` does not return before the self-woken turn
     that follows a background Bash completes; exit 0; the summary counts
     1 send + >=1 self-woken and names where to read the final reply
  2. ``--each-turn`` returns at the first turn's end, before the background
     finishes
  3. interim triage: a self-woken interim that asks the manager a question
     while more background work is out is flagged in the final summary; the
     triage source (the CLI's post_turn_summary vs Haiku) is recorded
  4. background protection: pair_clear refuses without force; the idle
     evictor spares the runtime; a forced stop kills + records it and a
     running ``wait.py <pair>`` exits 9; the next send's reply is REAL (the
     stale-resume "phantom" result is skipped); pair_clear(force=True) kills +
     records
  5. context alert: ``--context-alert`` fires exit 8 once on a crossing; the
     printed re-arm line does not fire immediately
  6. soft pair_stop mid-turn with background pending reports it as surviving,
     and the pair later wakes itself

Throughout: no wake-up is ever left unresolved (``wake_stale``) — sampled
every second across all six cases.

Isolation: a fresh temp CLAUDE_HOME (set before importing the package) with a
``projects`` junction to the real ~/.claude/projects (the CLI needs it). The
pairs' cwd is a scratch dir in the temp home; the only real-home writes are
their session transcripts under ~/.claude/projects/<that cwd>, removed at the
end. Cleanup touches only what this run created; anything it kills is a PID
it spawned itself (never an image name). Expected runtime: ~6-8 min.

    python tests/smoke_live_v016.py
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_REAL_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
_TMP_HOME = Path(tempfile.mkdtemp(prefix="cs-live-v016-"))
os.environ["CLAUDE_HOME"] = str(_TMP_HOME)  # before importing the package
_link = _TMP_HOME / "projects"
if os.name == "nt":
    subprocess.run(["cmd", "/c", "mklink", "/J", str(_link), str(_REAL_HOME / "projects")],
                   check=True, capture_output=True)
else:
    os.symlink(_REAL_HOME / "projects", _link)

from claude_squared import activity as A  # noqa: E402
from claude_squared import async_tasks as AT  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as runtime_mod  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared.cli_paths import encode_cwd_for_project  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402

PASSED = FAILED = 0
CASES: dict[str, str] = {}
TRIAGE_SOURCES: list[str] = []
WATCHERS: list[subprocess.Popen] = []   # every wait.py we started (killed by PID if left)
RUN = uuid.uuid4().hex[:6]
WS = _TMP_HOME / "ws"
WS.mkdir()
WAIT_PY = S._WAIT_SCRIPT_PATH
TURN_RE = re.compile(r"turns: (\d+) \((\d+) send/compact/invoke, (\d+) self-woken; (\d+) interim\)")


def check(label: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    PASSED += bool(ok)
    FAILED += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail[:600]) if detail and not ok else ''}",
          flush=True)
    return bool(ok)


def tool(value):
    return getattr(value, "fn", value)


def call(fn, *a, **kw) -> str:
    try:
        return tool(fn)(*a, **kw)
    except PairError as exc:
        return f"ERROR: {exc}"


# ---- the commands the pairs run in the background --------------------------
# 2026-09-25: at "auto" the CLI asked for approval of a backgrounded
# powershell in a temp cwd (denied headless) — pre-approve exactly this shape.
if os.name == "nt":
    # Haiku sometimes picks the PowerShell TOOL for these despite the prompt
    # (run 2026-09-25: auto mode then refused the unapproved call); the
    # workspace is a throwaway temp dir, so approve that tool outright.
    ALLOWED = ["Bash(powershell -NoProfile -Command *)", "PowerShell"]

    def bg_cmd(tag: str, secs: int) -> str:
        return (f"powershell -NoProfile -Command \"'s' | Out-File started-{tag}.txt; "
                f"Start-Sleep {secs}; 'd' | Out-File done-{tag}.txt\"")

    def fg_cmd(secs: int) -> str:
        return f"powershell -NoProfile -Command \"Start-Sleep {secs}; 'fg-done'\""
else:
    ALLOWED = ["Bash(python3 -c *)"]

    def bg_cmd(tag: str, secs: int) -> str:
        return (f"python3 -c \"import pathlib, time; pathlib.Path('started-{tag}.txt').write_text('s'); "
                f"time.sleep({secs}); pathlib.Path('done-{tag}.txt').write_text('d')\"")

    def fg_cmd(secs: int) -> str:
        return f"python3 -c \"import time; time.sleep({secs}); print('fg-done')\""


def launch_msg(tag: str, secs: int, *, then: str = "") -> str:
    return (f"Call the Bash tool with run_in_background set to true to run exactly this command, "
            f"unmodified:\n{bg_cmd(tag, secs)}\nDo not wait for it and do not check on it. Right after "
            f"launching it, reply with exactly: BG-LAUNCHED\n"
            + (then or "When you are later notified that it finished, reply with exactly: "
                       "BG-FINISHED (no tool calls)."))


def marker(tag: str, kind: str = "done") -> Path:
    return WS / f"{kind}-{tag}.txt"


# ---- helpers -----------------------------------------------------------------

def create(name: str) -> bool:
    out = call(S.pair_create, name=name, purpose="v0.16 live check", model="haiku",
               permission_mode="auto", cwd=str(WS), allowed_tools=ALLOWED)
    return check(f"created {name}", not out.startswith("ERROR") and name in R.load().pairs, out)


def send_async(name: str, message: str) -> str | None:
    handle = call(S.pair_send_async, name, message)
    tid = next((ln.split(":", 1)[1].strip() for ln in handle.splitlines()
                if ln.startswith("Async task:")), None)
    check(f"{name}: send accepted", tid is not None, handle)
    return tid


def finished(tid: str | None, timeout: float = 180):
    state = AT.wait_for_task(tid, timeout) if tid else None
    return state


def reply(state) -> str:
    return (state.result.response or "") if state is not None and state.result is not None else ""


def watch(*args: str) -> subprocess.Popen:
    env = dict(os.environ, CLAUDE_HOME=str(_TMP_HOME), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(WAIT_PY), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    WATCHERS.append(proc)
    return proc


def watch_argv(argv: list[str]) -> subprocess.Popen:
    env = dict(os.environ, CLAUDE_HOME=str(_TMP_HOME), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=env)
    WATCHERS.append(proc)
    return proc


def done_watching(proc: subprocess.Popen, timeout: float):
    """(exit code, stdout, stderr, utc exit time); a watcher that outlives the
    timeout is killed by ITS pid and reported as code None."""
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, datetime.utcnow()
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=10)
        return None, out, err, datetime.utcnow()


def self_woken_done(name: str) -> list:
    out = []
    for p in R.async_dir().glob("*.json"):
        try:
            st = AT.load_task(p.stem)
        except Exception:
            continue
        if (st is not None and st.pair_name == name and AT.is_self_woken_task(st)
                and st.status == "done"):
            out.append(st)
    return sorted(out, key=lambda s: s.finished_at or datetime.min)


def main_log(name: str) -> str:
    try:
        return (_TMP_HOME / "pairs" / "logs" / name / "main.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_until(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def runtime_of(name: str):
    return runtime_mod.registry().get_or_none(name)


def turns_line(out: str):
    m = TURN_RE.search(out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def case(key: str, ok: bool, note: str) -> None:
    CASES[key] = f"{'ok  ' if ok else 'FAIL'} {note}"


PAIR_IDLE = f"lv16-idle-{RUN}"
PAIR_TRIAGE = f"lv16-triage-{RUN}"
PAIR_PROTECT = f"lv16-protect-{RUN}"
PAIR_STOP = f"lv16-stop-{RUN}"
t_start = time.monotonic()
print(f"(temp CLAUDE_HOME {_TMP_HOME}; wait.py {WAIT_PY})", flush=True)

# An unresolved wake-up (announced, never started within its bound) must
# never occur in these normal scenarios — the wake triggers are meant to fire
# only for turns the CLI really queues (critic decision, 2026-09-25). Sampled
# every second across all cases: the activity view's wake_stale, and the
# runtime's own wake age.
UNRESOLVED: list[str] = []
_SAMPLING = threading.Event()


def _sample_wakes() -> None:
    while not _SAMPLING.is_set():
        for name in (PAIR_IDLE, PAIR_TRIAGE, PAIR_PROTECT, PAIR_STOP):
            try:
                if A.view(name, grace_seconds=0).get("wake_stale"):
                    UNRESOLVED.append(f"{name}: wake_stale in the activity view")
                rt = runtime_mod.registry().get_or_none(name)
                at = getattr(rt, "_wake_at", None) if rt is not None else None
                if at is not None and time.monotonic() - at >= A.WAKE_PENDING_SECONDS:
                    UNRESOLVED.append(f"{name}: runtime wake unresolved for {time.monotonic() - at:.0f}s")
            except Exception:
                pass
        _SAMPLING.wait(1.0)


threading.Thread(target=_sample_wakes, daemon=True, name="wake-sampler").start()

try:
    check("wait.py installed in the temp home", WAIT_PY is not None and Path(WAIT_PY).exists()
          and Path(WAIT_PY).resolve().is_relative_to(_TMP_HOME.resolve()), str(WAIT_PY))

    # ------------------------------------------------------------------ 1 + 2
    print("\n=== 1+2. until-idle and --each-turn around a background Bash ===", flush=True)
    before = FAILED
    if create(PAIR_IDLE):
        tid = send_async(PAIR_IDLE, launch_msg("idle", 30))
        w_idle = watch(PAIR_IDLE, "--poll", "0.5", "--timeout", "240")
        w_each = watch(PAIR_IDLE, "--each-turn", "--poll", "0.5", "--timeout", "240")
        code_e, out_e, err_e, t_each = done_watching(w_each, 150)
        marker_at_each = marker("idle").exists()
        check("--each-turn exits 0", code_e == 0, f"code={code_e} out={out_e} err={err_e}")
        check("--each-turn returned at the first turn end", "turn ended" in out_e, out_e)
        check("... before the background finished", not marker_at_each)
        ok2 = FAILED == before
        state = finished(tid, 120)
        check("the launch turn replied BG-LAUNCHED", "BG-LAUNCHED" in reply(state), reply(state))
        code_i, out_i, err_i, t_idle = done_watching(w_idle, 200)
        check("until-idle exits 0", code_i == 0, f"code={code_i} out={out_i} err={err_i}")
        check("... only after the background finished", marker("idle").exists())
        woken = [s for s in self_woken_done(PAIR_IDLE) if s.finished_at and s.finished_at <= t_idle]
        check("... and after a self-woken turn completed", bool(woken),
              f"self-woken tasks: {[(s.task_id[:8], s.status, s.finished_at) for s in self_woken_done(PAIR_IDLE)]}")
        counts = turns_line(out_i)
        check("summary counts 1 send + >=1 self-woken",
              counts is not None and counts[1] == 1 and counts[2] >= 1, out_i)
        check("summary names where to read the final reply", "final reply: pair_poll(" in out_i, out_i)
        print("    " + "\n    ".join(out_i.strip().splitlines()[:6]), flush=True)
        case("1 until-idle", FAILED == before, f"exit {code_i}; turns {counts}")
        case("2 each-turn", ok2, f"exit {code_e} before the background finished: {not marker_at_each}")
    else:
        case("1 until-idle", False, "pair creation failed")
        case("2 each-turn", False, "pair creation failed")

    # ------------------------------------------------------------------ 5
    print("\n=== 5. context alert fires once on a crossing; re-arm doesn't fire at once ===", flush=True)
    before = FAILED
    if PAIR_IDLE in R.load().pairs:
        big = WS / "big.txt"
        big.write_text("".join(f"line {i:04d}: the quick brown fox jumps over the lazy dog again\n"
                               for i in range(1500)) + "LAST-LINE-OK\n", encoding="utf-8")
        ctx = A.view(PAIR_IDLE).get("context") or {}
        used0, window = int(ctx.get("used") or 0), int(ctx.get("window") or 0)
        check("live context recorded for the pair", used0 > 0 and window > 0, json.dumps(ctx))
        if used0 and window:
            pct = math.ceil((used0 + 4000) * 1000 / window) / 10.0  # 4k tokens above the fill now
            # Send FIRST: a watcher started while the pair is idle correctly
            # returns idle at once (first run, 2026-09-25). Its first
            # observation (the turn just opened) is the fill it measures from.
            tid = send_async(PAIR_IDLE, "Use the Read tool on big.txt with NO offset and NO limit (the "
                                        "whole file), then reply with exactly its last line and nothing else.")
            w8 = watch(PAIR_IDLE, "--context-alert", str(pct), "--poll", "0.5", "--timeout", "240")
            code8, out8, err8, _t = done_watching(w8, 150)
            check("exit 8 on the crossing", code8 == 8, f"code={code8} out={out8} err={err8}")
            check("the alert gives the numbers + the clean-compaction recipe",
                  "CONTEXT ALERT" in out8 and "pair_compact" in out8, out8)
            rearm = next((ln.split("Keep watching:", 1)[1].strip() for ln in out8.splitlines()
                          if "Keep watching:" in ln), None)
            check("a re-arm line is printed", bool(rearm), out8)
            if rearm:
                w8b = watch_argv(shlex.split(rearm))
                code8b, out8b, err8b, _t = done_watching(w8b, 200)
                check("the re-armed watcher does not fire (returns idle instead)",
                      code8b == 0 and "CONTEXT ALERT" not in out8b, f"code={code8b} out={out8b} err={err8b}")
            state = finished(tid, 120)
            # Case 5 tests the ALERT; Haiku sometimes garbles the quoted line
            # (run 2026-09-25: it merged the Read tool's line number into it).
            check("the read turn completed", getattr(state, "status", None) == "done" and bool(reply(state)),
                  reply(state))
        case("5 context alert", FAILED == before, f"threshold {pct if used0 and window else '?'}% of {window}")
    else:
        case("5 context alert", False, "no pair")

    # ------------------------------------------------------------------ 3
    print("\n=== 3. interim triage flags a question asked while more background is out ===", flush=True)
    before = FAILED
    if create(PAIR_TRIAGE):
        msg = ("Do exactly this, one tool call per command:\n"
               f"1. Call the Bash tool with run_in_background set to true to run exactly:\n{bg_cmd('t1', 15)}\n"
               f"2. Call the Bash tool with run_in_background set to true to run exactly:\n{bg_cmd('t2', 55)}\n"
               "Then reply with exactly: LAUNCHED\n"
               "When you are notified that the FIRST command finished while the second is still "
               "running, make no tool calls and reply with exactly this question to your manager: "
               "'Manager: step 1 finished. Should I delete its output before step 2 ends? I need "
               "your yes or no before I continue.'\n"
               "When you are notified that the SECOND command finished, reply with exactly: ALL-DONE")
        tid = send_async(PAIR_TRIAGE, msg)
        w3 = watch(PAIR_TRIAGE, "--poll", "0.5", "--timeout", "300")
        code3, out3, err3, _t = done_watching(w3, 260)
        check("until-idle exits 0", code3 == 0, f"code={code3} out={out3} err={err3}")
        interims = [t for t in A.view(PAIR_TRIAGE).get("window_turns") or [] if t.get("interim")
                    and t.get("kind") == "self_woken"]
        check("a self-woken interim was recorded", bool(interims),
              json.dumps(A.view(PAIR_TRIAGE).get("window_turns"), default=str)[:600])
        triaged = [t for t in interims if isinstance(t.get("triage"), dict)
                   and t["triage"].get("state") == "done"]
        check("the interim was triaged", bool(triaged), json.dumps(interims, default=str)[:600])
        for t in triaged:
            tri = t["triage"]
            TRIAGE_SOURCES.append(f"{tri.get('source')} (importance {tri.get('importance')}, "
                                  f"needs_attention {tri.get('needs_attention')}, "
                                  f"category {tri.get('category')!r}, reason {tri.get('reason')!r})")
        flagged = [t for t in triaged if t["triage"].get("needs_attention")]
        check("the question interim is flagged for attention", bool(flagged),
              "; ".join(TRIAGE_SOURCES) + f" | preview: {[t.get('preview') for t in interims]}")
        check("the summary lists it with a reason and pair_poll",
              "interims flagged for attention" in out3 and "pair_poll('" in out3, out3)
        print("    " + "\n    ".join(out3.strip().splitlines()[:10]), flush=True)
        case("3 interim triage", FAILED == before,
             f"sources: {', '.join(s.split(' ')[0] for s in TRIAGE_SOURCES) or 'none'}")
    else:
        case("3 interim triage", False, "pair creation failed")

    # ------------------------------------------------------------------ 4
    print("\n=== 4. background protection + the stale-resume (phantom) guard ===", flush=True)
    before = FAILED
    if create(PAIR_PROTECT):
        state = finished(send_async(PAIR_PROTECT, launch_msg("p1", 90, then="Do nothing when it finishes.")))
        check("launch replied BG-LAUNCHED", "BG-LAUNCHED" in reply(state), reply(state))
        rt = runtime_of(PAIR_PROTECT)
        pending = rt.pending_background_tasks() if rt is not None else []
        if check("background pending in the runtime", bool(pending), str(pending)):
            out = call(S.pair_clear, PAIR_PROTECT)
            check("pair_clear refuses without force", out.startswith("ERROR") and "force=True" in out, out)
            rt.last_activity = datetime.utcnow() - timedelta(hours=2)  # well past the 10 min idle cutoff
            runtime_mod.registry()._evict_idle()
            check("the idle evictor spares it (2 h idle, background pending)",
                  runtime_of(PAIR_PROTECT) is rt and rt.is_alive())
            w9 = watch(PAIR_PROTECT, "--poll", "0.5", "--timeout", "120")
            time.sleep(2.0)
            out = call(S.pair_stop, PAIR_PROTECT, force=True)
            check("pair_stop(force=True) kills + records", "killed and recorded as lost" in out, out)
            code9, out9, err9, _t = done_watching(w9, 60)
            check("the running wait.py exits 9", code9 == 9, f"code={code9} out={out9} err={err9}")
            check("... naming what was lost", "BACKGROUND LOST" in err9, err9)
            state = finished(send_async(PAIR_PROTECT, "Reply with exactly: PING-OK"), 120)
            check("the next send's reply is REAL (not the empty stale-resume result)",
                  "PING-OK" in reply(state), repr(reply(state)))
            check("... the CLI's stale-resume notice was seen and skipped",
                  "STALE RESUME NOTICE" in main_log(PAIR_PROTECT),
                  "no notice in main.log — the CLI did not emit one this time (guard not exercised)")
            state = finished(send_async(PAIR_PROTECT, launch_msg("p2", 90, then="Do nothing when it finishes.")))
            check("second launch replied BG-LAUNCHED", "BG-LAUNCHED" in reply(state), reply(state))
            out = call(S.pair_clear, PAIR_PROTECT, force=True)
            check("pair_clear(force=True) clears and reports the loss",
                  out.startswith("Cleared") and "killed and recorded as lost" in out, out)
        case("4 protection", FAILED == before, "refuse / spare / kill+record / exit 9 / real reply")
    else:
        case("4 protection", False, "pair creation failed")

    # ------------------------------------------------------------------ 6
    print("\n=== 6. soft pair_stop mid-turn keeps background work alive ===", flush=True)
    before = FAILED
    if create(PAIR_STOP):
        msg = ("Do exactly these two steps, in order, one tool call each.\n"
               f"Step 1: call the Bash tool with run_in_background set to true to run exactly:\n{bg_cmd('s', 40)}\n"
               f"Step 2: after step 1 is launched, call the Bash tool in the FOREGROUND "
               f"(run_in_background false) to run exactly:\n{fg_cmd(30)}\n"
               "Then reply with exactly: BOTH-DONE. If you are later notified that the background "
               "command finished, reply with exactly: BG-FINISHED (no tool calls).")
        tid = send_async(PAIR_STOP, msg)
        mid = wait_until(lambda: marker("s", "started").exists()
                         and "(foreground)" in main_log(PAIR_STOP), 120)
        if check("the foreground step started with the background out", mid, main_log(PAIR_STOP)[-800:]):
            time.sleep(3.0)
            out = call(S.pair_stop, PAIR_STOP)
            check("soft stop interrupted the turn", "interrupt" in out, out)
            check("... and reports the background as surviving",
                  "still running and will wake the pair" in out, out)
            state = finished(tid, 30)
            check("the interrupted task is stopped", state is not None and state.status == "stopped",
                  getattr(state, "status", None))
            w6 = watch(PAIR_STOP, "--poll", "0.5", "--timeout", "180")
            code6, out6, err6, _t = done_watching(w6, 150)
            check("the background finished anyway", marker("s").exists())
            check("... and the pair woke itself (wait.py idle, a self-woken turn done)",
                  code6 == 0 and bool(self_woken_done(PAIR_STOP)), f"code={code6} out={out6} err={err6}")
        case("6 soft stop", FAILED == before, "background survived and woke the pair")
    else:
        case("6 soft stop", False, "pair creation failed")

    _SAMPLING.set()
    check("no unresolved wake-up (wake_stale) in any scenario (cases 1-6)", not UNRESOLVED,
          "; ".join(sorted(set(UNRESOLVED))[:5]))
except Exception:
    import traceback
    traceback.print_exc()
    check("harness ran to completion", False, "crashed; see traceback above")
finally:
    print("\n=== cleanup ===", flush=True)

    def step(label: str, fn) -> None:
        """One cleanup step; an error is counted and the rest continue."""
        try:
            fn()
            print(f"    {label}")
        except Exception as exc:  # noqa: BLE001
            check(label, False, f"{type(exc).__name__}: {exc}")

    for proc in WATCHERS:  # our own watcher processes, by PID
        if proc.poll() is None:
            step(f"stopped leftover watcher pid {proc.pid}", proc.kill)
    for name in (PAIR_IDLE, PAIR_TRIAGE, PAIR_STOP, PAIR_PROTECT):
        if name in R.load().pairs:
            step(f"forgot {name}", lambda n=name: print("     ", call(S.pair_forget, n, archive=False)))
    step("stopped all runtimes this process started", lambda: runtime_mod.registry().stop_all())

    def _remove_project_dir() -> None:
        proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
        if proj.exists():
            shutil.rmtree(proj)

    step("removed the pairs' transcripts from ~/.claude/projects", _remove_project_dir)

    def _remove_link() -> None:
        # The link only, never its target: RemoveDirectory on a junction
        # (or unlink on a symlink) deletes the link itself.
        if os.name == "nt":
            if os.lstat(_link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                os.rmdir(_link)
        elif _link.is_symlink():
            _link.unlink()

    step("removed the projects link", _remove_link)
    print(f"  (temp CLAUDE_HOME was {_TMP_HOME})")
    print("\n=== per-case ===")
    for key in sorted(CASES):
        print(f"  {key:<20} {CASES[key]}")
    print(f"  triage source(s): {'; '.join(TRIAGE_SOURCES) or '(none recorded)'}")
    print(f"\n{PASSED} passed, {FAILED} failed in {time.monotonic() - t_start:.0f}s")
    sys.exit(1 if FAILED else 0)
