"""Live harness for v0.18.0 API-key billing (costs usage: Haiku, low effort).

Default (fake key): a pair is created on the plan, switched to API billing with
a FAKE key in a temp CLAUDE_HOME, and sent one turn through the warm runtime —
the turn must come back "Invalid API key" (the key reached the CLI despite an
inherited CLAUDE_CODE_ENTRYPOINT) with the transcript labelled ``sdk-cli``.
Then the same pair goes back to the plan and must answer. Spends ~3 small
Haiku turns of plan usage, no API credits.

``--real``: the same, with the key from ``~/.claude/pairs/api_key`` copied into
the temp home (never printed). The API turn must ANSWER, cost something, and not
hit "Credit balance too low" — which also shows the run is covered by the
credits. Spends one Haiku turn of API credits.

Isolation: temp CLAUDE_HOME with a ``projects`` junction to the real
``~/.claude/projects``; a scratch cwd under it. Cleanup forgets the pair,
removes its project dir, removes the junction itself (never its target), then
the temp tree.

Run:  PYTHONIOENCODING=utf-8 python -u tests/smoke_live_billing.py [--real]
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REAL = "--real" in sys.argv
_REAL_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
_TMP_HOME = Path(tempfile.mkdtemp(prefix="cs-live-billing-"))
os.environ["CLAUDE_HOME"] = str(_TMP_HOME)  # before importing the package
_link = _TMP_HOME / "projects"
if os.name == "nt":
    subprocess.run(["cmd", "/c", "mklink", "/J", str(_link), str(_REAL_HOME / "projects")],
                   check=True, capture_output=True)
else:
    os.symlink(_REAL_HOME / "projects", _link)

from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as runtime_mod  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared.adapters import claude as CL  # noqa: E402
from claude_squared.cli_paths import encode_cwd_for_project  # noqa: E402

PASSED = FAILED = 0
NAME = "billing-live-" + secrets.token_hex(3)
WS = _TMP_HOME / "ws"
WS.mkdir()


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    PASSED += bool(ok)
    FAILED += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))


def tool(fn):
    return getattr(fn, "fn", fn)


def send(message: str):
    runner = S._build_send_runner(NAME, message, hard_timeout_seconds=300, override_model=None,
                                  override_effort=None, override_permission_mode=None)
    return runner(None)


key_path = CL.api_key_path()
KEY = "<unset>"

try:
    # Inside the try: a missing real key file must still reach the cleanup.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if REAL:
        shutil.copyfile(_REAL_HOME / "pairs" / "api_key", key_path)
    else:
        key_path.write_text("sk-ant-api03-PROBE" + secrets.token_hex(16), encoding="utf-8")
    KEY = CL.read_api_key()
    print(f"=== {'REAL' if REAL else 'FAKE'} key; inherited CLAUDE_CODE_ENTRYPOINT="
          f"{os.environ.get('CLAUDE_CODE_ENTRYPOINT')!r} ===")
    created = tool(S.pair_create)(NAME, purpose="billing harness", model="haiku", effort="low",
                                  permission_mode="read-only", cwd=str(WS))
    check("pair created on the plan", R.get_pair(NAME).billing == "plan", created)
    switched = tool(S.pair_billing)(NAME, "api")
    check("pair_billing switched it to api", R.get_pair(NAME).billing == "api", switched)
    check("the switch result shows only the key's last four", KEY not in switched and KEY[-4:] in switched)

    print("=== one turn on the key ===")
    r = send("Reply with exactly: PONG")
    reply = r.response.replace(KEY, "<KEY>")
    if REAL:
        check("the API turn answered", "PONG" in reply and not r.safety_kind, f"{reply[:160]!r} {r.safety_kind}")
        check("it cost something", float(r.cost_usd or 0) > 0, str(r.cost_usd))
        check("not out of credits (the run is covered)", r.safety_kind != "credit_exhausted", reply[:160])
    else:
        check("the fake key reached the CLI (Invalid API key)", "invalid api key" in reply.lower(), reply[:160])
        check("rendered as a rejected key", r.safety_kind == "api_key_rejected", str(r.safety_kind))
    check("the footer shows the API cost", "API $" in S._fmt_send_result(r))
    spec = R.get_pair(NAME)
    jsonl = CL.ClaudeAdapter().transcript_path(spec)
    lines = jsonl.read_text(encoding="utf-8").splitlines() if jsonl and jsonl.exists() else []
    tagged = [ln for ln in lines if '"entrypoint":"' in ln]
    last = tagged[-1].split('"entrypoint":"')[1].split('"')[0] if tagged else None
    check("the transcript is labelled sdk-cli", last == "sdk-cli", str(last))
    leaks = [str(p) for p in [R.registry_path(), *(_TMP_HOME / "pairs").rglob("*.log"),
                              *([jsonl] if jsonl else [])]
             if p.is_file() and p != key_path and KEY in p.read_text(encoding="utf-8", errors="replace")]
    check("the key is in no registry, log or transcript", not leaks, ", ".join(leaks))
    check("pair_info masks the key", KEY not in tool(S.pair_info)(NAME, verbose=True))

    print("=== back on the plan ===")
    tool(S.pair_update)(NAME, billing="plan")
    r2 = send("Reply with exactly: PONG")
    check("the same pair answers on the plan", "PONG" in r2.response, r2.response[:160])
    check("no API footer on the plan", "API $" not in S._fmt_send_result(r2))
finally:
    print("\n=== cleanup ===")
    try:
        runtime_mod.registry().stop_all()
        if NAME in R.load().pairs:
            tool(S.pair_forget)(NAME, archive=False)
    except Exception as exc:
        check("pair cleanup", False, f"{type(exc).__name__}: {exc}")
    proj = _REAL_HOME / "projects" / encode_cwd_for_project(str(WS))
    if proj.exists():
        shutil.rmtree(proj, ignore_errors=True)
    check("the scratch project dir is gone", not proj.exists())
    os.rmdir(_link)  # the junction / symlink itself, never its target
    check("junction removed, real projects intact", not _link.exists() and (_REAL_HOME / "projects").is_dir())
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
