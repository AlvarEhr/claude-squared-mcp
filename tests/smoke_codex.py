"""v0.13.0 smoke: the Codex backend + the backend-neutral vocabulary.

Two halves:

  UNIT (default) — no CLI or network; all state uses temporary fixtures:
    permission aliases/translation, per-backend effort coercion, model-id
    helpers, ``[1m]``/legacy-spelling normalization on PairSpec, the Codex
    models-cache/config readers, exec argument construction with a mock binary,
    event → log/SendResult parsing on recorded probe events, and v3 migration
    of a synthetic legacy registry. No user cache or registry is read.

  LIVE (``--live``) — drives the server tool functions end-to-end against the
    real codex CLI (``gpt-5.6-luna`` at low effort, scratch cwd) in a TEMP
    CLAUDE_HOME so the user's registry/logs are untouched. Costs ~12 luna calls
    on the ChatGPT plan. Exercises: create (workspace + auto levels), send +
    footer, in-workspace write (T-N log lines), out-of-workspace denial →
    PAIR HANDOFF, guardian review, status/info/transcript/context/log,
    rewind_points + rewind (the MANGO/PAPAYA test), fork, async send +
    pair_stop tree-kill + resume, clear (new thread id), update semantics,
    compact/invoke hard-errors, adopt, forget.

Run:  PYTHONIOENCODING=utf-8 python -u tests/smoke_codex.py [--live]
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

LIVE = "--live" in sys.argv
_SESSION_TEMP = tempfile.TemporaryDirectory(prefix="cs-codex-smoke-", ignore_cleanup_errors=True)
_TMP_HOME = Path(_SESSION_TEMP.name)
# Set before package imports, including when this script is run directly.
os.environ["CLAUDE_HOME"] = str(_TMP_HOME)
if LIVE:
    # Codex keeps using its actual home (auth + cache) in the explicit live half.
    os.environ["CLAUDE_PAIR_SYNC_CAP_SECONDS"] = "300"

from claude_squared import codex_models as CM  # noqa: E402
from claude_squared import models as M  # noqa: E402
from claude_squared.adapters import claude as CA  # noqa: E402
from claude_squared.adapters import codex as CX  # noqa: E402
from claude_squared.adapters.claude import ClaudeAdapter  # noqa: E402
from claude_squared.adapters.codex import CodexAdapter, config_readd_args  # noqa: E402


@contextmanager
def offline_fixtures():
    """Patch filesystem roots and CLI discovery for the unit half only."""
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cs-codex-unit-")))
        home = root / "codex"
        home.mkdir()
        levels = ["low", "medium", "high", "xhigh", "max"]
        models = []
        for tier in ("sol", "terra", "luna", "astra"):
            models.append({
                "slug": f"gpt-{'6' if tier == 'astra' else '5.6'}-{tier}",
                "visibility": "list", "priority": len(models) + 1,
                "supported_reasoning_levels": [
                    {"effort": level} for level in levels + ([] if tier == "luna" else ["ultra"])
                ],
                "context_window": 272_000, "max_context_window": 872_000,
                "effective_context_window_percent": 95,
            })
        models.append({"slug": "gpt-7-sol", "visibility": "hide"})
        models.append({"slug": "gpt-7-mini", "visibility": "list", "upgrade": {"model": "gpt-5.6-luna"}})
        (home / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")
        (home / "config.toml").write_text(
            'web_search = "cached"\nnotify = ["should-not-run"]\n'
            '[windows]\nsandbox = "elevated"\n'
            '[mcp_servers.should_not_inherit]\ncommand = "should-not-run"\n',
            encoding="utf-8",
        )
        stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(root / "claude")}))
        # codex.py imports codex_home by value, so patch both lookup sites.
        # Never change CODEX_HOME: --live must keep the user's actual auth/store.
        stack.enter_context(patch.object(CM, "codex_home", return_value=home))
        stack.enter_context(patch.object(CX, "codex_home", return_value=home))
        stack.enter_context(patch.object(CX, "codex_executable", return_value=str(root / "codex-fixture")))
        stack.enter_context(patch.object(CA, "_cli_permission_choices", return_value=("manual",)))
        spawn = stack.enter_context(patch.object(
            subprocess, "Popen", side_effect=AssertionError("Offline Codex checks must not launch processes")
        ))
        yield root
        spawn.assert_not_called()

passed = 0
failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail[:300]}" if detail else ""))


# ===========================================================================
# UNIT
# ===========================================================================

def run_unit_checks(fixture_root: Path) -> None:
    print("=== permission vocabulary ===")
    for alias, level in [("bypassPermissions", "unrestricted"), ("acceptEdits", "workspace"),
                         ("default", "read-only"), ("dontAsk", "read-only"), ("plan", "plan"),
                         ("auto", "auto"), ("READ-ONLY", "read-only"), ("workspace-write", "workspace"),
                         ("approve-for-me", "auto"), ("danger-full-access", "unrestricted")]:
        check(f"{alias!r} -> {level}", M.normalize_permission(alias) == level)
    check("None -> default 'auto'", M.normalize_permission(None) == "auto")
    try:
        M.normalize_permission("bogus")
        check("bogus raises", False)
    except ValueError:
        check("bogus raises", True)
    check("claude native map", ClaudeAdapter.native_permission("workspace") == "acceptEdits"
          and ClaudeAdapter.native_permission("bypassPermissions") == "bypassPermissions")
    check("codex auto -> --approve-for-me", CodexAdapter.permission_args("auto") == ["--approve-for-me"])
    check("codex plan -> read-only sandbox", CodexAdapter.permission_args("plan") == ["-s", "read-only"])
    check("codex unrestricted -> bypass flag",
          CodexAdapter.permission_args("unrestricted") == ["--dangerously-bypass-approvals-and-sandbox"])
    check("codex native override", CodexAdapter.permission_args("auto", {"sandbox": "danger-full-access", "approval": "none"})
          == ["-s", "danger-full-access"])
    check("no -a flag ever emitted", all("-a" not in CodexAdapter.permission_args(l) for l in M.PERMISSION_LEVELS))

    print("\n=== effort ===")
    check("default effort is high", M.DEFAULT_EFFORT == "high" and M.default_effort_for_model("opus") == "high")
    check("haiku -> None", M.default_effort_for_model("haiku") is None)
    check("sonnet xhigh -> high", M.coerce_effort_for_model("claude-sonnet-5", "xhigh")[0] == "high")
    check("opus max ok", M.coerce_effort_for_model("claude-opus-5", "max") == ("max", None))
    check("unknown effort coerces (no registry poisoning)", M.coerce_effort_for_model("opus", "bogus")[0] == "high")
    check("codex fallback set includes ultra when cache absent",
          "ultra" in M.EFFORT_LEVELS_CODEX_FALLBACK)

    print("\n=== model-id helpers ===")
    check("infer_backend gpt", M.infer_backend("gpt-5.6-sol") == "codex")
    check("infer_backend alias", M.infer_backend("sol") == "codex" and M.infer_backend("astra") == "codex")
    check("infer_backend claude", M.infer_backend("opus") == "claude" and M.infer_backend("claude-fable-5[1m]") == "claude")
    check("parse_codex_model", M.parse_codex_model("gpt-5.6-luna") == ((5, 6), "luna")
          and M.parse_codex_model("gpt-6-astra") == ((6,), "astra") and M.parse_codex_model("gpt-5.5") == ((5, 5), None)
          and M.parse_codex_model("codex-auto-review") is None)
    check("pinned vs floating", M.is_pinned_model("gpt-5.6-sol") and not M.is_pinned_model("sol")
          and M.is_pinned_model("claude-opus-5") and not M.is_pinned_model("opus"))
    check("short labels", M.short_model_label("gpt-5.6-luna") == "luna" and M.short_model_label("claude-opus-5") == "opus"
          and M.short_model_label("gpt-5.5") == "gpt-5.5")
    check("split_model_tier", M.split_model_tier("claude-opus-5[1m]") == ("claude-opus-5", "1m")
          and M.split_model_tier("opus") == ("opus", "default"))
    check("premium astra", M.premium_model_note("astra") is not None and M.premium_model_note("gpt-6-astra") is not None)
    check("premium fable still", M.premium_model_note("claude-fable-5") is not None)
    check("not premium sol/opus", M.premium_model_note("gpt-5.6-sol") is None and M.premium_model_note("opus") is None)
    check("parse_model_id silent on codex", M.parse_model_id("gpt-5.6-sol") == (None, ()))
    check("context_window aliases", M.normalize_context_window("1M") == "1m" and M.normalize_context_window("[1m]") == "1m"
          and M.normalize_context_window(None) == "default" and M.normalize_context_window("standard") == "default")

    print("\n=== PairSpec normalization (registry safety net) ===")
    s = M.PairSpec(name="a", session_id="s", model="claude-opus-5[1m]", permission_mode="bypassPermissions", effort="xhigh")
    check("[1m] moves into context_window", s.model == "claude-opus-5" and s.context_window == "1m")
    check("legacy permission normalized", s.permission_mode == "unrestricted" and s.backend == "claude")
    check("cli_model re-appends [1m]", ClaudeAdapter.cli_model(s) == "claude-opus-5[1m]")
    s2 = M.PairSpec(name="b", session_id="s", model="gpt-5.6-luna", permission_mode="acceptEdits", effort="high")
    check("codex backend inferred", s2.backend == "codex" and s2.permission_mode == "workspace")
    s3 = M.PairSpec(name="c", session_id="s", model="haiku", effort="xhigh")
    check("haiku effort -> None", s3.effort is None)
    check("default effort on spec", M.PairSpec(name="d", session_id="s").effort == "high")
    check("SendResult cost nullable", M.SendResult(name="x", response="", session_id="s", model_used="m",
                                                    cost_usd=None, duration_ms=1).cost_usd is None)

    print("\n=== synthetic codex models cache ===")
    check("fixture cache is readable", CM.load_models_cache() is not None)
    slug, why = CM.codex_default_model()
    check("default excludes premium, hidden and retiring newer models", slug == "gpt-5.6-sol", why)
    for alias in ("sol", "terra", "luna"):
        r, note = CM.resolve_codex_model(alias)
        check(f"alias {alias} resolves", r == f"gpt-5.6-{alias}")
    r, note = CM.resolve_codex_model("gpt-9-nope")
    check("unlisted slug gets a note, not an error", r == "gpt-9-nope" and note and "NOT listed" in note)
    check("context windows apply effective %", CM.context_windows(slug) == (258_400, 828_400))
    check("supported efforts read", "ultra" in CM.supported_efforts(slug))
    check("luna unsupported effort is coerced", M.coerce_effort_for_model("luna", "ultra")[0] == "max")
    with patch.object(CM, "models_cache_path", return_value=fixture_root / "missing.json"):
        check("missing cache returns None", CM.load_models_cache() is None)
        check("missing cache cannot choose a default", CM.codex_default_model()[0] is None)
        check("explicit slug works without cache", CM.resolve_codex_model("gpt-5.6-luna") == ("gpt-5.6-luna", None))
        check("missing cache retains effort fallback", "ultra" in M._allowed_efforts("gpt-5.6-sol", "codex"))

    print("\n=== synthetic config isolation ===")
    readded = config_readd_args()
    check("windows sandbox re-added", 'windows.sandbox="elevated"' in readded)
    check("web search re-added", 'web_search="cached"' in readded)
    check("MCP servers and hooks excluded", not any("should-not-run" in arg or "should_not_inherit" in arg for arg in readded))

    print("\n=== exec args ===")
    workspace = str(fixture_root / "workspace with spaces")
    extra_dir = str(fixture_root / "extra")
    spec = M.PairSpec(name="t", backend="codex", session_id="x", model="luna", effort="high",
                      permission_mode="auto", context_window="1m", cwd=workspace,
                      extra_dirs=[extra_dir], backend_options={"config": {"foo.bar": 1}, "args": ["--color", "never"]})
    args = CodexAdapter()._exec_args(spec, model=None, effort=None, permission_mode=None)  # noqa: SLF001
    joined = " ".join(args)
    check("exec + --json + skip-git + -C", args[1:6] == ["exec", "--json", "--skip-git-repo-check", "-C", workspace])
    check("--ignore-user-config + thread-source", "--ignore-user-config --thread-source claude-squared" in joined)
    check("model + effort re-passed every call", "-m gpt-5.6-luna" in joined and 'model_reasoning_effort="high"' in joined)
    _m = re.search(r"model_auto_compact_token_limit=(\d+)", joined)
    check("1m config pair (compact threshold below the clamped window)",
          "model_context_window=1000000" in joined and _m is not None and 0 < int(_m.group(1)) <= 900_000)
    try:
        CodexAdapter.permission_args("auto", {"sandbox": "read-only"})
        check("sandbox override refused at auto (CLI rejects -s with --approve-for-me)", False)
    except ValueError as e:
        check("sandbox override refused at auto (CLI rejects -s with --approve-for-me)", "approve-for-me" in str(e))
    check("auto -> --approve-for-me", "--approve-for-me" in args)
    check("extra_dirs -> --add-dir", args[args.index("--add-dir") + 1] == extra_dir)
    check("backend_options.config + args", "foo.bar=1" in joined and args[-2:] == ["--color", "never"])
    if os.name == "nt" and any("windows.sandbox" in a for a in config_readd_args()):
        check("windows.sandbox re-added under --ignore-user-config", any("windows.sandbox" in a for a in args))
    check("mock executable used", args[0] == str(fixture_root / "codex-fixture"))

    print("\n=== event parsing on recorded probe shapes ===")
    _ad = CodexAdapter()
    from claude_squared.runtime import ToolCounter  # noqa: E402
    _ctr = ToolCounter(index_path=None)
    _tags: dict = {}
    ev_started = {"type": "item.started", "item": {"id": "item_1", "type": "command_execution",
                  "command": '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "Set-Content x"',
                  "status": "in_progress"}}
    ev_done = {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
               "command": "…", "aggregated_output": "ok\nline2", "exit_code": 0, "status": "completed"}}
    ev_fc = {"type": "item.started", "item": {"id": "item_2", "type": "file_change",
             "changes": [{"path": str(fixture_root / "inside.txt"), "kind": "add"}], "status": "in_progress"}}
    ev_msg = {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "Done."}}
    ev_turn = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 5, "reasoning_output_tokens": 1}}
    l1 = _ad._format_event(ev_started, _ctr, _tags)  # noqa: SLF001
    l2 = _ad._format_event(ev_done, _ctr, _tags)  # noqa: SLF001
    l3 = _ad._format_event(ev_fc, _ctr, _tags)  # noqa: SLF001
    l4 = _ad._format_event(ev_msg, _ctr, _tags)  # noqa: SLF001
    l5 = _ad._format_event(ev_turn, _ctr, _tags)  # noqa: SLF001
    check("tool_use line tagged T-1 with wrapper stripped", l1 and "[T-1] [tool_use] command_execution(Set-Content x)" in l1[0], str(l1))
    check("tool_result reuses T-1 + exit code", l2 and "[T-1] [tool_result] exit 0 ok / line2" in l2[0], str(l2))
    check("file_change gets T-2", l3 and "[T-2] [tool_use] file_change(add inside.txt)" in l3[0], str(l3))
    check("agent_message -> [text]", l4 and "[text] Done." in l4[0])
    check("completed turn is logged", l5 and "=== TURN COMPLETED (" in l5[0] and l5[0].endswith("tokens) ==="))

    _spec = M.PairSpec(name="u", backend="codex", session_id="01a07d5a-032f-7212-a219-f93df4ab9e8b", model="gpt-5.6-luna")
    _events = [{"type": "thread.started", "thread_id": _spec.session_id}, ev_msg, ev_turn,
               {"type": "_log_scope", "log_path": "x/main.log", "start_line": 1, "end_line": 3}]
    _res = _ad._build_send_result(_spec, _events, "", 0, 1234, time.time(), model_used="gpt-5.6-luna",  # noqa: SLF001
                                  permission_level="workspace")
    check("SendResult basics", _res.response == "Done." and _res.cost_usd is None and _res.backend == "codex"
          and _res.log_line_start == 1 and _res.log_line_end == 3 and _res.duration_ms == 1234)
    check("context window fallback from cache", _res.context is not None and _res.context.tokens_max >= 200_000)
    _den = _ad._build_send_result(  # noqa: SLF001
        _spec, [ev_msg, ev_turn], "2026-09-07T19:23:36Z ERROR codex_core::tools::router: error=patch rejected: "
        "writing outside of the project; rejected by user approval settings\n", 0, 1, time.time(),
        model_used="gpt-5.6-luna", permission_level="workspace")
    check("stderr denial -> PermissionDenial", len(_den.permission_denials) == 1 and _den.permission_denials[0].tool_name == "apply_patch")
    _deg = _ad._build_send_result(  # noqa: SLF001
        _spec, [ev_msg, ev_turn], "ERROR patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings\n",
        0, 1, time.time(), model_used="gpt-5.6-luna", permission_level="workspace")
    check("read-only degrade note", any("READ-ONLY" in n for n in _deg.notes))
    _fail = _ad._build_send_result(  # noqa: SLF001
        _spec, [{"type": "item.completed", "item": {"id": "e", "type": "error", "message": "The 'gpt-9' model is not supported when using Codex with a ChatGPT account."}},
                {"type": "turn.failed", "error": {"message": "invalid_request_error"}}], "", 1, 1, time.time(),
        model_used="gpt-9", permission_level="workspace")
    check("model-unavailable classified", _fail.safety_kind == "model_unavailable" and "(no reply" in _fail.response)

    print("\n=== synthetic registry v3 migration ===")
    from claude_squared import registry as R
    raw_registry = {
        "version": 2,
        "pairs": {
            "legacy-claude": {
                "session_id": "00000000-0000-4000-8000-000000000001",
                "model": "claude-opus-5[1m]", "permission_mode": "bypassPermissions",
                "effort": "xhigh", "purpose": "synthetic migration fixture",
            },
            "legacy-codex": {
                "session_id": "00000000-0000-4000-8000-000000000002",
                "model": "gpt-5.6-luna", "permission_mode": "acceptEdits", "effort": "high",
            },
        },
    }
    registry_path = R.registry_path()
    registry_path.write_text(json.dumps(raw_registry), encoding="utf-8")
    original_bytes = registry_path.read_bytes()
    reg = R.load()
    disk = json.loads(registry_path.read_text(encoding="utf-8"))
    check("version bumped to 3 in memory + on disk", reg.version == 3 and disk.get("version") == 3)
    check("every fixture pair still loads", set(reg.pairs) == set(raw_registry["pairs"]))
    check("legacy name inferred from key", reg.pairs["legacy-claude"].name == "legacy-claude")
    check("model tier migrated", reg.pairs["legacy-claude"].model == "claude-opus-5"
          and reg.pairs["legacy-claude"].context_window == "1m")
    check("permissions normalized", reg.pairs["legacy-claude"].permission_mode == "unrestricted"
          and reg.pairs["legacy-codex"].permission_mode == "workspace")
    check("backends inferred", reg.pairs["legacy-claude"].backend == "claude"
          and reg.pairs["legacy-codex"].backend == "codex")
    check("migration preserves original backup", registry_path.with_name("registry.v2.backup.json").read_bytes() == original_bytes)
    R.load()
    check("repeated load preserves backup", registry_path.with_name("registry.v2.backup.json").read_bytes() == original_bytes)


with offline_fixtures() as fixture_root:
    run_unit_checks(fixture_root)


# ===========================================================================
# LIVE
# ===========================================================================

if LIVE:
    print("\n=== LIVE: codex pairs end-to-end (luna/low) ===")
    from claude_squared import server as S  # noqa: E402
    from claude_squared.errors import PairError  # noqa: E402

    def T(tool):
        return getattr(tool, "fn", tool)

    def send(name, msg, **kw):
        return asyncio.run(T(S.pair_send)(name, msg, timeout_seconds=280, **kw))

    ws = Path(tempfile.mkdtemp(prefix="cs-codex-ws-"))
    outside = Path(tempfile.mkdtemp(prefix="cs-codex-outside-"))
    # Codex's workspace-write sandbox also allows the OS temp dirs — so put the
    # "outside" target somewhere that is genuinely outside: a sibling of C:\tmp.
    outside_dir = (Path("C:/tmp/cs-codex-outside") / Path(ws).name) if os.name == "nt" else outside
    outside_dir.mkdir(parents=True, exist_ok=True)

    out = T(S.pair_create)("cx", purpose="codex smoke", backend="codex", model="luna", effort="low",
                           permission_mode="acceptEdits", cwd=str(ws))
    print("  create ->", out.replace("\n", "\n            ")[:900])
    check("create headline names codex thread + neutral level",
          "codex thread" in out and "permission workspace" in out and "effort low" in out)
    check("alias note + context-window hint present",
          "floating alias" in out and "context_window='1m'" in out)
    check("legacy spelling note", "'acceptEdits' → neutral level 'workspace'" in out)
    spec = S.reg_mod.get_pair("cx")
    check("thread id recorded from CreateResult (not a placeholder)", spec.session_id and "pending" not in spec.session_id
          and len(spec.session_id) > 20, spec.session_id)
    check("rollout exists", CodexAdapter().session_exists(spec))

    r = send("cx", "Reply with exactly: PONG")
    print("  send ->", r.replace("\n", "\n          ")[:600])
    check("PONG reply", "PONG" in r)
    check("footer shows codex/luna + ctx", "codex/luna" in r and "% ctx (" in r)
    check("plan usage line", "plan usage:" in r)

    r = send("cx", "Create a file named inside.txt containing the text OK in the current working directory, "
                   "using a shell command. Then reply with exactly: WROTE")
    check("in-workspace write happened", (ws / "inside.txt").exists(), r[:300])
    log = T(S.pair_log)("cx", last_n=40)
    check("main.log has T-N tool lines + TURN marker", "[T-" in log and "=== TURN COMPLETED" in log, log[-400:])
    st = T(S.pair_status)("cx")
    check("status idle (codex)", "idle" in st and "codex" in st, st)

    tgt = outside_dir / "denied.txt"
    r = send("cx", f"Create a file at exactly this path containing OK: {tgt}. If the write is blocked, do not retry "
                   f"or work around it; just say BLOCKED.")
    print("  outside write ->", r.replace("\n", "\n                   ")[:700])
    check("outside write denied at workspace level", not tgt.exists())
    check("PAIR HANDOFF rendered with neutral remedy", "PAIR HANDOFF" in r and "unrestricted" in r, r[-600:])

    info = T(S.pair_info)("cx")
    check("info shows backend/thread/context_window", "backend: codex" in info and "context_window: default" in info, info)
    tr = T(S.pair_transcript)("cx", last_n=4)
    check("transcript reads rollout", "PONG" in tr or "WROTE" in tr, tr[:300])
    cx = T(S.pair_context)("cx")
    check("pair_context zero-inference", "**Tokens:**" in cx and "zero inference" in cx, cx)

    # rewind: MANGO then PAPAYA, rewind to before PAPAYA, ask.
    send("cx", "Remember the code word MANGO. Reply with exactly: noted")
    send("cx", "Also remember the code word PAPAYA. Reply with exactly: noted")
    pts = T(S.pair_rewind_points)("cx")
    print("  rewind points ->", pts[:500])
    check("rewind points listed", "rewind point(s)" in pts and "PAPAYA" in pts)
    n_papaya = None
    for line in pts.splitlines():
        if "PAPAYA" in line and line.strip().startswith("["):
            n_papaya = int(line.strip()[1:].split("]")[0])
    check("found PAPAYA point", n_papaya is not None)
    if n_papaya:
        rw = T(S.pair_rewind)("cx", to_point=n_papaya)
        print("  rewind ->", rw[:300])
        r = send("cx", "List every code word I asked you to remember, comma-separated, nothing else.")
        check("rewind took effect (MANGO only)", "MANGO" in r and "PAPAYA" not in r.split("[cx:")[0], r[:200])

    # manual compaction through the codex app-server (steering ignored with a note)
    cp = T(S.pair_compact)("cx", steering_prompt="keep the code words", timeout_seconds=280)
    print("  compact ->", cp.replace("\n", "\n              ")[:400])
    check("compact ran via app-server", "compacted" in cp and "app-server" in cp, cp)
    check("steering-ignored note present", "steering_prompt ignored" in cp, cp)
    r = send("cx", "Without re-reading anything: which code word did I ask you to remember? Reply with just the word.")
    check("summary retained after compaction (MANGO)", "MANGO" in r, r[:200])

    fk = T(S.pair_fork)("cx")
    print("  fork ->", fk[:200])
    check("fork created", "Forked 'cx'" in fk)
    fspec = S.reg_mod.get_pair("cx-fork")
    check("fork has its own thread id", fspec.session_id != spec.session_id and CodexAdapter().session_exists(fspec))
    r = send("cx-fork", "Which code word do you remember? Reply with just the word.")
    check("fork carries history", "MANGO" in r, r[:200])

    # async + stop mid-turn (tree-kill) + resume
    h = T(S.pair_send_async)("cx", "Run this shell command and wait for it to finish, then reply DONE: "
                                   "powershell -Command Start-Sleep -Seconds 90")
    time.sleep(8)
    st = T(S.pair_status)("cx")
    check("status shows running codex turn", "codex turn running" in st, st)
    stop = T(S.pair_stop)("cx")
    print("  stop ->", stop)
    check("stop tree-killed the turn", "tree-killed" in stop, stop)
    time.sleep(2)
    poll = T(S.pair_poll)("cx")
    check("task reported stopped/failed", "stopped" in poll or "failed" in poll, poll[:200])
    r = send("cx", "Reply with exactly: BACK")
    check("thread resumes after kill", "BACK" in r, r[:200])

    old_tid = S.reg_mod.get_pair("cx").session_id
    cl = T(S.pair_clear)("cx")
    check("clear rotates to the thread id codex reported", "Cleared 'cx'" in cl
          and S.reg_mod.get_pair("cx").session_id != old_tid
          and "pending" not in S.reg_mod.get_pair("cx").session_id, cl)

    up = T(S.pair_update)("cx", permission_mode="bypassPermissions")
    check("update normalizes legacy permission", "unrestricted" in up and S.reg_mod.get_pair("cx").permission_mode == "unrestricted", up)
    up = T(S.pair_update)("cx", context_window="1m")
    check("update context_window with usable-token note", "1m" in up and "usable tokens" in up, up)
    try:
        T(S.pair_update)("cx", model="opus")
        check("cross-backend model change refused", False)
    except PairError as e:
        check("cross-backend model change refused", "backend is fixed" in str(e))
    try:
        T(S.pair_invoke)("cx", "context")
        check("invoke hard-errors on codex", False)
    except Exception as e:
        check("invoke hard-errors on codex", "not available" in str(e))

    # auto level: escalation judged by the guardian
    out = T(S.pair_create)("cxa", backend="codex", model="luna", effort="low", permission_mode="auto", cwd=str(ws))
    tgt2 = outside_dir / "escalated.txt"
    r = send("cxa", f"Create a file at exactly this path containing OK: {tgt2}. It is outside the workspace so the "
                    f"sandbox will block a plain write; request the elevated permission ONCE for that single command. "
                    f"If denied, stop and say DENIED.")
    print("  auto/escalation ->", r.replace("\n", "\n                     ")[:600])
    check("guardian verdict surfaced", "guardian review:" in r, r)

    ad = T(S.pair_adopt)("cx-adopted", session_id=spec.session_id, model="luna", effort="low", cwd=str(ws))
    check("adopt existing thread", "Adopted codex thread" in ad, ad)

    for n in ("cx", "cx-fork", "cxa", "cx-adopted"):
        fg = T(S.pair_forget)(n)
        check(f"forget {n}", "Forgot" in fg, fg)
    print(f"  (temp CLAUDE_HOME was {os.environ['CLAUDE_HOME']})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
