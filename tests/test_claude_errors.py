"""CLI failure formatting without launching a CLI or reading live credentials."""
from contextlib import ExitStack
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
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="claude-errors-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))
from claude_squared.adapters import claude as C
from claude_squared import registry as R, runtime as RT, server as S
from claude_squared.models import PairSpec
from claude_squared.errors import CLIError

MESSAGE = "Failed to authenticate: OAuth session expired and could not be refreshed"
EXPIRED = {"is_error": True, "terminal_reason": "api_error", "result": MESSAGE,
           "usage": {"input_tokens": 0, "output_tokens": 0}}


class ClaudeErrors(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(dir=HOME.name, ignore_cleanup_errors=True))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": directory}))
        self.spec = PairSpec(name="fixture", session_id="fixture-session", model="haiku", permission_mode="auto")
        self.adapter = C.ClaudeAdapter()
        self.stack.enter_context(patch.object(C.ClaudeAdapter, "_spawn_env", return_value={}))
        self.stack.enter_context(patch.object(C, "_claude_executable", return_value="fixture-claude"))
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("No live CLI")))
        self.run = self.stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout=json.dumps(EXPIRED).encode(), stderr=b"")))

    def assert_login(self, text):
        self.assertIn("the claude CLI is not logged in", text)
        self.assertIn(MESSAGE, text)
        self.assertIn("`claude auth login`", text)
        self.assertIn("pairs can't sign in themselves", text)
        self.assertNotIn('"terminal_reason"', text)

    def test_exact_manager_shape_across_lifecycle_and_transport_calls(self):
        self.stack.enter_context(patch.object(self.adapter, "session_exists", return_value=True))
        operations = [lambda: self.adapter.create(self.spec), lambda: self.adapter.fork(self.spec, "sentinel"),
                      lambda: self.adapter.compact(self.spec), lambda: self.adapter.context(self.spec),
                      lambda: self.adapter.send(self.spec, "test", model="haiku"),
                      lambda: self.adapter._run_print(["--print"], timeout_seconds=5, pair_name="fixture")]
        for code in (0, 1):
            for typed in (False, True):
                event = dict(EXPIRED, **({"type": "result"} if typed else {}))
                self.run.return_value = SimpleNamespace(returncode=code, stdout=json.dumps(event).encode(), stderr=b"")
                for operation in operations:
                    with self.subTest(code=code, typed=typed), self.assertRaises(CLIError) as caught:
                        operation()
                    self.assert_login(str(caught.exception))

    def test_clear_and_hard_stop_surface_create_failure_without_rotating_registry(self):
        R.add_pair(self.spec)
        fake_runtime = SimpleNamespace(get_or_none=Mock(return_value=None), evict=Mock(return_value=[]))
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=fake_runtime))
        self.stack.enter_context(patch.object(S, "_adapter_for", return_value=self.adapter))
        self.stack.enter_context(patch.object(S, "_pending_work_split", return_value=([], [])))
        self.stack.enter_context(patch.object(S, "_refuse_if_background"))
        clear = getattr(S.pair_clear, "fn", S.pair_clear)
        stop = getattr(S.pair_stop, "fn", S.pair_stop)
        with self.assertRaises(CLIError) as caught:
            clear(self.spec.name, archive_old=False)
        self.assert_login(str(caught.exception))
        with self.assertRaises(CLIError) as caught:
            stop(self.spec.name, hard=True)
        self.assert_login(str(caught.exception))
        self.assertEqual(R.get_pair(self.spec.name).session_id, self.spec.session_id)

    def test_result_message_precedes_large_json_and_unrelated_stderr(self):
        output = json.dumps({"type": "system", "subtype": "init", "padding": "x" * 5000}) + "\n" + json.dumps(dict(EXPIRED, type="result"))
        text = C.claude_failure_message(self.spec, output, "noise from initialization")
        self.assert_login(text)
        generic = C.claude_failure_message(self.spec, json.dumps({"type": "result", "is_error": True,
            "padding": "x" * 5000, "result": "The actual failure"}), "unrelated stderr")
        self.assertEqual(generic, "The actual failure")

    def test_plain_and_structured_auth_signals_and_negative_controls(self):
        for stdout, stderr in [(json.dumps({"loggedIn": False}), ""),
                               (json.dumps({"loggedIn": False, "result": "Please authenticate"}), ""),
                               ("", json.dumps(EXPIRED))]:
            self.assertIn("claude auth login", C.claude_failure_message(self.spec, stdout, stderr))
        quoted = json.dumps({"type": "assistant", "message": {"content": MESSAGE}})
        last = json.dumps({"type": "result", "is_error": True, "result": "Too many turns"})
        self.assertEqual(C.claude_failure_message(self.spec, quoted + "\n" + last, ""), "Too many turns")
        self.assertNotIn("claude auth login", C.claude_failure_message(self.spec, quoted, ""))

    def test_mcp_diagnostics_and_successful_quoted_results_are_not_claude_auth_failures(self):
        mcp = "[MCP github] Failed to authenticate: GitHub token expired"
        for stdout, stderr in [("", mcp), (mcp, ""), ("", "not logged in"), ("", "Invalid API key"),
                (json.dumps({"type": "result", "is_error": True, "result": mcp}), ""),
                (json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Invalid API key"}), MESSAGE)]:
            with self.subTest(stdout=stdout, stderr=stderr):
                text = C.claude_failure_message(self.spec, stdout, stderr)
                self.assertNotIn("claude auth login", text)
                self.run.return_value = SimpleNamespace(returncode=1, stdout=stdout.encode(), stderr=stderr.encode())
                with self.assertRaises(CLIError) as caught:
                    self.adapter._run_print(["--print"], timeout_seconds=5, pair_name="fixture", env_spec=self.spec)
                self.assertNotIn("claude auth login", str(caught.exception))
        api = self.spec.model_copy(update={"billing": "api"})
        success = json.dumps({"type": "result", "subtype": "success", "result": "Invalid API key"})
        self.assertEqual(C.claude_failure_message(api, success, ""), "Invalid API key")

    def test_api_error_zero_usage_is_evidence_but_embedded_tool_failure_is_not(self):
        event = {"type": "result", "terminal_reason": "api_error", "usage": {"input_tokens": 0, "output_tokens": 0},
                 "result": "The request failed: OAuth session expired and could not be refreshed"}
        self.assertIn("claude auth login", C.claude_failure_message(self.spec, json.dumps(event), ""))
        event["usage"]["input_tokens"] = 30
        self.assertNotIn("claude auth login", C.claude_failure_message(self.spec, json.dumps(event), ""))
        event.update(is_error=True, result="The tool returned: Failed to authenticate: GitHub token expired")
        event["usage"]["input_tokens"] = 0
        self.assertNotIn("claude auth login", C.claude_failure_message(self.spec, json.dumps(event), ""))

    def test_warm_send_maps_only_error_results_and_preserves_api_key_guidance(self):
        with patch.object(self.adapter, "_read_last_turn_context_fill", return_value=None):
            result = self.adapter._build_send_result(self.spec, dict(EXPIRED, type="result"))
            self.assert_login(result.response)
            self.assertNotIn("retry usually resolves", result.safety_signal)
            answer = self.adapter._build_send_result(self.spec, {"result": MESSAGE})
            self.assertIsNone(answer.safety_signal)
            self.assertEqual(answer.response, MESSAGE)
            api = self.spec.model_copy(update={"billing": "api"})
            result = self.adapter._build_send_result(api, {"result": "Invalid API key", "is_error": True})
            self.assertEqual(result.safety_kind, "api_key_rejected")
            text = C.claude_failure_message(api, "", "Invalid API key")
            self.assertIn("rejected the key", text)
            self.assertNotIn("claude auth login", text)

    def test_runtime_transport_errors_keep_supervision_prefix_and_auth_guidance(self):
        error = RT._runtime_cli_error(self.spec, "CRASHED: runtime exited", json.dumps(EXPIRED), 1)
        self.assertTrue(str(error).startswith("CRASHED: "))
        self.assert_login(str(error))


if __name__ == "__main__":
    unittest.main()
