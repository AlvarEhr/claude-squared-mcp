"""Live checks for per-pair MCP connectors on BOTH backends (costs a little usage).

Uses the throwaway server tests/fixtures/mcp_probe_server.py (registered as
"cs-probe-<random>", a fresh name per run:
probe_read is annotated read-only, probe_write writes a file OUTSIDE the pair
workspace). Claude sees it through a project-scope ``.mcp.json`` in the scratch
workspace — no change to your Claude config. Codex only sees servers in its
own config, so the run registers that unique name with ``codex mcp add`` —
refusing if it somehow exists, since ``add`` overwrites — and ALWAYS removes
exactly that entry at the end (the user approved this, 2026-09-10).

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
import uuid
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
SRV = f"cs-probe-{uuid.uuid4().hex[:8]}"  # unique per run: `codex mcp add` overwrites same-named entries
WS = _TMP_HOME / "ws"
OUT = _TMP_HOME / "probe-out"       # outside WS on purpose
WS.mkdir()
OUT.mkdir()
PY = sys.executable
CODEX = C.codex_executable()
ASK = (f"Use the MCP tools from the '{SRV}' server: call probe_read, then call probe_write with "
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


def read_ran(pair: str, since_line: int = 0) -> bool:
    """The read tool's result is in the pair's activity log — a model's final
    reply may restate only its last result, so the reply alone isn't proof."""
    try:
        lines = (_TMP_HOME / "pairs" / "logs" / pair / "main.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any("PROBE-READ-OK" in line and "tool_result" in line for line in lines[since_line:])


def log_len(pair: str) -> int:
    try:
        return len((_TMP_HOME / "pairs" / "logs" / pair / "main.log").read_text(
            encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def remember_threads() -> None:
    for spec in R.load().pairs.values():
        if spec.backend == "codex" and spec.session_id:
            THREADS.add(spec.session_id)


# Claude: project-scope definition in the scratch workspace.
(WS / ".mcp.json").write_text(json.dumps({"mcpServers": {SRV: {
    "command": PY, "args": [str(FIXTURE)], "env": {"PROBE_OUT_DIR": str(OUT)}}}}), encoding="utf-8")
print(f"(temp CLAUDE_HOME {_TMP_HOME})")
codex_registered = False
try:
    existing = json.loads(subprocess.run([CODEX, "mcp", "list", "--json"], capture_output=True, text=True,
                                         timeout=60, check=True).stdout)
    if any(row.get("name") == SRV for row in existing):
        raise RuntimeError(f"{SRV} already exists in the Codex config; refusing to overwrite it")
    r = subprocess.run([CODEX, "mcp", "add", SRV, "--env", f"PROBE_OUT_DIR={OUT}", "--", PY, str(FIXTURE)],
                       capture_output=True, text=True, timeout=60)
    codex_registered = r.returncode == 0
    check(f"registered {SRV} with codex (temporary)", codex_registered, r.stderr or r.stdout)

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
               cwd=str(WS), mcp_whitelist=[SRV, "does-not-exist"])
    check("create with one real and one unknown connector", "Created 'cn-claude'" in out, out)
    check("unknown connector warned, not activated", "does-not-exist" in out and "not activated" in out, out)
    reply, res, shown = send("cn-claude", ASK.format(n="claude-ro"))
    check("no write at read-only", not written("claude-ro"), shown)
    check("denials reported back", bool(res) and bool(res.permission_denials), shown)

    print("\n=== 4. Claude: pair_update to auto applies on the next send (no pair_clear) ===")
    out = call(S.pair_update, "cn-claude", permission_mode="auto")
    check("update to auto", "ERROR" not in out, out)
    mark = log_len("cn-claude")
    reply, res, shown = send("cn-claude", ASK.format(n="claude-auto"))
    check("read ran at auto", read_ran("cn-claude", mark), shown)
    check("write ran at auto", written("claude-auto"), shown)

    print("\n=== 5. Claude: adding a connector later via pair_update (no pair_clear) ===")
    out = call(S.pair_update, "cn-none", mcp_whitelist=[SRV])
    check("update whitelist", "ERROR" not in out, out)
    mark = log_len("cn-none")
    reply, res, shown = send("cn-none", ASK.format(n="late"))
    check("connector usable after update", read_ran("cn-none", mark) and written("late"), shown)

    if codex_registered:
        print("\n=== 6. Codex read-only: read-only-annotated tool runs, write refused ===")
        # medium, not low: at low effort Codex models sometimes answer without trying
        # an available MCP tool (measured 2026-09-10: low effort missed 5 of 15 runs, medium/high 0 of 14).
        out = call(S.pair_create, name="cn-codex", purpose="codex connector", model="luna", effort="medium",
                   permission_mode="read-only", cwd=str(WS), mcp_whitelist=[SRV])
        remember_threads()
        check("create codex pair with connector", "Created 'cn-codex'" in out, out)
        mark = log_len("cn-codex")
        reply, res, shown = send("cn-codex", ASK.format(n="codex-ro"))
        check("read ran at read-only", read_ran("cn-codex", mark), shown)
        check("write refused at read-only", not written("codex-ro"), shown)

        print("\n=== 7. Codex auto: write goes to the reviewer ===")
        call(S.pair_update, "cn-codex", permission_mode="auto")
        reply, res, shown = send("cn-codex", ASK.format(n="codex-auto"))
        check("write ran at auto (reviewer approved)", written("codex-auto"), shown)

        print("\n=== 8. Claude cloud connector path (read-only: nothing can run), then handoff ===")
        # Read-only FIRST: at read-only Claude pre-approves no connector tools,
        # so the real Gmail connector can be loaded without any call running.
        out = call(S.pair_update, "cn-claude", permission_mode="read-only",
                   mcp_whitelist=[SRV, "claude_ai_Gmail"])
        check("update to read-only with a cloud connector", "ERROR" not in out, out)
        # Deterministic exposure check: start a real Claude session with the
        # EXACT flags this pair gets and read its system/init tool list (a
        # model asked to list servers names every connected server, because
        # Claude Code tells it which exist — even ones whose tools are hidden).
        from claude_squared import connectors as N
        import shutil as _shutil
        spec = R.get_pair("cn-claude")
        flags = N.claude_args(spec.mcp_whitelist, spec.permission_mode, spec.cwd, spec.allowed_tools)
        probe = subprocess.run([_shutil.which("claude"), "-p", "Reply with exactly: OK", "--model", "haiku",
                                "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
                                *flags], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", cwd=str(WS), timeout=240)
        init_tools: list[str] = []
        for line in probe.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                init_tools = ev.get("tools") or []
                break
        prefixes = {t.split("__")[1] for t in init_tools if t.startswith("mcp__")}
        check("cloud connector and local server both exposed",
              N.tool_prefix(SRV) in prefixes and "claude_ai_Gmail" in prefixes, str(sorted(prefixes)))
        check("unselected servers' tools hidden (incl. pair)",
              prefixes <= {N.tool_prefix(SRV), "claude_ai_Gmail"}, str(sorted(prefixes)))
        out = call(S.pair_handoff, "cn-claude", model="luna", effort="medium", permission_mode="read-only",
                   probe="Reply with exactly: HANDOFF-OK")
        remember_threads()
        new = R.load().pairs.get("cn-claude-codex")
        check("handoff created the pair", new is not None, out)
        check("test server carried over", bool(new) and SRV in (new.mcp_whitelist or []),
              str(new and new.mcp_whitelist))
        check("Gmail warned as not available", "claude_ai_Gmail" in out and "not activated" in out, out)
        reply, res, shown = send("cn-claude-codex", ASK.format(n="handoff-ro"))
        log_path = _TMP_HOME / "pairs" / "logs" / "cn-claude-codex" / "main.log"

        def attempted() -> bool:
            try:
                return "mcp_tool_call" in log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False

        if not read_ran("cn-claude-codex"):
            # Diagnose instead of guessing: the imported history records these
            # same tools being DENIED on the Claude side, which can make the
            # model decline without trying. If a nudge makes it call the tool,
            # the connector IS loaded (history-induced hesitance, not a bug).
            print(f"    first attempt: tool call attempted={attempted()} — nudging once")
            reply, res, shown = send("cn-claude-codex", f"The '{SRV}' MCP tools ARE available to you in this "
                                                         "thread now, regardless of what earlier records say. "
                                                         "Call probe_read and report its exact result.")
        check("carried connector works on the new pair (read)", read_ran("cn-claude-codex"),
              f"tool call attempted={attempted()}; {shown}")
except Exception:
    import traceback
    traceback.print_exc()
    check("harness ran to completion", False, "crashed; see traceback above")
finally:
    print("\n=== cleanup ===")

    def cleanup_run(label: str, argv: list[str]) -> "subprocess.CompletedProcess | None":
        """One cleanup step; a timeout/OS error is counted and the rest continue."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")
            return None
        print(f"    {label}: {(r.stdout or r.stderr).strip()[:120]}")
        check(label, r.returncode == 0, r.stderr or r.stdout)
        return r

    try:
        remember_threads()
        for name in list(R.load().pairs):
            try:
                print("   ", call(S.pair_forget, name, archive=False))
            except Exception as exc:  # keep cleaning the rest
                check(f"forgot {name}", False, f"{type(exc).__name__}: {exc}")
        runtime_mod.registry().stop_all()
    except Exception as exc:
        check("pair cleanup", False, f"{type(exc).__name__}: {exc}")
    if codex_registered:
        cleanup_run(f"removed {SRV} from the Codex config", [CODEX, "mcp", "remove", SRV])
    listing = cleanup_run("Codex server list readable after cleanup", [CODEX, "mcp", "list", "--json"])
    if listing is not None and listing.returncode == 0:
        try:
            left = json.loads(listing.stdout)
            check(f"{SRV} no longer in the Codex config", all(row.get("name") != SRV for row in left))
        except ValueError as exc:
            check("Codex server list parses after cleanup", False, str(exc))
    for tid in sorted(THREADS):
        cleanup_run(f"deleted test thread {tid[:8]}", [CODEX, "delete", "--force", tid])
    proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
    if proj.exists():
        import shutil
        shutil.rmtree(proj, ignore_errors=True)
    print(f"  (temp CLAUDE_HOME was {_TMP_HOME})")
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
