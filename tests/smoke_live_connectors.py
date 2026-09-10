"""Live checks for per-pair MCP connectors on BOTH backends (costs a little usage).

Uses the throwaway server tests/fixtures/mcp_probe_server.py ("cs-probe":
probe_read is annotated read-only, probe_write writes a file OUTSIDE the pair
workspace). Claude sees it through a project-scope ``.mcp.json`` in the scratch
workspace — no change to your Claude config. Codex only sees servers in its
own config, so the run registers ``cs-probe`` with ``codex mcp add`` and ALWAYS
removes it at the end (the user approved this, 2026-09-10).

Policy under test (confirmed by the user): read-only / plan / workspace —
Codex runs only read-only-annotated tools, Claude runs none (each blocked call
is reported); auto — Codex runs all (writes judged by its reviewer), Claude
runs all (pre-approved per server); unrestricted — all.

    python tests/smoke_live_connectors.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_REAL_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
_TMP_HOME = Path(tempfile.mkdtemp(prefix="cs-live-conn-"))
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

PASSED = FAILED = 0
THREADS: set[str] = set()
FIXTURE = ROOT / "tests" / "fixtures" / "mcp_probe_server.py"
WS = _TMP_HOME / "ws"
OUT = _TMP_HOME / "probe-out"       # outside WS on purpose
WS.mkdir()
OUT.mkdir()
PY = sys.executable
CODEX = C.codex_executable()
ASK = ("Use the MCP tools from the 'cs-probe' server: call probe_read, then call probe_write with "
       "name='{n}.txt' and text='hello'. Use no other tools. Report each tool's exact result, or say "
       "exactly why a call was not made.")


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    PASSED += bool(ok)
    FAILED += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail[:500]) if detail and not ok else ''}", flush=True)


def tool(value):
    return getattr(value, "fn", value)


def call(fn, *a, **kw) -> str:
    try:
        return tool(fn)(*a, **kw)
    except PairError as exc:
        return f"ERROR: {exc}"


def send(name: str, message: str, timeout: float = 300):
    """pair_send_async + wait; returns (reply_text, SendResult|None, rendered)."""
    handle = call(S.pair_send_async, name, message)
    if handle.startswith("ERROR"):
        return handle, None, handle
    tid = next((l.split(":", 1)[1].strip() for l in handle.splitlines() if l.startswith("Async task:")), None)
    state = A.wait_for_task(tid, timeout) if tid else None
    if state is None or state.result is None:
        err = f"ERROR: task {getattr(state, 'status', '?')}: {getattr(state, 'error', '')}"
        return err, None, err
    return state.result.response or "", state.result, S._fmt_send_result(state.result)


def written(n: str) -> bool:
    return (OUT / f"{n}.txt").exists()


def remember_threads() -> None:
    for spec in R.load().pairs.values():
        if spec.backend == "codex" and spec.session_id:
            THREADS.add(spec.session_id)


# Claude: project-scope definition in the scratch workspace.
(WS / ".mcp.json").write_text(json.dumps({"mcpServers": {"cs-probe": {
    "command": PY, "args": [str(FIXTURE)], "env": {"PROBE_OUT_DIR": str(OUT)}}}}), encoding="utf-8")
print(f"(temp CLAUDE_HOME {_TMP_HOME})")
codex_registered = False
try:
    r = subprocess.run([CODEX, "mcp", "add", "cs-probe", "--env", f"PROBE_OUT_DIR={OUT}", "--", PY, str(FIXTURE)],
                       capture_output=True, text=True, timeout=60)
    codex_registered = r.returncode == 0
    check("registered cs-probe with codex (temporary)", codex_registered, r.stderr or r.stdout)

    print("\n=== 1. default: a pair with no connectors gets none ===")
    out = call(S.pair_create, name="cn-none", purpose="no connectors", model="haiku", permission_mode="auto", cwd=str(WS))
    check("create", "Created 'cn-none'" in out, out)
    reply, res, shown = send("cn-none", ASK.format(n="none"))
    check("no connector tools available by default", not written("none") and "PROBE-READ-OK" not in reply, shown)

    print("\n=== 2. pair itself is never loadable ===")
    out = call(S.pair_create, name="cn-pair", purpose="recursion", model="haiku", cwd=str(WS), mcp_whitelist=["pair"])
    check("mcp_whitelist=['pair'] refused", out.startswith("ERROR"), out)

    print("\n=== 3. Claude read-only: connector loaded, every call denied and reported ===")
    out = call(S.pair_create, name="cn-claude", purpose="claude connector", model="haiku", permission_mode="read-only",
               cwd=str(WS), mcp_whitelist=["cs-probe", "does-not-exist"])
    check("create with one real and one unknown connector", "Created 'cn-claude'" in out, out)
    check("unknown connector warned, not activated", "does-not-exist" in out and "not activated" in out, out)
    reply, res, shown = send("cn-claude", ASK.format(n="claude-ro"))
    check("no write at read-only", not written("claude-ro"), shown)
    check("denials reported back", bool(res) and bool(res.permission_denials), shown)

    print("\n=== 4. Claude: pair_update to auto applies on the next send (no pair_clear) ===")
    out = call(S.pair_update, "cn-claude", permission_mode="auto")
    check("update to auto", "ERROR" not in out, out)
    reply, res, shown = send("cn-claude", ASK.format(n="claude-auto"))
    check("read ran at auto", "PROBE-READ-OK" in reply, shown)
    check("write ran at auto", written("claude-auto"), shown)

    print("\n=== 5. Claude: adding a connector later via pair_update (no pair_clear) ===")
    out = call(S.pair_update, "cn-none", mcp_whitelist=["cs-probe"])
    check("update whitelist", "ERROR" not in out, out)
    reply, res, shown = send("cn-none", ASK.format(n="late"))
    check("connector usable after update", "PROBE-READ-OK" in reply and written("late"), shown)

    if codex_registered:
        print("\n=== 6. Codex read-only: read-only-annotated tool runs, write refused ===")
        out = call(S.pair_create, name="cn-codex", purpose="codex connector", model="luna", effort="low",
                   permission_mode="read-only", cwd=str(WS), mcp_whitelist=["cs-probe"])
        remember_threads()
        check("create codex pair with connector", "Created 'cn-codex'" in out, out)
        reply, res, shown = send("cn-codex", ASK.format(n="codex-ro"))
        check("read ran at read-only", "PROBE-READ-OK" in reply, shown)
        check("write refused at read-only", not written("codex-ro"), shown)

        print("\n=== 7. Codex auto: write goes to the reviewer ===")
        call(S.pair_update, "cn-codex", permission_mode="auto")
        reply, res, shown = send("cn-codex", ASK.format(n="codex-auto"))
        check("write ran at auto (reviewer approved)", written("codex-auto"), shown)

        print("\n=== 8. Claude cloud connector path (read-only: nothing can run), then handoff ===")
        # Read-only FIRST: at read-only Claude pre-approves no connector tools,
        # so the real Gmail connector can be loaded without any call running.
        out = call(S.pair_update, "cn-claude", permission_mode="read-only",
                   mcp_whitelist=["cs-probe", "claude_ai_Gmail"])
        check("update to read-only with a cloud connector", "ERROR" not in out, out)
        reply, res, shown = send("cn-claude", "Do not call any tools. List the MCP server names whose tools you "
                                              "can see (tool names look like mcp__<server>__<tool>), one per line.")
        check("cloud connector visible alongside the local one",
              "cs-probe" in reply.replace("cs_probe", "cs-probe") and "gmail" in reply.lower(), shown)
        check("pair's own tools never visible", "mcp__pair__" not in reply, shown)
        out = call(S.pair_handoff, "cn-claude", model="luna", effort="low", permission_mode="read-only",
                   probe="Reply with exactly: HANDOFF-OK")
        remember_threads()
        new = R.load().pairs.get("cn-claude-codex")
        check("handoff created the pair", new is not None, out)
        check("cs-probe carried over", bool(new) and "cs-probe" in (new.mcp_whitelist or []),
              str(new and new.mcp_whitelist))
        check("Gmail warned as not available", "claude_ai_Gmail" in out and "not activated" in out, out)
        reply, res, shown = send("cn-claude-codex", ASK.format(n="handoff-ro"))
        check("carried connector works on the new pair (read)", "PROBE-READ-OK" in reply, shown)
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
    if codex_registered:
        r = subprocess.run([CODEX, "mcp", "remove", "cs-probe"], capture_output=True, text=True, timeout=60)
        print("    codex mcp remove cs-probe:", (r.stdout or r.stderr).strip()[:120])
    left = subprocess.run([CODEX, "mcp", "list", "--json"], capture_output=True, text=True, timeout=60).stdout
    check("cs-probe no longer in the Codex config", "cs-probe" not in left)
    for tid in sorted(THREADS):
        r = subprocess.run([CODEX, "delete", "--force", tid], capture_output=True, text=True, timeout=60)
        print(f"    codex delete {tid[:8]}: {(r.stdout or r.stderr).strip()[:80]}")
    proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
    if proj.exists():
        import shutil
        shutil.rmtree(proj, ignore_errors=True)
    print(f"  (temp CLAUDE_HOME was {_TMP_HOME})")
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
