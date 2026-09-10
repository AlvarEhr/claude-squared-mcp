"""Offline connector inventory, policy, spawn and handoff regressions."""

from __future__ import annotations

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-connectors-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name

from claude_squared import connectors as N, codex_models as CM, registry as R, server as S
from claude_squared.adapters import claude as CL, codex as CX
from claude_squared.models import CreateResult, PairSpec, SendResult
from claude_squared.errors import PairError
from claude_squared.runtime import PairRuntime, RuntimeRegistry, TurnLogScope


def tool(value):
    return getattr(value, "fn", value)


class ConnectorTests(unittest.TestCase):
    real_list_output = staticmethod(N._list_output)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory / "claude")}))
        self.config_path = self.directory / ".claude.json"
        self.stack.enter_context(patch.object(N, "claude_config_path", return_value=self.config_path))
        self.user_config = {"mcpServers": {
            "probe": {"command": "user-command", "args": ["server.py"]},
            "other": {"command": "other-command"},
            "pair": {"command": "do-not-run"},
            "Claude_Squared": {"command": "do-not-run-either"},
        }, "projects": {str(self.directory): {"mcpServers": {
            "probe": {"command": "local-command", "args": ["server.py"], "env": {"TOKEN": "fixture"}},
        }}}}
        self.config_path.write_text(json.dumps(self.user_config), encoding="utf-8")
        (self.directory / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "probe": {"command": "project-command"}, "project-only": {"url": "https://fixture.invalid/mcp"},
        }}), encoding="utf-8")
        self.claude_listing = (
            "Checking MCP server health...\nprobe: command: with colon - Connected\n"
            "plugin:github:github: command: with colon - Connected\n"
            "claude.ai Gmail: https://fixture.invalid:443/mcp - Connected\n"
            "claude.ai Calendar: https://fixture.invalid/calendar - Failed to connect\n"
            "PAIR: command - Connected\n"
        )
        self.codex_rows = [
            {"name": "probe", "enabled": True, "transport": {"type": "stdio", "command": "codex-command",
             "args": ["server.py"], "env": {"TOKEN": "fixture"}, "env_vars": [], "cwd": None},
             "auth_status": "unsupported", "tool_timeout_sec": 30},
            {"name": "claude_ai_Gmail", "enabled": True, "transport": {"type": "streamable_http",
             "url": "https://fixture.invalid/mcp", "http_headers": {"X-Fixture": "yes"},
             "bearer_token_env_var": "MCP_TOKEN"}},
            {"name": "disabled", "enabled": False, "transport": {"type": "stdio", "command": "disabled"}},
        ]
        self.listing = self.stack.enter_context(patch.object(N, "_list_output", side_effect=
            lambda backend, cwd: self.claude_listing if backend == "claude" else json.dumps(self.codex_rows)))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess.run")))
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected Popen")))
        self.stack.enter_context(patch.object(CL, "_claude_executable", return_value="fixture-claude"))
        self.stack.enter_context(patch.object(CL, "_cli_permission_choices", return_value=("manual", "auto", "plan")))
        self.stack.enter_context(patch.object(CX, "codex_executable", return_value="fixture-codex"))
        self.stack.enter_context(patch.object(CX, "config_readd_args", return_value=[]))
        home = self.directory / "codex"
        home.mkdir()
        for module in (CX, CM):
            self.stack.enter_context(patch.object(module, "codex_home", return_value=home))
        (home / "models_cache.json").write_text(json.dumps({"models": [{
            "slug": "gpt-5.6-sol", "visibility": "list", "context_window": 272_000,
            "max_context_window": 872_000, "effective_context_window_percent": 95,
            "supported_reasoning_levels": [{"effort": "high"}],
        }]}), encoding="utf-8")
        self.runtime = SimpleNamespace(get_or_none=Mock(return_value=None), evict=Mock())
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=self.runtime))
        with N._CACHE_LOCK:
            N._CACHE.clear()

    def spec(self, backend="claude", **changes):
        data = dict(name="source", session_id="source-id", model="opus" if backend == "claude" else "sol",
                    backend=backend, cwd=str(self.directory), mcp_whitelist=["probe"])
        data.update(changes)
        return PairSpec(**data)

    def result_event(self):
        return {"type": "result", "subtype": "success", "result": "Done", "session_id": "source-id",
                "duration_ms": 1, "usage": {"input_tokens": 1}}

    def stream_output(self, servers):
        return b"\n".join(json.dumps(event).encode() for event in [
            {"type": "system", "subtype": "init", "mcp_servers": servers}, self.result_event(),
        ])

    def test_claude_scope_precedence_and_cloud_plugin_names(self):
        inventory = {entry.name: entry for entry in N.claude_inventory(str(self.directory))}
        self.assertEqual(inventory["probe"].definition["command"], "local-command")
        self.assertEqual(inventory["other"].definition["command"], "other-command")
        self.assertIn("project-only", inventory)
        self.assertEqual(inventory["plugin:github:github"].kind, "plugin")
        self.assertEqual(inventory["claude.ai Gmail"].kind, "cloud")
        self.assertNotIn("Checking MCP server health...", inventory)
        self.assertEqual(N.tool_prefix("plugin:github:github"), "plugin_github_github")
        self.assertEqual(N.match("CLAUDE_AI_gMAIL", list(inventory.values())).name, "claude.ai Gmail")
        self.user_config["projects"] = {}
        self.config_path.write_text(json.dumps(self.user_config), encoding="utf-8")
        self.assertEqual(N.match("probe", N.claude_inventory(str(self.directory))).definition["command"], "project-command")
        (self.directory / ".mcp.json").unlink()
        self.assertEqual(N.match("probe", N.claude_inventory(str(self.directory))).definition["command"], "user-command")

    def test_codex_transport_definitions_and_disabled_server(self):
        inventory = N.codex_inventory(str(self.directory))
        probe = N.match("PROBE", inventory)
        self.assertEqual(probe.definition["env"], {"TOKEN": "fixture"})
        self.assertEqual(probe.definition["tool_timeout_sec"], 30)
        self.assertNotIn("type", probe.definition)
        self.assertNotIn("auth_status", probe.definition)
        self.assertEqual(N.match("claude.ai Gmail", inventory).definition["bearer_token_env_var"], "MCP_TOKEN")
        self.assertIsNone(N.match("disabled", inventory))

    def test_listing_cache_is_scoped_and_expires(self):
        with patch.object(subprocess, "run", return_value=SimpleNamespace(stdout="listed")) as run:
            self.assertEqual(self.real_list_output("claude", str(self.directory)), "listed")
            self.real_list_output("claude", str(self.directory))
            self.assertEqual(run.call_count, 1)
            key = ("claude", "fixture-claude", str(self.directory))
            N._CACHE[key] = (N._CACHE[key][0] - N.INVENTORY_TTL_SECONDS - 1, "stale")
            self.real_list_output("claude", str(self.directory))
            self.assertEqual(run.call_count, 2)
            self.real_list_output("codex", str(self.directory))
            self.assertEqual(run.call_args.args[0], ["fixture-codex", "mcp", "list", "--json"])
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_availability_failures_only_warn(self):
        with patch.object(subprocess, "run", side_effect=OSError("offline")):
            self.assertEqual(self.real_list_output("codex", str(self.directory)), "")
        with patch.object(N, "_list_output", return_value="not json"):
            self.assertEqual(N.codex_inventory(str(self.directory)), [])
            selected, notes = N.select(["missing"], "codex", str(self.directory))
        self.assertEqual(selected, [])
        self.assertEqual(notes, ["connector missing is not available and not activated"])

    def test_no_selection_keeps_strict_empty_defaults_without_discovery(self):
        with patch.object(N, "inventory", side_effect=AssertionError("discovery without opt-in")):
            self.assertEqual(N.claude_args(None, "auto"), [
                "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',
                "--disallowed-tools", "mcp__claude_ai_*", "--disallowed-tools", "mcp__pair__*",
                "--disallowed-tools", "mcp__Claude_Squared__*",
            ])
            self.assertEqual(N.codex_args([], "auto"), [])
            self.assertEqual(N.codex_startup_notes(None, None, "mcp: other failed"), [])

    def test_confirmed_policy_matrix_and_native_arguments(self):
        for level in ("read-only", "plan", "workspace", "auto", "unrestricted"):
            with self.subTest(level=level):
                cl = N.claude_args(["probe"], level, str(self.directory))
                self.assertEqual("--allowedTools" in cl, level == "auto")
                if level == "auto":
                    self.assertIn("mcp__probe", cl)
                self.assertIn("--strict-mcp-config", cl)
                self.assertEqual(json.loads(cl[cl.index("--mcp-config") + 1])["mcpServers"]["probe"]["command"], "local-command")
                cx = CX.CodexAdapter()._exec_args(self.spec("codex", permission_mode=level), model=None,
                                                effort=None, permission_mode=None)
                definition = next(arg for arg in cx if arg.startswith("mcp_servers="))
                mode = "approve" if level == "unrestricted" else "writes"
                self.assertIn(f'"default_tools_approval_mode" = "{mode}"', definition)
                self.assertIn('"command" = "codex-command"', definition)
                self.assertEqual("--approve-for-me" in cx, level == "auto")
                self.assertEqual("--dangerously-bypass-approvals-and-sandbox" in cx, level == "unrestricted")
                if level == "auto":
                    self.assertNotIn("-s", cx)

    def test_explicit_tool_rules_are_preserved_without_server_approval(self):
        args = CL.ClaudeAdapter()._common_create_args(self.spec(permission_mode="read-only",
            allowed_tools=["mcp__probe__probe_read", "Bash(git status)"]))
        self.assertIn("mcp__probe__probe_read", args)
        self.assertIn("Bash(git status)", args)
        self.assertNotIn("mcp__probe", args)
        self.assertEqual(args.count("--allowedTools"), 1)
        self.assertNotIn("--allowed-tools", args)

    def test_cloud_plugin_selection_disallows_every_other_inventory_server(self):
        args = N.claude_args(["probe", "claude_ai_Gmail"], "auto", str(self.directory))
        self.assertNotIn("--strict-mcp-config", args)
        self.assertIn("mcp__claude_ai_Gmail", args)
        self.assertNotIn("mcp__claude_ai_*", args)
        for prefix in ("other", "project-only", "plugin_github_github", "claude_ai_Calendar", "pair", "Claude_Squared"):
            self.assertIn(f"mcp__{prefix}__*", args)
        self.assertNotIn("mcp__probe__*", args)
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(set(config["mcpServers"]), {"probe"})
        plugin = N.claude_args(["plugin_github_github"], "auto", str(self.directory))
        self.assertNotIn("--strict-mcp-config", plugin)
        self.assertIn("mcp__plugin_github_github", plugin)

    def test_codex_literal_names_and_http_transport_survive_serialization(self):
        self.codex_rows[1]["name"] = "claude.ai Gmail"
        args = N.codex_args(["claude_ai_Gmail"], "workspace", str(self.directory))
        self.assertIn('"claude.ai Gmail" = {', args[1])
        self.assertIn('"bearer_token_env_var" = "MCP_TOKEN"', args[1])
        self.assertIn('"http_headers" = {"X-Fixture" = "yes"}', args[1])
        try:
            import tomllib
        except ImportError:
            return
        parsed = tomllib.loads(args[1])
        self.assertEqual(parsed["mcp_servers"]["claude.ai Gmail"]["default_tools_approval_mode"], "writes")

    def test_pair_servers_are_refused_at_create_and_update(self):
        for backend in ("claude", "codex"):
            spec = self.spec(backend)
            R.add_pair(spec)
            for name in ("pair", "PAIR", "Claude_Squared", "claude.squared"):
                with self.subTest(backend=backend, name=name):
                    with self.assertRaisesRegex(PairError, "recursive pair creation"):
                        tool(S.pair_create)("new", backend=backend, mcp_whitelist=[name])
                    with self.assertRaisesRegex(PairError, "recursive pair creation"):
                        tool(S.pair_update)("source", mcp_whitelist=[name])
            self.assertEqual(R.get_pair("source").mcp_whitelist, ["probe"])
            R.remove_pair("source")

    def test_codex_selection_is_one_inline_table_and_nothing_else(self):
        # No startup-wait override: measured unnecessary (see codex_args).
        args = N.codex_args(["probe"], "read-only", str(self.directory))
        self.assertEqual(len(args), 2)
        self.assertTrue(args[1].startswith("mcp_servers="))
        self.assertNotIn("required", args[1])

    def test_stored_pair_selection_keeps_the_pairs_allowed_tools(self):
        # Astra review catch: a stored ['pair'] must count as "no connectors",
        # so the pair's own allow-list still reaches the CLI on spawn.
        spec = PairSpec(name="legacy", session_id="s", cwd=str(self.directory),
                        mcp_whitelist=["pair"], allowed_tools=["Bash(git status)", "Read"])
        args = CL.ClaudeAdapter()._common_create_args(spec)
        self.assertIn("--allowed-tools", args)
        self.assertIn("Bash(git status) Read", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--allowedTools", args)
        self.assertEqual(N.claude_init_notes(["pair"], []), [])

    def test_stored_pair_selection_is_skipped_at_spawn_not_fatal(self):
        # A whitelist saved before 0.15.0 may contain 'pair' (it was silently
        # ignored then). Spawning must not fail over it: skip it with a note,
        # keep the pair MCP's tools disallowed, and grant it no allow rule.
        args = N.claude_args(["pair", "probe"], "auto", str(self.directory))
        self.assertIn("mcp__pair__*", args)
        rules = args[args.index("--allowedTools") + 1:] if "--allowedTools" in args else []
        self.assertIn("mcp__probe", rules)
        self.assertNotIn("mcp__pair", rules)
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertNotIn("pair", config["mcpServers"])
        selected, notes = N.select(["pair"], "claude", str(self.directory))
        self.assertEqual(selected, [])
        self.assertTrue(any("never loaded" in note for note in notes))
        self.assertEqual(N.claude_args(["pair"], "auto", str(self.directory))[:1], ["--strict-mcp-config"])
        codex = N.codex_args(["pair", "probe"], "read-only", str(self.directory))
        self.assertNotIn('"pair"', codex[1])

    def test_unknown_selections_are_stored_at_create_on_both_backends(self):
        def create(spec, **kwargs):
            return CreateResult(name=spec.name, session_id="created-id")
        with patch.object(CL.ClaudeAdapter, "create", side_effect=create), \
                patch.object(CX.CodexAdapter, "create", side_effect=create):
            for backend in ("claude", "codex"):
                report = tool(S.pair_create)(backend, backend=backend, mcp_whitelist=["missing"])
                self.assertIn("connector missing is not available and not activated", report)
                self.assertEqual(R.get_pair(backend).mcp_whitelist, ["missing"])

    def test_update_evicts_without_clear_and_can_disable_connectors(self):
        for backend in ("claude", "codex"):
            R.add_pair(self.spec(backend))
            self.runtime.evict.reset_mock()
            report = tool(S.pair_update)("source", mcp_whitelist=["missing"])
            self.assertIn("not available and not activated", report)
            self.assertNotIn("Run pair_clear", report)
            self.assertEqual(R.get_pair("source").mcp_whitelist, ["missing"])
            self.runtime.evict.assert_called_once_with("source")
            report = tool(S.pair_update)("source", purpose="still working")
            self.assertIn("connector missing is not available and not activated", report)
            tool(S.pair_update)("source", mcp_whitelist=[])
            self.assertIsNone(R.get_pair("source").mcp_whitelist)
            R.remove_pair("source")

    def test_cross_process_selection_change_replaces_warm_runtime(self):
        registry = RuntimeRegistry()
        old = SimpleNamespace(spec=self.spec(), is_alive=lambda: True, stop=Mock())
        registry._runtimes["source"] = old
        replacement = SimpleNamespace(is_alive=lambda: True)
        with patch.object(S.runtime_mod, "PairRuntime", return_value=replacement):
            result = registry.get_or_start(self.spec(mcp_whitelist=["other"]), CL.ClaudeAdapter())
        old.stop.assert_called_once()
        self.assertIs(result, replacement)

    def test_init_status_notes_match_aliases_and_preserve_denials(self):
        spec = self.spec(mcp_whitelist=["claude_ai_Gmail", "probe", "missing"])
        events = [{"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "claude.ai Gmail", "status": "connected"}, {"name": "probe", "status": "failed"}]},
            {"type": "system", "subtype": "init", "parent_tool_use_id": "child", "mcp_servers": []},
            dict(self.result_event(), permission_denials=[{"tool_name": "mcp__probe__probe_write"}])]
        CL.ClaudeAdapter._attach_mcp_init(events)
        result = CL.ClaudeAdapter()._build_send_result(spec, events[-1])
        self.assertEqual(len(result.notes), 2)
        self.assertIn("probe", result.notes[0])
        self.assertIn("missing", result.notes[1])
        self.assertEqual(result.permission_denials[0].tool_name, "mcp__probe__probe_write")

    def test_one_shot_override_reapplies_policy_and_reads_init(self):
        spec = self.spec(permission_mode="auto")
        response = SimpleNamespace(returncode=0, stdout=self.stream_output([{"name": "probe", "status": "failed"}]), stderr=b"")
        with patch.object(subprocess, "run", return_value=response) as run, \
                patch.object(CL.ClaudeAdapter, "session_exists", return_value=True):
            result = CL.ClaudeAdapter().send(spec, "hello", permission_mode="read-only")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", args)
        self.assertIn("--mcp-config", args)
        self.assertIn("AskUserQuestion", args)
        self.assertNotIn("--allowedTools", args)
        self.assertIn("failed", result.notes[0])

    def test_stream_json_invoke_and_create_surface_startup_notes(self):
        spec = self.spec()
        response = SimpleNamespace(returncode=0, stdout=self.stream_output([]), stderr=b"")
        with patch.object(subprocess, "run", return_value=response):
            invoked = CL.ClaudeAdapter().invoke_skill(spec, "context")
            created = CL.ClaudeAdapter().create(spec)
        self.assertIn("missing from system/init", invoked.notes[0])
        self.assertIn("missing from system/init", created.notes[0])

    def test_persistent_runtime_attaches_its_own_init_status(self):
        spec = self.spec()
        runtime = PairRuntime(spec, CL.ClaudeAdapter())
        runtime._on_event_for_log(json.dumps({"type": "system", "subtype": "init",
                                            "mcp_servers": [{"name": "probe", "status": "failed"}]}))
        runtime._on_event_for_log(json.dumps({"type": "system", "subtype": "init",
                                            "parent_tool_use_id": "child", "mcp_servers": []}))
        runtime.proc = SimpleNamespace(stdin=io.BytesIO())
        runtime._current_scope = TurnLogScope(runtime.main_log_path, 1)
        runtime._stdout_q.put(json.dumps(self.result_event()))
        with patch.object(runtime, "is_alive", return_value=True), patch.object(runtime, "_prepare_solicited_turn", return_value=0):
            raw = runtime.send("hello", timeout_seconds=1)
        self.assertEqual(raw["_mcp_servers"], [{"name": "probe", "status": "failed"}])
        result = CL.ClaudeAdapter()._build_send_result(spec, raw)
        self.assertIn("failed", result.notes[0])
        runtime._current_scope = TurnLogScope(runtime.main_log_path, 1)
        implicit_result = self.result_event()
        runtime._after_result(implicit_result)
        self.assertEqual(implicit_result["_mcp_servers"], raw["_mcp_servers"])

    def test_codex_startup_summary_does_not_blame_ready_server(self):
        notes = N.codex_startup_notes(["probe", "claude.ai Gmail", "missing"], str(self.directory),
            "mcp: probe ready\nmcp startup: ready: probe; failed: claude_ai_Gmail\n")
        self.assertEqual(len(notes), 2)
        self.assertIn("missing", notes[0])
        self.assertIn("claude_ai_Gmail failed to start", notes[1])
        self.assertFalse(any("probe failed" in note for note in notes))
        self.assertEqual(N.codex_startup_notes(["probe"], str(self.directory), "MCP tool call probe failed: requires approval"), [])

    def test_codex_send_result_has_startup_failure_notes(self):
        result = CX.CodexAdapter()._build_send_result(
            self.spec("codex"), [], "mcp: probe failed: handshake timed out", 0, 1, 0,
            model_used="gpt-5.6-sol", permission_level="workspace")
        self.assertIn("probe failed to start", result.notes[0])

    def test_handoff_carries_only_available_names_and_activates_them(self):
        source = self.spec(mcp_whitelist=["claude.ai Gmail", "missing"])
        R.add_pair(source)
        path = CL.ClaudeAdapter().transcript_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user","message":{"content":"hello"}}\n', encoding="utf-8")
        staged = []
        def imported(snapshot, **kwargs):
            self.assertNotEqual(snapshot, path)
            self.assertEqual(snapshot.parent, path.parent)
            staged.append(snapshot)
            return {"thread_id": "fresh-thread", "import_id": "import-1", "imported_at": "2026-09-10T12:00:00Z"}
        def send(spec, message, **kwargs):
            args = CX.CodexAdapter()._exec_args(spec, model=None, effort=None, permission_mode=None)
            config = next(arg for arg in args if arg.startswith("mcp_servers="))
            self.assertIn('"claude_ai_Gmail"', config)
            self.assertNotIn("missing", config)
            return SendResult(name=spec.name, session_id=spec.session_id, model_used="gpt-5.6-sol",
                              response="Ready", duration_ms=1, cost_usd=None, backend="codex")
        with patch.object(CX, "import_claude_session", side_effect=imported), \
                patch.object(CX, "measure_imported_history", return_value=15_000), \
                patch.object(CX.CodexAdapter, "send", side_effect=send):
            report = tool(S.pair_handoff)("source", timeout_seconds=5)
        target = R.get_pair("source-codex")
        self.assertEqual(target.mcp_whitelist, ["claude_ai_Gmail"])
        self.assertIn("MCP connector 'claude.ai Gmail' is carried", report)
        self.assertIn("MCP connector 'missing' is not available", report)
        self.assertIn("mcp_whitelist:missing", target.handoff_from["omitted"])
        self.assertTrue(all(not snapshot.exists() for snapshot in staged))
        self.assertEqual(R.get_pair("source").mcp_whitelist, source.mcp_whitelist)

    def test_fork_preserves_selection_and_checks_inventory_at_next_spawn(self):
        source = self.spec("codex")
        R.add_pair(source)
        with patch.object(CX.CodexAdapter, "fork", return_value="fork-id"):
            tool(S.pair_fork)("source", new_name="fork")
        fork = R.get_pair("fork")
        self.assertEqual(fork.mcp_whitelist, ["probe"])
        self.codex_rows = []
        args = CX.CodexAdapter()._exec_args(fork, model=None, effort=None, permission_mode=None)
        self.assertFalse(any(arg.startswith("mcp_servers=") for arg in args))


if __name__ == "__main__":
    unittest.main()
