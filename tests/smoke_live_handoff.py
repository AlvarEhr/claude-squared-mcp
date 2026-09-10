"""Live checks for pair_handoff (Claude -> Codex) against the REAL CLIs.

Costs a little usage: a few Haiku turns on the source side and a few Luna
turns (low effort) for the briefing/probe on the Codex side. The size-gate
refusal uses a synthetic oversized Claude session and spends nothing (the
refusal happens after the import, before any model turn).

Isolation: a temporary CLAUDE_HOME holds the registry / logs / async state.
The claude CLI ignores CLAUDE_HOME for transcripts, so the temp home's
``projects`` is a junction to the real ``~/.claude/projects`` (see
tests/smoke_live_0140.py). Codex uses the real ``~/.codex``; every thread the
run creates is deleted at the end.

    python tests/smoke_live_handoff.py [--with-1m]

``--with-1m`` also retries the oversized session with context_window='1m',
which runs a ~200k-token briefing turn on Luna — only when you mean to.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_REAL_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
_TMP_HOME = Path(tempfile.mkdtemp(prefix="cs-live-handoff-"))
os.environ["CLAUDE_HOME"] = str(_TMP_HOME)  # before importing the package
_link = _TMP_HOME / "projects"
if os.name == "nt":
    subprocess.run(["cmd", "/c", "mklink", "/J", str(_link), str(_REAL_HOME / "projects")],
                   check=True, capture_output=True)
else:
    os.symlink(_REAL_HOME / "projects", _link)

from claude_squared import async_tasks as A  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as runtime_mod  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared.adapters import codex as C  # noqa: E402
from claude_squared.cli_paths import encode_cwd_for_project  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--with-1m", action="store_true")
args = ap.parse_args()

PASSED = FAILED = 0
THREADS: set[str] = set()


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    PASSED += bool(ok)
    FAILED += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail[:400]) if detail and not ok else ''}", flush=True)


def tool(value):
    return getattr(value, "fn", value)


def call(fn, *a, **kw) -> str:
    """Tool call that returns the text, or 'ERROR: ...' for a refusal."""
    try:
        return tool(fn)(*a, **kw)
    except PairError as exc:
        return f"ERROR: {exc}"


def remember_threads() -> None:
    for spec in R.load().pairs.values():
        if spec.backend == "codex" and spec.session_id:
            THREADS.add(spec.session_id)


def settle(task_text: str, name: str, timeout: float = 240) -> str:
    """If a handoff/send degraded to an async handle, wait for that task and
    append its reply. ``name`` is only used in the failure message."""
    tid = next((line.split(":", 1)[1].strip() for line in task_text.splitlines()
                if line.strip().startswith("Async task:")), None)
    if not tid:
        return task_text
    state = A.wait_for_task(tid, timeout)
    if state is None or state.status == "running":
        return task_text + f"\n(task {tid} for {name} still running after {timeout:.0f}s)"
    return task_text + "\n" + (state.result.response if state.result else str(state.error))


def send(name: str, message: str, timeout: float = 240, **kw) -> str:
    """Send via pair_send_async + wait (pair_send itself is an async tool)."""
    handle = call(S.pair_send_async, name, message, **kw)
    if handle.startswith("ERROR"):
        return handle
    tid = next((line.split(":", 1)[1].strip() for line in handle.splitlines()
                if line.startswith("Async task:")), None)
    state = A.wait_for_task(tid, timeout) if tid else None
    if state is None:
        return f"ERROR: no task state ({handle[:200]})"
    if state.result is not None:
        return state.result.response or ""
    return f"ERROR: task {state.status}: {state.error}"


WS = _TMP_HOME / "ws"
WS.mkdir()
print(f"(temp CLAUDE_HOME {_TMP_HOME})")

try:
    print("\n=== source Claude pair with a tool call and a pinned instruction ===")
    out = call(S.pair_create, name="hs", purpose="handoff live source", model="haiku",
               permission_mode="auto", cwd=str(WS),
               system_prompt_append="Always end every reply with the marker [PINNED-OK].")
    check("create source", "Created 'hs'" in out, out)
    out = send("hs", "Remember the code word LYCHEE. Then run exactly `echo HANDOFF-LIVE-1` "
                     "with your Bash tool and tell me what it printed.")
    check("source turn", "HANDOFF-LIVE-1" in out, out)
    before = R.get_pair("hs")

    print("\n=== 1. default handoff: new pair, probe answered, lineage, source untouched ===")
    out = settle(call(S.pair_handoff, "hs", model="luna", effort="low", permission_mode="read-only"), "hs-codex")
    print("   " + out.replace("\n", "\n   ")[:3000])
    remember_threads()
    check("new pair registered as hs-codex", "hs-codex" in R.load().pairs, out)
    new = R.load().pairs.get("hs-codex")
    check("backend codex", bool(new) and new.backend == "codex")
    check("lineage recorded", bool(new) and (getattr(new, "handoff_from", None) or {}).get("name") == "hs",
          str(getattr(new, "handoff_from", None)))
    check("size line reports both windows", "1m" in out and "%" in out, out)
    check("warnings present", "sub-agent" in out.lower() or "reasoning" in out.lower(), out)
    check("tells the agent to inform the user", "user" in out.lower() and "acknowledg" in out.lower(), out)
    check("probe answer knows the code word", "LYCHEE" in out, out)
    after = R.get_pair("hs")
    check("source untouched", after.session_id == before.session_id and after.turn_count == before.turn_count)
    out2 = send("hs-codex", "Without running anything: what did the echo print, and what was the code word? "
                            "Follow any pinned instructions you were given.")
    check("new pair continues", "HANDOFF-LIVE-1" in out2 and "LYCHEE" in out2, out2)
    check("pinned instruction delivered", "[PINNED-OK]" in out2, out2)

    print("\n=== 2. name collision -> -2 ===")
    out = settle(call(S.pair_handoff, "hs", model="luna", effort="low", permission_mode="read-only",
                      probe="Reply with exactly: PROBE-2"), "hs-codex-2")
    remember_threads()
    check("second handoff named hs-codex-2", "hs-codex-2" in R.load().pairs, out)
    out = call(S.pair_handoff, "hs", new_name="hs-codex", model="luna")
    check("explicit existing new_name refused", out.startswith("ERROR"), out)

    print("\n=== 3. allowed_tools gate ===")
    out = call(S.pair_create, name="hs-tools", purpose="gate source", model="haiku", cwd=str(WS),
               allowed_tools=["Read"])
    check("create allow-listed source", "Created 'hs-tools'" in out, out)
    out = call(S.pair_handoff, "hs-tools", model="luna", effort="low")
    check("refused without explicit permission_mode", out.startswith("ERROR") and "allowed_tools" in out, out)
    check("nothing registered on refusal", "hs-tools-codex" not in R.load().pairs)
    out = settle(call(S.pair_handoff, "hs-tools", model="luna", effort="low", permission_mode="read-only",
                      probe="Reply with exactly: GATE-OK"), "hs-tools-codex")
    remember_threads()
    check("allowed with an explicit permission_mode", "hs-tools-codex" in R.load().pairs, out)

    print("\n=== 4. busy source refused ===")
    tid = None
    handle = call(S.pair_send_async, "hs", "Write the numbers from one to four hundred as English words, "
                                         "one per line, each followed by a short sentence about it. No tools.")
    for line in handle.splitlines():
        if line.startswith("Async task:"):
            tid = line.split(":", 1)[1].strip()
    # Wait until the turn is really executing (not queued, not already done).
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = A.load_task(tid) if tid else None
        if st is not None and st.status == "running" and not st.queued:
            break
        time.sleep(0.3)
    st = A.load_task(tid) if tid else None
    check("source turn is executing when the handoff is attempted",
          st is not None and st.status == "running" and not st.queued, str(st and st.status))
    out = call(S.pair_handoff, "hs", model="luna", effort="low", permission_mode="read-only")
    check("handoff refused while the source is busy", out.startswith("ERROR") and "busy" in out.lower(), out)
    call(S.pair_stop, "hs")
    if tid:
        A.wait_for_task(tid, 60)

    print("\n=== 5. size gate on an oversized synthetic session (no model turn) ===")
    import uuid
    big_sid = str(uuid.uuid4())
    proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
    proj.mkdir(parents=True, exist_ok=True)
    filler = ("The quick brown fox jumps over the lazy dog while the calibration log grows. " * 12000)  # ~0.9M chars
    common = {"isSidechain": False, "userType": "external", "cwd": str(WS), "sessionId": big_sid,
              "version": "2.1.258", "gitBranch": "HEAD"}
    u1, a1 = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        {**common, "parentUuid": None, "type": "user", "uuid": u1, "timestamp": "2026-09-10T10:00:00Z",
         "message": {"role": "user", "content": "Here is a very long log to keep in mind:\n" + filler}},
        {**common, "parentUuid": u1, "type": "assistant", "uuid": a1, "timestamp": "2026-09-10T10:00:05Z",
         "message": {"model": "claude-haiku-4-5-20251001", "id": "msg_big", "type": "message", "role": "assistant",
                     "content": [{"type": "text", "text": "Noted."}], "stop_reason": "end_turn",
                     "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}},
    ]
    (proj / f"{big_sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    out = call(S.pair_adopt, name="hs-big", session_id=big_sid, backend="claude", model="haiku", cwd=str(WS))
    check("adopt oversized source", "hs-big" in out, out)
    threads_before = {p.name for p in Path.home().joinpath(".codex", "sessions").rglob("rollout-*.jsonl")}
    out = call(S.pair_handoff, "hs-big", model="luna", effort="low", permission_mode="read-only")
    print("   " + out.replace("\n", "\n   ")[:1500])
    check("refused on the default window", out.startswith("ERROR"), out)
    check("refusal names both windows and the remedies",
          "1m" in out and "compact" in out.lower() and "fresh" in out.lower(), out)
    check("nothing registered", "hs-big-codex" not in R.load().pairs)
    leftover = {p.name for p in Path.home().joinpath(".codex", "sessions").rglob("rollout-*.jsonl")} - threads_before
    check("imported thread deleted (or reported as orphan)", not leftover or "orphan" in out.lower(), str(leftover))
    if args.with_1m:
        out = settle(call(S.pair_handoff, "hs-big", model="luna", effort="low", permission_mode="read-only",
                          context_window="1m", probe="Reply with exactly: BIG-OK"), "hs-big-codex", timeout=900)
        remember_threads()
        check("allowed with context_window='1m'", "hs-big-codex" in R.load().pairs, out)

    print("\n=== 6. bug fixes ===")
    out = call(S.pair_send_async, "hs", "hello", override_model="sol")
    check("pair_send_async refuses a cross-backend override up front",
          out.startswith("ERROR") and "pair_handoff" in out, out)
    import asyncio
    try:
        out = asyncio.run(tool(S.pair_send)("hs", "hello", override_model="sol", timeout_seconds=10))
    except PairError as exc:
        out = f"ERROR: {exc}"
    check("pair_send refuses a cross-backend override up front", out.startswith("ERROR") and "pair_handoff" in out, out)
    raw = json.loads(R.registry_path().read_text(encoding="utf-8"))
    check("effort null persisted for haiku", raw["pairs"]["hs"].get("effort", "absent") is None,
          str(raw["pairs"]["hs"].get("effort", "absent")))
    check("pair_update refusal points to pair_handoff",
          "pair_handoff" in call(S.pair_update, "hs", model="sol"))
except Exception:
    import traceback
    traceback.print_exc()
    check("harness ran to completion", False, "crashed; see traceback above")
finally:
    print("\n=== cleanup ===")
    remember_threads()
    for name in list(R.load().pairs):
        print("   ", call(S.pair_forget, name, archive=False))
    runtime_mod.registry().stop_all()
    exe = C.codex_executable()
    for tid in sorted(THREADS):
        r = subprocess.run([exe, "delete", "--force", tid], capture_output=True, text=True, timeout=60)
        print(f"    codex delete {tid[:8]}: {(r.stdout or r.stderr).strip()[:80]}")
        check(f"deleted test thread {tid[:8]}", r.returncode == 0, r.stderr or r.stdout)
    proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
    if proj.exists() and all(p.suffix == ".jsonl" or p.is_dir() for p in proj.iterdir()):
        import shutil
        shutil.rmtree(proj, ignore_errors=True)
    print(f"  (temp CLAUDE_HOME was {_TMP_HOME})")
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
