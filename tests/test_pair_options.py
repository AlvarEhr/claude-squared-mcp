"""Offline tests for v0.17.0 per-pair Claude options: ``disallowed_tools``,
Claude ``backend_options`` (``settings`` / ``args``), the per-pair
``context_alert_at``, and the profile + append combination.

Nothing here spawns ``claude`` or ``codex``: every spawn path is exercised
with ``subprocess.run`` / ``subprocess.Popen`` mocked, and argv is captured
instead of executed."""

from __future__ import annotations

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Isolation BEFORE importing the package: importing the server runs an
# orphan sweep over the pairs dir, and the user may have live pairs.
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-pairopts-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import activity as A  # noqa: E402
from claude_squared import async_tasks as AT  # noqa: E402
from claude_squared import codex_models as CM  # noqa: E402
from claude_squared import connectors as N  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as RT  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared import _wait_script as W  # noqa: E402
from claude_squared.adapters import claude as CL  # noqa: E402
from claude_squared.adapters import codex as CX  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402
from claude_squared.models import ContextStatus, CreateResult, PairSpec, SendResult  # noqa: E402
from claude_squared.runtime import PairRuntime, RuntimeRegistry  # noqa: E402

WINDOW_1M = 1_000_000
# Global default level on a 1M Claude window: compaction point (window - 30k)
# minus the default headroom max(15% of the window, 40k) = 970k - 150k.
DEFAULT_LEVEL_1M = 820_000

# The tool-scope flags a pair with no connectors and no disallowed_tools got
# before v0.17.0 (from `git show HEAD:src/claude_squared/adapters/claude.py`,
# v0.16.2). Every spawn path sends exactly this block.
MCP_MINIMAL = [
    "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',
    "--disallowed-tools", "mcp__claude_ai_*",
    "--disallowed-tools", "mcp__pair__*",
    "--disallowed-tools", "mcp__Claude_Squared__*",
    "--disallowed-tools", "AskUserQuestion",
]


def tool(value):
    return getattr(value, "fn", value)


def contains_run(argv, run):
    """True when ``run`` appears in ``argv`` as one contiguous block."""
    n = len(run)
    return any(argv[i:i + n] == run for i in range(len(argv) - n + 1))


class PairOptionsCase(unittest.TestCase):
    """Per-test CLAUDE_HOME / CODEX_HOME, and every CLI spawn refused unless a
    test patches it in on purpose."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.codex_home = self.directory / "codex"
        self.codex_home.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory / "claude"),
                                                         "CODEX_HOME": str(self.codex_home)}))
        self.addCleanup(self.retire_activity)
        for module in (CX, CM):
            self.stack.enter_context(patch.object(module, "codex_home", return_value=self.codex_home))
        (self.codex_home / "models_cache.json").write_text(json.dumps({"models": [{
            "slug": "gpt-5.6-sol", "visibility": "list", "priority": 1, "context_window": 272_000,
            "max_context_window": 872_000, "effective_context_window_percent": 95,
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "max", "ultra")],
        }]}), encoding="utf-8")
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess.run")))
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected Popen")))
        self.stack.enter_context(patch.object(CL, "_claude_executable", return_value="claude"))
        self.stack.enter_context(patch.object(RT, "_claude_executable", return_value="claude"))
        self.stack.enter_context(patch.object(CL, "_cli_permission_choices", return_value=("manual", "auto", "plan")))
        self.stack.enter_context(patch.object(CX, "codex_executable", return_value="fixture-codex"))
        self.stack.enter_context(patch.object(CX, "config_readd_args", return_value=[]))
        self.stack.enter_context(patch.object(N, "inventory", return_value=[]))
        self.runtime = SimpleNamespace(get_or_none=Mock(return_value=None), evict=Mock(return_value=[]))
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=self.runtime))

    def retire_activity(self):
        # The activity flusher must stop using this fixture before its temp
        # tree is removed (Windows otherwise sees newly recreated files).
        home = str(self.directory / "claude" / "pairs")
        with A._WRITERS_LOCK:
            writers = [A._WRITERS.pop(key) for key in list(A._WRITERS) if key[0] == home]
        for writer in writers:
            with writer._io_lock, writer._lock:
                writer._forgotten = True

    def spec(self, name="p", backend="claude", **changes):
        data = dict(name=name, session_id=f"{name}-sid", backend=backend,
                    model="opus" if backend == "claude" else "sol", cwd=str(self.directory))
        data.update(changes)
        return PairSpec(**data)

    def full_spec(self, **changes):
        """Every v0.17 option at once (plus ultracode), for the cache invariants."""
        data = dict(ultracode=True, disallowed_tools=["Artifact", "Bash(git push *)"],
                    backend_options={"settings": {"permissions": {"deny": ["WebFetch"]}},
                                     "args": ["--max-budget-usd", "40"]})
        data.update(changes)
        return self.spec("full", **data)


# ---------------------------------------------------------------------------
# Argument building
# ---------------------------------------------------------------------------

class ClaudeArgsTests(PairOptionsCase):

    def capture_resume_paths(self, spec, *, send_override=None):
        """argv of the one-shot override send, fork and stream-json paths."""
        captured = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            out = {"type": "result", "subtype": "success", "session_id": "fork-sid", "result": "ok"}
            return SimpleNamespace(returncode=0, stdout=json.dumps(out).encode(), stderr=b"")
        adapter = CL.ClaudeAdapter()
        with patch.object(subprocess, "run", side_effect=fake_run), \
                patch.object(CL.ClaudeAdapter, "session_exists", return_value=True), \
                patch.object(CL.ClaudeAdapter, "_build_send_result", return_value=None), \
                patch.object(AT, "mark_task_executing", lambda task_id: None):
            adapter.send(spec, "hello", **(send_override or {"effort": "low"}))
            adapter.fork(spec, "SENTINEL")
            adapter._run_stream_json(spec, ["/context"], timeout_seconds=5)
        self.assertEqual(len(captured), 3)
        return dict(zip(("send", "fork", "stream"), captured))

    def capture_create(self, spec):
        """argv of create() (its one --print spawn)."""
        captured = []

        def fake_print(args, **kwargs):
            captured.append(list(args))
            return {"session_id": spec.session_id, "result": "pair-ready"}
        with patch.object(CL.ClaudeAdapter, "_run_print", side_effect=fake_print):
            CL.ClaudeAdapter().create(spec)
        return captured[0]

    def capture_runtime_start(self, spec):
        """argv of the persistent runtime's spawn (no reader threads run)."""
        proc = SimpleNamespace(pid=0, returncode=None, stdin=io.BytesIO(), stdout=io.BytesIO(),
                               stderr=io.BytesIO(), poll=lambda: None)
        with patch.object(subprocess, "Popen", return_value=proc) as popen, \
                patch.object(PairRuntime, "_stdout_reader", lambda self, gen=None: None), \
                patch.object(PairRuntime, "_stderr_reader", lambda self: None):
            PairRuntime(spec, CL.ClaudeAdapter()).start()
        return list(popen.call_args.args[0])

    def test_minimal_spec_argv_is_unchanged_from_v016(self):
        # A pair without the new options must produce byte-for-byte the argv
        # v0.16.2 produced (hard-coded from HEAD) — any drift would miss the
        # prompt cache of every existing pair after the upgrade.
        cwd = str(self.directory)
        spec = PairSpec(name="p", session_id="sid", model="opus", cwd=cwd)
        self.assertEqual(CL.ClaudeAdapter()._common_create_args(spec), MCP_MINIMAL + ["--add-dir", cwd])
        argv = self.capture_resume_paths(spec)
        self.assertEqual(argv["send"], [
            "claude", "--print", "--output-format", "json", "--resume", "sid", "--model", "opus",
            *MCP_MINIMAL, "--effort", "low", "--permission-mode", "auto", "-p", "hello"])
        self.assertEqual(argv["fork"], [
            "claude", "--print", "--output-format", "json", "--resume", "sid", "--fork-session",
            "--model", "opus", "--permission-mode", "auto", "-p", "SENTINEL", *MCP_MINIMAL])
        self.assertEqual(argv["stream"], [
            "claude", "--print", "--verbose", "--resume", "sid", "--model", "opus",
            "--permission-mode", "auto", "--input-format", "stream-json", "--output-format", "stream-json",
            *MCP_MINIMAL])

    def test_ultracode_alone_keeps_the_v016_settings_bytes(self):
        # The merged --settings must not perturb an ultracode-only pair: the
        # exact string (and its position) is what v0.16 sent at create/runtime.
        cwd = str(self.directory)
        spec = PairSpec(name="p", session_id="sid", model="opus", cwd=cwd, ultracode=True)
        adapter = CL.ClaudeAdapter()
        self.assertEqual(adapter._settings_args(spec), ["--settings", '{"ultracode": true}'])
        self.assertEqual(adapter._common_create_args(spec),
                         MCP_MINIMAL + ["--settings", '{"ultracode": true}', "--add-dir", cwd])

    def test_no_settings_and_no_ultracode_means_no_settings_flag(self):
        adapter = CL.ClaudeAdapter()
        for options in ({}, {"settings": {}}, {"args": []}, {"settings": "{}"}):
            with self.subTest(options=options):
                spec = self.spec(backend_options=options)
                self.assertEqual(adapter._settings_args(spec), [])
                self.assertEqual(adapter._option_args(spec), [])
                self.assertNotIn("--settings", adapter._common_create_args(spec))

    def test_settings_merge_with_ultracode_into_one_sorted_json(self):
        # One --settings flag: a second one would REPLACE the first, so the
        # user's settings and the ultracode key must travel together. Sorted
        # keys make the same config always yield the same argument.
        spec = self.spec(ultracode=True, backend_options={"settings": {"z": 1, "a": {"y": 2, "b": 3}}})
        args = CL.ClaudeAdapter()._common_create_args(spec)
        self.assertEqual(args.count("--settings"), 1)
        value = args[args.index("--settings") + 1]
        self.assertEqual(value, '{"a": {"b": 3, "y": 2}, "ultracode": true, "z": 1}')
        self.assertEqual(json.loads(value), {"a": {"y": 2, "b": 3}, "z": 1, "ultracode": True})

    def test_settings_stored_as_json_string_still_apply(self):
        # A registry value written by hand (or by a host that JSON-encodes)
        # is parsed at spawn the same way pair_create normalizes it.
        spec = self.spec(backend_options={"settings": '{"model": "x", "env": {"A": "1"}}'})
        self.assertEqual(CL.ClaudeAdapter()._settings_args(spec),
                         ["--settings", '{"env": {"A": "1"}, "model": "x"}'])

    def test_disallowed_tools_are_stripped_deduplicated_and_skip_headless(self):
        spec = self.spec(disallowed_tools=[" Artifact ", "Artifact", "", "AskUserQuestion", "Bash(git push *)"])
        self.assertEqual(CL.ClaudeAdapter.disallowed_tools(spec), ["Artifact", "Bash(git push *)"])
        args = CL.ClaudeAdapter()._mcp_args(spec)
        # AskUserQuestion is always passed once (headless); never twice.
        self.assertEqual(args.count("AskUserQuestion"), 1)
        self.assertEqual(args.count("Artifact"), 1)
        self.assertEqual(CL.ClaudeAdapter.disallowed_tools(self.spec()), [])

    def test_disallowed_tools_follow_the_connector_and_headless_flags(self):
        # They ride inside _mcp_args (the tool-scope block every path sends),
        # after the existing flags, so a pair without them is unchanged.
        spec = self.spec(disallowed_tools=["Artifact", "Bash(git push *)"])
        args = CL.ClaudeAdapter()._mcp_args(spec)
        self.assertEqual(args, MCP_MINIMAL + ["--disallowed-tools", "Artifact",
                                              "--disallowed-tools", "Bash(git push *)"])

    def test_raw_args_end_every_argv(self):
        # A raw flag missing its value (["--system-prompt"]) would consume the
        # next token — once that was --print / --permission-mode. They now end
        # every argv (after -p <message>), so they can only fail loudly.
        for raw in (["--betas", "x", "y"], ["--system-prompt"]):
            with self.subTest(raw=raw):
                spec = self.spec(backend_options={"args": raw}, allowed_tools=["Read"])
                self.assertNotIn(raw[0], CL.ClaudeAdapter()._common_create_args(spec))
                argvs = {"create": self.capture_create(spec), "runtime": self.capture_runtime_start(spec),
                         **self.capture_resume_paths(spec)}
                for name, argv in argvs.items():
                    with self.subTest(path=name):
                        self.assertEqual(argv[-len(raw):], raw)
                        self.assertEqual(argv.count(raw[0]), 1)

    def test_connector_rewrite_keeps_raw_args_last(self):
        # With connectors, _run_print switches to stream-json and adds
        # --verbose: inserted after the output format, not appended.
        captured = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            out = json.dumps({"type": "result", "subtype": "success", "result": "ok"})
            return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        spec = self.spec(mcp_whitelist=["probe"])
        with patch.object(subprocess, "run", side_effect=fake_run), \
                patch.object(CL.connectors, "effective", return_value=True):
            CL.ClaudeAdapter()._run_print(["--print", "--output-format", "json", "-p", "m", "--betas", "x"],
                                          timeout_seconds=5, pair_name="p", connector_spec=spec)
        self.assertEqual(captured[0][1:], ["--print", "--output-format", "stream-json", "--verbose",
                                           "-p", "m", "--betas", "x"])

    def test_every_spawn_path_sends_the_same_tool_scope_and_options(self):
        # Prompt caching: the tool list and settings must be identical on the
        # runtime, the override send, fork and the stream-json one-shots
        # (compact, /context, invoke) — a different list on one path misses
        # the cache for the whole context.
        spec = self.full_spec()
        adapter = CL.ClaudeAdapter()
        mcp = adapter._mcp_args(spec)
        settings = ["--settings", '{"permissions": {"deny": ["WebFetch"]}, "ultracode": true}']
        extra = ["--max-budget-usd", "40"]
        self.assertEqual(adapter._option_args(spec), settings + extra)
        self.assertTrue(contains_run(mcp, ["--disallowed-tools", "Artifact",
                                           "--disallowed-tools", "Bash(git push *)"]))
        common = adapter._common_create_args(spec)
        self.assertEqual(common[:len(mcp) + 2], mcp + settings)
        self.assertFalse(contains_run(common, extra))  # raw args end each argv instead
        runtime = self.capture_runtime_start(spec)
        self.assertEqual(runtime[1:1 + len(common)], common)
        paths = self.capture_resume_paths(spec)
        for name, argv in [("create", self.capture_create(spec)), ("runtime", runtime), *paths.items()]:
            with self.subTest(path=name):
                self.assertTrue(contains_run(argv, mcp + settings))
                self.assertEqual(argv[-len(extra):], extra)
                self.assertEqual(argv.count("--settings"), 1)

    def test_override_permission_does_not_change_the_options(self):
        # A per-call permission override reaches connector rules only; the
        # pair's own removals and options stay the same.
        spec = self.full_spec()
        paths = self.capture_resume_paths(spec, send_override={"permission_mode": "read-only"})
        adapter = CL.ClaudeAdapter()
        self.assertTrue(contains_run(paths["send"], adapter._mcp_args(spec) + adapter._settings_args(spec)))
        self.assertEqual(paths["send"][-2:], adapter._extra_args(spec))

    def test_spawn_refuses_malformed_settings_or_args(self):
        # Dropping a malformed settings value silently could drop a
        # permission rule the user relies on — the spawn must refuse.
        adapter = CL.ClaudeAdapter()
        for options in ({"settings": "not json"}, {"settings": [1]}, {"settings": {"ultracode": True}},
                        {"args": ["--model", "x"]}, {"args": "--max-budget-usd 40"}, {"args": ["40"]}):
            with self.subTest(options=options):
                spec = self.spec(backend_options=options)
                with self.assertRaisesRegex(PairError, "unusable backend_options"):
                    adapter._common_create_args(spec)
                with self.assertRaisesRegex(PairError, "pair_update"):
                    adapter._option_args(spec)

    def test_spawn_ignores_unknown_keys_stored_before_v017(self):
        # Before v0.17.0 a Claude pair accepted any backend_options keys and
        # ignored them; such a pair must still start, with no extra flags.
        cwd = str(self.directory)
        spec = PairSpec(name="p", session_id="sid", model="opus", cwd=cwd,
                        backend_options={"config": {"x": 1}, "sandbox": "read-only"})
        self.assertEqual(CL.ClaudeAdapter()._common_create_args(spec), MCP_MINIMAL + ["--add-dir", cwd])
        self.assertEqual(CL.ClaudeAdapter()._option_args(spec), [])


# ---------------------------------------------------------------------------
# validate_backend_options
# ---------------------------------------------------------------------------

# Written out here rather than read from the adapter's tables, so a flag
# dropped from them fails this test. (Adding more refusals is fine.)
REFUSED_ARGS = [
    # set by the pair's own fields
    "--settings", "--disallowed-tools", "--disallowedTools", "--allowed-tools", "--allowedTools",
    "--model", "--fallback-model", "--effort", "--permission-mode", "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions", "--append-system-prompt", "--append-system-prompt-file",
    "--mcp-config", "--strict-mcp-config", "--add-dir",
    # the spawn's own stream protocol
    "--print", "-p", "--resume", "-r", "--continue", "-c", "--session-id", "--fork-session",
    "--input-format", "--output-format", "--replay-user-messages", "--include-partial-messages",
    "--forward-subagent-text", "--include-hook-events",
    # would loosen the pair or bypass an opt-in (review of 5f66ff8, P2-2)
    "--setting-sources", "--restricted", "--safe-mode", "--bare", "--permission-prompt-tool",
    "--permission-prompts", "--plugin-dir", "--plugin-url", "--agent", "--agents",
    # would lose or move the session, or never start it
    "--no-session-persistence", "--worktree", "-w", "--tmux", "--bg", "--background", "--cloud",
    "--desktop", "--environment", "--remote-control", "--teleport", "--from-pr", "--ide",
    "--version", "-v", "--help", "-h", "--", "-",
]
REFUSED_SETTINGS = [
    {"hooks": {"PreToolUse": []}}, {"disableAllHooks": True}, {"enabledPlugins": {"x@y": True}},
    {"enableAllProjectMcpServers": True}, {"enabledMcpjsonServers": ["x"]}, {"agent": "reviewer"},
    {"sandbox": {"enabled": True}}, {"permissions": {"allow": ["Bash"]}},
    {"permissions": {"defaultMode": "bypassPermissions"}}, {"permissions": {"additionalDirectories": ["/"]}},
]


class ValidateBackendOptionsTests(unittest.TestCase):
    validate = staticmethod(CL.ClaudeAdapter.validate_backend_options)

    def test_accepts_and_normalizes_both_keys(self):
        self.assertEqual(self.validate(None), {})
        self.assertEqual(self.validate({}), {})
        self.assertEqual(self.validate({"settings": {"a": 1}}), {"settings": {"a": 1}})
        self.assertEqual(self.validate({"settings": '{"a": 1}'}), {"settings": {"a": 1}})
        self.assertEqual(self.validate({"args": ["--max-budget-usd", "40"]}), {"args": ["--max-budget-usd", "40"]})
        self.assertEqual(self.validate({"args": '["--max-budget-usd", "40"]'}), {"args": ["--max-budget-usd", "40"]})
        self.assertEqual(self.validate({"args": []}), {"args": []})

    def test_does_not_mutate_the_input(self):
        original = {"settings": '{"a": 1}'}
        self.validate(original)
        self.assertEqual(original, {"settings": '{"a": 1}'})

    def test_unknown_keys_refused_unless_spawn_mode(self):
        with self.assertRaisesRegex(ValueError, "not Claude options"):
            self.validate({"config": {"x": 1}})
        self.assertEqual(self.validate({"config": {"x": 1}}, allow_unknown=True), {"config": {"x": 1}})

    def test_settings_must_be_a_json_object(self):
        for settings in ("not json", "[1, 2]", 5, ["a"], '"str"'):
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    self.validate({"settings": settings})

    def test_settings_may_not_carry_ultracode(self):
        with self.assertRaisesRegex(ValueError, "ultracode parameter"):
            self.validate({"settings": {"ultracode": True}})
        with self.assertRaisesRegex(ValueError, "ultracode parameter"):
            self.validate({"settings": '{"ultracode": false}'}, allow_unknown=True)

    def test_args_must_be_a_list_of_strings(self):
        for args in ("--max-budget-usd 40", ["--max-budget-usd", 40], {"a": 1}, '{"a": 1}', None):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, "list of strings"):
                    self.validate({"args": args})

    def test_args_must_start_with_a_flag(self):
        # They end every argv: a leading bare value would become a prompt.
        with self.assertRaisesRegex(ValueError, "must start with a flag"):
            self.validate({"args": ["40", "--max-budget-usd"]})

    def test_every_refused_flag_is_refused_in_both_spellings(self):
        for flag in REFUSED_ARGS:
            for args in ([flag], [f"{flag}=value"], ["--max-budget-usd", "4", flag]):
                with self.subTest(args=args):
                    with self.assertRaisesRegex(ValueError, f"may not contain {re.escape(flag)}"):
                        self.validate({"args": args})

    def test_combined_short_flags_are_checked_letter_by_letter(self):
        with self.assertRaisesRegex(ValueError, "-c:"):
            self.validate({"args": ["-dc"]})
        self.assertEqual(self.validate({"args": ["-d", "api"]}), {"args": ["-d", "api"]})

    def test_loosening_settings_are_refused(self):
        for settings in REFUSED_SETTINGS:
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, "may not contain"):
                    self.validate({"settings": settings})
                with self.assertRaisesRegex(ValueError, "may not contain"):
                    self.validate({"settings": json.dumps(settings)}, allow_unknown=True)
        with self.assertRaisesRegex(ValueError, "permissions must be a JSON object"):
            self.validate({"settings": {"permissions": ["Bash"]}})

    def test_tightening_and_neutral_settings_pass(self):
        settings = {"permissions": {"deny": ["WebFetch"], "ask": ["Bash(git push *)"]},
                    "env": {"A": "1"}, "model": "x", "cleanupPeriodDays": 36500}
        self.assertEqual(self.validate({"settings": settings}), {"settings": settings})

    def test_refusal_names_the_field_to_use(self):
        with self.assertRaisesRegex(ValueError, "set it with disallowed_tools instead"):
            self.validate({"args": ["--disallowedTools", "Bash"]})
        with self.assertRaisesRegex(ValueError, r"backend_options\.settings"):
            self.validate({"args": ["--settings", "{}"]})
        with self.assertRaisesRegex(ValueError, "stream protocol"):
            self.validate({"args": ["--output-format", "text"]})
        with self.assertRaisesRegex(ValueError, "deny rules and hooks"):
            self.validate({"args": ["--setting-sources", "project"]})

    def test_unmanaged_flags_pass_verbatim(self):
        args = ["--max-budget-usd", "40", "--betas", "a", "b", "--debug"]
        self.assertEqual(self.validate({"args": args}), {"args": args})


# ---------------------------------------------------------------------------
# Context alert: threshold, wait.py, send result, pair_status lines
# ---------------------------------------------------------------------------

def wait_globals():
    """The wait.py source exec'd in-process (what pairs/wait.py runs)."""
    g = {"__name__": "offline_wait"}
    exec(compile(W.WAIT_SCRIPT_SOURCE, "wait.py", "exec"), g)
    return g


class ContextAlertTests(PairOptionsCase):

    def ctx(self, used, window=WINDOW_1M):
        return {"used": used, "window": window, "backend": "claude"}

    def result(self, used, window=WINDOW_1M, warning="band warning"):
        return SendResult(name="p", response="ok", session_id="p-sid", model_used="opus", duration_ms=1,
                          context=ContextStatus(tokens_used=used, tokens_max=window,
                                                percent=used * 100 / window, warning=warning))

    def test_threshold_takes_the_earlier_of_pair_and_global_level(self):
        self.assertEqual(A.context_alert_threshold(WINDOW_1M), DEFAULT_LEVEL_1M)
        self.assertEqual(A.context_alert_threshold(WINDOW_1M, alert_at=350_000), 350_000)
        # Capped at the global level: a misjudged value (or a smaller window
        # than assumed) still alerts before auto-compaction.
        self.assertEqual(A.context_alert_threshold(WINDOW_1M, alert_at=900_000), DEFAULT_LEVEL_1M)
        self.assertEqual(A.context_alert_threshold(200_000, alert_at=350_000), 130_000)
        self.assertEqual(A.context_alert_threshold(WINDOW_1M, 50_000, alert_at=350_000), 350_000)
        self.assertEqual(A.context_alert_threshold(200_000, 40_000, "codex", 150_000, 50_000), 50_000)
        self.assertIsNone(A.context_alert_threshold(None, alert_at=350_000))

    def test_a_headroom_larger_than_the_window_keeps_the_pair_level(self):
        # Review of 5f66ff8, P2-1: the global level clamps to 0 there, and
        # min(alert_at, 0) = 0 is never crossed; the pair's value is capped at
        # the compaction point instead.
        self.assertEqual(A.context_alert_threshold(200_000, 300_000), 0)
        self.assertEqual(A.context_alert_threshold(200_000, 300_000, alert_at=100_000), 100_000)
        self.assertEqual(A.context_alert_threshold(200_000, 300_000, alert_at=190_000), 170_000)
        # A default-window Codex pair (estimated point: 90% of the window).
        self.assertEqual(A.context_alert_threshold(272_000, 300_000, "codex", None, 200_000), 200_000)
        tool(S.pair_settings_set)(context_alert_headroom=300_000)
        spec = self.spec(context_alert_at=100_000)
        below, past = self.result(90_000, 200_000), self.result(150_000, 200_000)
        S._apply_context_alert(spec, below)
        S._apply_context_alert(spec, past)
        self.assertIsNone(below.context.warning)
        self.assertIn("context alert (100,000)", past.context.warning)
        R.add_pair(self.spec("small", context_alert_at=100_000))
        g = wait_globals()
        alert = g["Alert"]("default", None)
        self.assertIsNone(self.poll(g, alert, "small", 90_000, 200_000))
        self.assertEqual(self.poll(g, alert, "small", 120_000, 200_000), (120_000, 200_000, 100_000))

    def test_threshold_ignores_non_positive_and_non_int_alert_at(self):
        for bad in (0, -5, True, False, "350000", 350_000.0):
            with self.subTest(alert_at=bad):
                self.assertEqual(A.context_alert_threshold(WINDOW_1M, alert_at=bad), DEFAULT_LEVEL_1M)

    def poll(self, g, alert, pair, used, window=WINDOW_1M):
        """One watcher poll as wait_pair runs it: one registry read, then the check."""
        alert.note_registry(*g["_registry_read"](pair))
        return alert.check(self.ctx(used, window), None)

    def test_wait_alert_fires_at_the_pair_level(self):
        # The acceptance case: a 1M pair with context_alert_at=350000 exits 8
        # when 350k is crossed; the same crossing on a pair without it does not.
        R.add_pair(self.spec("alerted", context_alert_at=350_000))
        R.add_pair(self.spec("plain"))
        g = wait_globals()
        for pair, expected in (("alerted", (360_000, WINDOW_1M, 350_000)), ("plain", None)):
            with self.subTest(pair=pair):
                alert = g["Alert"]("default", None)
                self.assertIsNone(self.poll(g, alert, pair, 300_000))  # first sight never fires
                self.assertEqual(self.poll(g, alert, pair, 360_000), expected)
        # No registry read yet (task mode before the pair is known): global level.
        alert = g["Alert"]("default", None)
        alert.check(self.ctx(300_000), None)
        self.assertIsNone(alert.check(self.ctx(360_000), None))

    def test_wait_alert_rereads_the_registry_each_poll(self):
        # A pair_update while a watcher already runs applies to it.
        R.add_pair(self.spec("p"))
        g = wait_globals()
        alert = g["Alert"]("default", None)
        self.assertIsNone(self.poll(g, alert, "p", 300_000))
        self.assertIn("context_alert_at", tool(S.pair_update)("p", context_alert_at=350_000))
        self.runtime.evict.assert_not_called()
        self.assertEqual(self.poll(g, alert, "p", 360_000), (360_000, WINDOW_1M, 350_000))

    def test_wait_keeps_the_last_good_value_when_a_read_fails(self):
        # On Windows an open handle can fail the writer's os.replace, and a
        # reader can see a torn or missing file: that proves nothing.
        R.add_pair(self.spec("p", context_alert_at=350_000))
        g = wait_globals()
        alert = g["Alert"]("default", None)
        self.assertIsNone(self.poll(g, alert, "p", 300_000))
        path = R.registry_path()
        good = path.read_text(encoding="utf-8")
        path.write_text(good[: len(good) // 2], encoding="utf-8")
        self.assertEqual(g["_registry_read"]("p"), (False, None))
        self.assertEqual(self.poll(g, alert, "p", 360_000), (360_000, WINDOW_1M, 350_000))
        path.unlink()
        self.assertEqual(g["_registry_read"]("p"), (False, None))
        alert = g["Alert"]("default", None)
        alert.alert_at = 350_000
        alert.note_registry(*g["_registry_read"]("p"))
        self.assertEqual(alert.alert_at, 350_000)

    def test_wait_fires_once_when_the_level_drops_behind_the_fill(self):
        # A crossing needs prev < level <= used; lowering context_alert_at
        # below the current fill would otherwise never fire.
        R.add_pair(self.spec("p"))
        g = wait_globals()
        alert = g["Alert"]("default", None)
        self.assertIsNone(self.poll(g, alert, "p", 400_000))
        self.assertIsNone(self.poll(g, alert, "p", 410_000))
        tool(S.pair_update)("p", context_alert_at=350_000)
        self.assertEqual(self.poll(g, alert, "p", 412_000), (412_000, WINDOW_1M, 350_000))
        # Already past the old level when it dropped: that is not new.
        R.add_pair(self.spec("late"))
        alert = g["Alert"]("default", None)
        self.assertIsNone(self.poll(g, alert, "late", 150_000, 200_000))  # level 130k: already past
        tool(S.pair_update)("late", context_alert_at=100_000)
        self.assertIsNone(self.poll(g, alert, "late", 151_000, 200_000))

    def test_wait_forgotten_check_uses_the_polls_read(self):
        g = wait_globals()
        view = {"owners": [{"status": "forgotten"}]}
        self.assertTrue(g["_forgotten"](view, (True, None)))
        self.assertFalse(g["_forgotten"](view, (False, None)))  # unreadable registry
        self.assertFalse(g["_forgotten"](view, (True, {"name": "p"})))

    def test_wait_pct_and_off_modes_ignore_the_pair_value(self):
        R.add_pair(self.spec("p", context_alert_at=350_000))
        g = wait_globals()
        alerts = {mode: g["Alert"](mode, pct) for mode, pct in (("pct", 90), ("off", None), ("default", None))}
        for alert in alerts.values():
            alert.note_registry(*g["_registry_read"]("p"))
        self.assertEqual(alerts["pct"].level(WINDOW_1M), 900_000)
        self.assertIsNone(alerts["off"].level(WINDOW_1M))
        self.assertEqual(alerts["default"].level(WINDOW_1M), 350_000)

    def test_wait_reader_rejects_malformed_registry_values(self):
        # wait.py is stdlib-only and reads the raw JSON: a hand-edited value
        # of the wrong type must fall back to the global level, not crash.
        R.add_pair(self.spec("p"))
        g = wait_globals()
        path = R.registry_path()
        for bad in (True, "350000", -5, 0, 1.5):
            with self.subTest(value=bad):
                data = json.loads(path.read_text(encoding="utf-8"))
                data["pairs"]["p"]["context_alert_at"] = bad
                path.write_text(json.dumps(data), encoding="utf-8")
                ok, entry = g["_registry_read"]("p")
                self.assertTrue(ok)
                self.assertIsNone(g["_alert_at"](entry))
                alert = g["Alert"]("default", None)
                alert.note_registry(ok, entry)
                self.assertEqual(alert.level(WINDOW_1M), DEFAULT_LEVEL_1M)
        self.assertEqual(g["_registry_read"]("missing"), (True, None))
        self.assertIsNone(g["_alert_at"](None))

    def test_send_result_warning_follows_the_pair_level(self):
        spec = self.spec(context_alert_at=350_000)
        below = self.result(300_000)
        S._apply_context_alert(spec, below)
        self.assertIsNone(below.context.warning)
        at = self.result(350_000)
        S._apply_context_alert(spec, at)
        self.assertIn("past this pair's context alert (350,000)", at.context.warning)
        self.assertIn("steering_prompt", at.context.warning)

    def test_send_result_uses_the_capped_level(self):
        # 65% would carry the 60% band warning; a pair with its own (capped)
        # alert gets its warning from that level instead.
        spec = self.spec(context_alert_at=900_000)
        mid = self.result(650_000)
        S._apply_context_alert(spec, mid)
        self.assertIsNone(mid.context.warning)
        late = self.result(830_000)
        S._apply_context_alert(spec, late)
        self.assertIn(f"({DEFAULT_LEVEL_1M:,})", late.context.warning)

    def test_send_result_unchanged_without_a_pair_alert(self):
        result = self.result(650_000, warning="Consider pair_compact('p')")
        S._apply_context_alert(self.spec(), result)
        self.assertEqual(result.context.warning, "Consider pair_compact('p')")
        no_ctx = SendResult(name="p", response="ok", session_id="s", model_used="opus", duration_ms=1)
        S._apply_context_alert(self.spec(context_alert_at=350_000), no_ctx)
        self.assertIsNone(no_ctx.context)

    def test_status_lines_use_the_pair_level(self):
        R.add_pair(self.spec("alerted", context_alert_at=350_000))
        R.add_pair(self.spec("plain"))
        view = {"context": {"used": 400_000, "window": WINDOW_1M, "backend": "claude", "source": "stream"}}
        with patch.object(S.activity_mod, "view", return_value=view):
            alerted = "\n".join(S._activity_status_lines("alerted"))
            plain = "\n".join(S._activity_status_lines("plain"))
        self.assertIn("past the context alert (350,000)", alerted)
        self.assertIn(f"alert at {DEFAULT_LEVEL_1M:,}", plain)
        self.assertNotIn("past the context alert", plain)


# ---------------------------------------------------------------------------
# MCP tools: pair_create / pair_update / pair_info / pair_handoff
# ---------------------------------------------------------------------------

class ServerToolTests(PairOptionsCase):

    def create(self, name="c", **kwargs):
        def fake_create(spec, **_kw):
            return CreateResult(name=spec.name, session_id=spec.session_id or "created")
        with patch.object(CL.ClaudeAdapter, "create", side_effect=fake_create) as create, \
                patch.object(CX.CodexAdapter, "create", side_effect=fake_create):
            kwargs.setdefault("cwd", str(self.directory))
            report = tool(S.pair_create)(name, **kwargs)
        return report, create

    def test_create_stores_the_new_fields_normalized(self):
        report, create = self.create(
            model="opus", disallowed_tools=["Artifact", " Bash(git push *) "], context_alert_at="350_000",
            backend_options={"settings": '{"env": {"A": "1"}}', "args": '["--max-budget-usd", "40"]'})
        stored = R.get_pair("c")
        self.assertEqual(stored.disallowed_tools, ["Artifact", "Bash(git push *)"])
        self.assertEqual(stored.context_alert_at, 350_000)
        # JSON strings are parsed once at create, so the registry holds objects.
        self.assertEqual(stored.backend_options, {"settings": {"env": {"A": "1"}}, "args": ["--max-budget-usd", "40"]})
        self.assertEqual(create.call_args.args[0].disallowed_tools, stored.disallowed_tools)
        self.assertIn("Created 'c'", report)

    def test_create_refuses_disallowed_tools_on_codex(self):
        with self.assertRaisesRegex(PairError, "Claude-only"):
            self.create(backend="codex", disallowed_tools=["Artifact"])
        self.assertNotIn("c", R.load().pairs)

    def test_create_validates_claude_backend_options(self):
        for options, pattern in (({"config": {"x": 1}}, "not Claude options"),
                                 ({"args": ["--model", "x"]}, "may not contain --model"),
                                 ({"settings": {"ultracode": True}}, "ultracode parameter"),
                                 ({"settings": "nope"}, "JSON object")):
            with self.subTest(options=options):
                with self.assertRaisesRegex(PairError, pattern):
                    self.create(model="opus", backend_options=options)
                self.assertNotIn("c", R.load().pairs)

    def test_create_refuses_settings_on_codex(self):
        with self.assertRaisesRegex(PairError, "settings is Claude-only"):
            self.create(backend="codex", backend_options={"settings": {"a": 1}})

    def test_create_rejects_a_bad_context_alert(self):
        for bad in (-1, True, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(PairError):
                    self.create(model="opus", context_alert_at=bad)

    def test_create_notes_a_missing_profile_file(self):
        report, _ = self.create("missing", model="opus", profile_name="no-such-role")
        self.assertIn("profile 'no-such-role' has no file at", report)
        (R.profiles_dir() / "reviewer.md").write_text("You review.", encoding="utf-8")
        report, _ = self.create("present", model="opus", profile_name="reviewer")
        self.assertNotIn("has no file at", report)

    def test_update_disallowed_tools_evicts_and_clears(self):
        R.add_pair(self.spec())
        report = tool(S.pair_update)("p", disallowed_tools=["Artifact"])
        self.assertEqual(R.get_pair("p").disallowed_tools, ["Artifact"])
        self.runtime.evict.assert_called_once()
        self.assertEqual(self.runtime.evict.call_args.args[0], "p")
        self.assertIn("disallowed_tools", self.runtime.evict.call_args.args[1])
        # Re-applied at every spawn: no pair_clear needed.
        self.assertNotIn("pair_clear", report)
        # The same value again is not a change: no respawn.
        self.runtime.evict.reset_mock()
        tool(S.pair_update)("p", disallowed_tools=["Artifact"])
        self.runtime.evict.assert_not_called()
        # [] clears back to None (the key then leaves the registry file).
        tool(S.pair_update)("p", disallowed_tools=[])
        self.assertIsNone(R.get_pair("p").disallowed_tools)
        self.runtime.evict.assert_called_once()

    def test_update_context_alert_needs_no_respawn(self):
        R.add_pair(self.spec())
        tool(S.pair_update)("p", context_alert_at=350_000)
        self.assertEqual(R.get_pair("p").context_alert_at, 350_000)
        tool(S.pair_update)("p", context_alert_at="400_000")
        self.assertEqual(R.get_pair("p").context_alert_at, 400_000)
        tool(S.pair_update)("p", context_alert_at=0)
        self.assertIsNone(R.get_pair("p").context_alert_at)
        self.runtime.evict.assert_not_called()

    def test_update_rejects_a_bad_context_alert(self):
        R.add_pair(self.spec(context_alert_at=350_000))
        for bad in (-1, True, "abc", 1.5):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(PairError, "context_alert_at"):
                    tool(S.pair_update)("p", context_alert_at=bad)
        self.assertEqual(R.get_pair("p").context_alert_at, 350_000)

    def test_update_refuses_disallowed_tools_on_codex(self):
        R.add_pair(self.spec(backend="codex"))
        with self.assertRaisesRegex(PairError, "Claude-only"):
            tool(S.pair_update)("p", disallowed_tools=["Artifact"])
        self.assertIsNone(R.get_pair("p").disallowed_tools)
        self.runtime.evict.assert_not_called()

    def test_update_validates_backend_options_for_the_pairs_backend(self):
        R.add_pair(self.spec())
        with self.assertRaisesRegex(PairError, "ultracode parameter"):
            tool(S.pair_update)("p", backend_options={"settings": {"ultracode": True}})
        with self.assertRaisesRegex(PairError, "may not contain --resume"):
            tool(S.pair_update)("p", backend_options={"args": ["--resume", "x"]})
        self.runtime.evict.assert_not_called()
        tool(S.pair_update)("p", backend_options='{"settings": "{\\"env\\": {\\"A\\": \\"1\\"}}"}')
        self.assertEqual(R.get_pair("p").backend_options, {"settings": {"env": {"A": "1"}}})
        self.runtime.evict.assert_called_once()
        R.add_pair(self.spec("cx", backend="codex"))
        with self.assertRaisesRegex(PairError, "settings is Claude-only"):
            tool(S.pair_update)("cx", backend_options={"settings": {"a": 1}})
        with self.assertRaisesRegex(PairError, "permissions.allow"):
            tool(S.pair_update)("p", backend_options={"settings": {"permissions": {"allow": ["Bash"]}}})

    def test_secrets_in_settings_are_masked_on_display(self):
        # Display only: the registry keeps the values (the docs say to keep
        # secrets out — settings travel on the claude command line).
        secret = {"settings": {"env": {"API_TOKEN": "s3cret-env"}, "apiKeyHelper": "s3cret-helper",
                               "model": "x"}}
        R.add_pair(self.spec())
        update = tool(S.pair_update)("p", backend_options=secret)
        info = tool(S.pair_info)("p")
        verbose = tool(S.pair_info)("p", verbose=True)
        verbose_update = tool(S.pair_update)("p", purpose="x", verbose=True)
        for shown in (update, info, verbose, verbose_update):
            self.assertNotIn("s3cret", shown)
        self.assertIn('"env": {"API_TOKEN": "***"}', info)
        self.assertIn('"apiKeyHelper": "***"', info)
        self.assertIn('"model": "x"', info)
        self.assertEqual(R.get_pair("p").backend_options, secret)

    def test_pair_info_shows_the_new_fields(self):
        R.add_pair(self.spec(disallowed_tools=["Artifact", "Bash(git push *)"], context_alert_at=350_000,
                             backend_options={"settings": {"a": 1}}))
        info = tool(S.pair_info)("p")
        self.assertIn("disallowed_tools: Artifact, Bash(git push *)", info)
        self.assertIn("context alert at: 350,000 tokens", info)
        self.assertIn('backend_options: {"settings": {"a": 1}}', info)
        R.add_pair(self.spec("bare"))
        bare = tool(S.pair_info)("bare")
        self.assertNotIn("disallowed_tools", bare)
        self.assertNotIn("context alert at", bare)

    def test_handoff_drops_claude_only_options_and_carries_the_alert(self):
        source = self.spec("source", disallowed_tools=["Artifact"], context_alert_at=350_000,
                           backend_options={"settings": {"a": 1}})
        R.add_pair(source)
        path = CL.ClaudeAdapter().transcript_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user","message":{"content":"hello"}}\n', encoding="utf-8")

        def imported(snapshot, **kwargs):
            return {"thread_id": "fresh-thread", "import_id": "import-1", "imported_at": "2026-10-09T12:00:00Z"}

        def send(spec, message, **kwargs):
            return SendResult(name=spec.name, session_id=spec.session_id, model_used="gpt-5.6-sol",
                              response="Ready", duration_ms=1, cost_usd=None, backend="codex")
        with patch.object(CX, "import_claude_session", side_effect=imported), \
                patch.object(CX, "measure_imported_history", return_value=15_000), \
                patch.object(CX.CodexAdapter, "send", side_effect=send):
            report = tool(S.pair_handoff)("source", timeout_seconds=5)
        target = R.get_pair("source-codex")
        self.assertEqual(target.context_alert_at, 350_000)
        self.assertIsNone(target.disallowed_tools)
        self.assertEqual(target.backend_options, {})
        self.assertIn("disallowed_tools", target.handoff_from["omitted"])
        self.assertIn("backend_options", target.handoff_from["omitted"])
        self.assertIn("tool removals ['Artifact']", report)
        self.assertIn("backend_options (settings / raw CLI args)", report)

    def test_warm_runtime_is_replaced_when_another_process_changes_options(self):
        # pair_update evicts only its own process's runtime; another process's
        # warm runtime must notice the change at its next get_or_start.
        for change in ({"disallowed_tools": ["Artifact"]}, {"backend_options": {"settings": {"a": 1}}},
                       {"ultracode": True}, {"fallback_model": "sonnet"}, {"extra_dirs": ["D:/x"]},
                       {"allowed_tools": ["Read"]}, {"system_prompt_append": "x"}, {"profile_name": "x"}):
            with self.subTest(change=change):
                registry = RuntimeRegistry()
                old = SimpleNamespace(spec=self.spec(), is_alive=lambda: True, stop=Mock())
                registry._runtimes["p"] = old
                replacement = SimpleNamespace(is_alive=lambda: True)
                with patch.object(RT, "PairRuntime", return_value=replacement):
                    result = registry.get_or_start(self.spec(**change), CL.ClaudeAdapter())
                old.stop.assert_called_once()
                self.assertIs(result, replacement)
        registry = RuntimeRegistry()
        same = SimpleNamespace(spec=self.spec(context_alert_at=1), is_alive=lambda: True, stop=Mock())
        registry._runtimes["p"] = same
        # context_alert_at is not spawn-pinned: the warm runtime is kept.
        self.assertIs(registry.get_or_start(self.spec(context_alert_at=350_000), CL.ClaudeAdapter()), same)
        same.stop.assert_not_called()

    def test_runtime_and_update_share_one_spawn_field_list(self):
        self.assertIs(S._SPAWN_PINNED_FIELDS, RT.SPAWN_PINNED_FIELDS)
        for field in ("disallowed_tools", "backend_options", "ultracode", "fallback_model", "extra_dirs",
                      "allowed_tools", "mcp_whitelist", "system_prompt_append", "profile_name"):
            self.assertIn(field, RT.SPAWN_PINNED_FIELDS)
        self.assertNotIn("context_alert_at", RT.SPAWN_PINNED_FIELDS)

    def test_unchanged_options_keep_the_warm_runtime(self):
        # Both specs are read back from the registry: unchanged, non-empty
        # values must compare equal, or every send would respawn the runtime
        # and kill its background work.
        report, _ = self.create(
            "warm", model="opus", disallowed_tools=["Artifact", "Bash(git push *)"], ultracode=True,
            backend_options={"settings": '{"permissions": {"deny": ["WebFetch"]}, "env": {"A": "1"}}',
                             "args": ["--max-budget-usd", "5"]},
            allowed_tools=["Read"], extra_dirs=[str(self.directory / "extra")], fallback_model="sonnet",
            system_prompt_append="Focus.", context_alert_at=350_000)
        registry = RuntimeRegistry()
        same = SimpleNamespace(spec=R.get_pair("warm"), is_alive=lambda: True, stop=Mock())
        registry._runtimes["warm"] = same
        R.update_pair("warm", turn_count=7, purpose="later", self_woken_pending=[{"task_id": "t"}])
        tool(S.pair_update)("warm", context_alert_at=300_000)
        self.assertIs(registry.get_or_start(R.get_pair("warm"), CL.ClaudeAdapter()), same)
        same.stop.assert_not_called()


# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------

class RegistryRoundTripTests(PairOptionsCase):

    def raw(self):
        return json.loads(R.registry_path().read_text(encoding="utf-8"))["pairs"]

    def test_new_fields_and_unknown_keys_round_trip(self):
        R.registry_path().write_text(json.dumps({"version": R.REGISTRY_VERSION, "pairs": {"p": {
            "name": "p", "session_id": "s", "model": "opus", "backend": "claude",
            "disallowed_tools": ["Artifact"], "context_alert_at": 350_000,
            "backend_options": {"settings": {"a": 1}, "args": ["--max-budget-usd", "4"]},
            "future_field": {"from": "a newer process"},
        }}}), encoding="utf-8")
        spec = R.get_pair("p")
        self.assertEqual(spec.disallowed_tools, ["Artifact"])
        self.assertEqual(spec.context_alert_at, 350_000)
        R.update_pair("p", purpose="touch")  # forces a save
        raw = self.raw()["p"]
        self.assertEqual(raw["disallowed_tools"], ["Artifact"])
        self.assertEqual(raw["context_alert_at"], 350_000)
        self.assertEqual(raw["backend_options"], {"settings": {"a": 1}, "args": ["--max-budget-usd", "4"]})
        self.assertEqual(raw["future_field"], {"from": "a newer process"})

    def test_unset_fields_never_reach_the_file(self):
        # exclude_none: a v0.16 process sharing the registry never sees the
        # new keys unless a pair actually uses them.
        R.add_pair(self.spec())
        self.assertNotIn("disallowed_tools", self.raw()["p"])
        self.assertNotIn("context_alert_at", self.raw()["p"])
        tool(S.pair_update)("p", context_alert_at=350_000, disallowed_tools=["Artifact"])
        self.assertEqual(self.raw()["p"]["context_alert_at"], 350_000)
        tool(S.pair_update)("p", context_alert_at=0, disallowed_tools=[])
        self.assertNotIn("context_alert_at", self.raw()["p"])
        self.assertNotIn("disallowed_tools", self.raw()["p"])


# ---------------------------------------------------------------------------
# profile_name + system_prompt_append
# ---------------------------------------------------------------------------

class SystemPromptTests(PairOptionsCase):

    def test_profile_text_then_append_separated_by_a_blank_line(self):
        (R.profiles_dir() / "reviewer.md").write_text("\nYou review diffs.\n\n", encoding="utf-8")
        adapter = CL.ClaudeAdapter()
        spec = self.spec(profile_name="reviewer", system_prompt_append="  Focus on tests.  ")
        self.assertEqual(adapter._resolve_system_prompt(spec), "You review diffs.\n\nFocus on tests.")
        args = adapter._common_create_args(spec)
        self.assertEqual(args[args.index("--append-system-prompt") + 1], "You review diffs.\n\nFocus on tests.")
        self.assertEqual(adapter._resolve_system_prompt(self.spec(profile_name="reviewer")), "You review diffs.")

    def test_missing_profile_leaves_only_the_append(self):
        adapter = CL.ClaudeAdapter()
        self.assertEqual(adapter._resolve_system_prompt(
            self.spec(profile_name="absent", system_prompt_append="Focus.")), "Focus.")
        self.assertIsNone(adapter._resolve_system_prompt(self.spec(profile_name="absent")))


if __name__ == "__main__":
    unittest.main()
