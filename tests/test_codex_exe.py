"""Offline: the Codex binary is re-resolved when the Codex app updates it.

The Codex app's updater installs a new ``bin/<hash>/codex.exe``, rewrites
CODEX_CLI_PATH in config.toml and deletes the old folder. A long-lived MCP
process used to cache the first path it found, so every later Codex spawn
failed with WinError 2 until a restart (2026-09-27). Fake binaries and
isolated stores only; ``sys.executable`` stands in for a working codex.
"""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-exe-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import registry as R
from claude_squared.adapters import codex as C
from claude_squared.adapters.appserver import AppServerClient
from claude_squared.errors import CLIError
from claude_squared.models import PairSpec


EVENTS = [
    {"type": "thread.started", "thread_id": "exe-thread"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "hello"}},
    {"type": "turn.completed", "usage": {"input_tokens": 40, "output_tokens": 10}},
]


class CodexExeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.codex_home = self.dir / "codex"
        self.codex_home.mkdir()
        self.local = self.dir / "local"
        env = {"CLAUDE_HOME": str(self.dir / "claude"), "CODEX_HOME": str(self.codex_home),
               "LOCALAPPDATA": str(self.local)}
        self.stack.enter_context(patch.dict(os.environ, env))
        os.environ.pop("CLAUDE_PAIR_CODEX_PATH", None)  # restored by patch.dict
        self.stack.enter_context(patch.dict(C._EXE_CACHE, clear=True))
        self.stack.enter_context(patch.object(C.shutil, "which", return_value=None))
        self.versions = self.stack.enter_context(patch.object(C, "_codex_version", return_value=(0, 155, 0)))

    # -- helpers -----------------------------------------------------------

    def fake_binary(self, name: str) -> str:
        path = self.local / "OpenAI" / "Codex" / "bin" / name / "codex.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really codex")
        return str(path)

    def point_config_at(self, exe: str) -> None:
        # What the Codex app writes (single-quoted TOML literal string).
        (self.codex_home / "config.toml").write_text(
            "[mcp_servers.pair]\ncommand = 'python'\n\n"
            f"[mcp_servers.pair.env]\nCODEX_CLI_PATH = '{exe}'\n", encoding="utf-8")

    def delete_binary(self, exe: str) -> None:
        Path(exe).unlink()
        Path(exe).parent.rmdir()

    # -- resolution --------------------------------------------------------

    def test_an_updated_config_path_is_followed_without_a_restart(self):
        old, new = self.fake_binary("80f78947ad880e6e"), self.fake_binary("faa963e871dd422c")
        self.point_config_at(old)
        self.assertEqual(C.codex_executable(), old)
        # The updater: new binary in place, config rewritten, old folder gone.
        self.point_config_at(new)
        self.delete_binary(old)
        self.assertEqual(C.codex_executable(), new)
        spec = PairSpec(name="exe", backend="codex", model="gpt-6-luna", session_id="t", cwd=str(self.dir))
        with patch.object(C, "resolve_codex_model", return_value=("gpt-6-luna", None)), \
                patch.object(C, "context_windows", return_value=None):
            args = C.CodexAdapter()._exec_args(spec, model=None, effort=None, permission_mode=None)
        self.assertEqual(args[0], new)

    def test_a_config_path_counts_only_once_its_file_exists(self):
        scanned = self.fake_binary("0ddb895c950eaeba")
        self.point_config_at(str(self.local / "OpenAI" / "Codex" / "bin" / "pending" / "codex.exe"))
        self.assertEqual(C.codex_executable(), scanned)

    def test_an_unreadable_config_falls_through_to_the_scan(self):
        scanned = self.fake_binary("faa963e871dd422c")
        (self.codex_home / "config.toml").write_text("[mcp_servers.pair.env\nCODEX_CLI_PATH = '", encoding="utf-8")
        self.assertEqual(C.codex_executable(), scanned)

    def test_the_scan_is_cached_only_while_its_binary_exists(self):
        first = self.fake_binary("80f78947ad880e6e")
        self.assertEqual(C.codex_executable(), first)
        scans = self.versions.call_count
        self.assertEqual(C.codex_executable(), first)
        self.assertEqual(self.versions.call_count, scans, "a live cached path must not rescan")
        self.delete_binary(first)
        second = self.fake_binary("faa963e871dd422c")
        self.assertEqual(C.codex_executable(), second)
        self.assertGreater(self.versions.call_count, scans)

    def test_refresh_rescans_even_when_the_cached_binary_exists(self):
        first = self.fake_binary("80f78947ad880e6e")
        self.assertEqual(C.codex_executable(), first)
        newer = self.fake_binary("faa963e871dd422c")
        self.versions.side_effect = lambda exe: (0, 156, 0) if exe == newer else (0, 155, 0)
        self.assertEqual(C.codex_executable(), first)
        self.assertEqual(C.codex_executable(refresh=True), newer)

    def test_nothing_found_is_not_cached(self):
        self.assertEqual(C.codex_executable(), "codex")
        self.assertNotIn("exe", C._EXE_CACHE)
        installed = self.fake_binary("faa963e871dd422c")
        self.assertEqual(C.codex_executable(), installed)

    def test_env_override_still_wins(self):
        self.point_config_at(self.fake_binary("faa963e871dd422c"))
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_PATH": sys.executable}):
            self.assertEqual(C.codex_executable(), sys.executable)

    # -- spawning ----------------------------------------------------------

    def test_popen_retries_once_when_the_binary_vanished(self):
        gone = str(self.dir / "gone" / "codex.exe")
        args = [gone, "-c", "print('ran')"]
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_PATH": sys.executable}):
            proc = C.popen_codex(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(out.strip(), b"ran")
        self.assertEqual(args[0], sys.executable, "callers must see the binary that ran")

    def test_popen_does_not_retry_when_the_fresh_path_is_the_same(self):
        gone = str(self.dir / "gone" / "codex.exe")
        with patch.object(C, "codex_executable", return_value=gone) as resolve:
            with self.assertRaises(FileNotFoundError):
                C.popen_codex([gone, "exec"], stdin=subprocess.DEVNULL)
        resolve.assert_called_once_with(refresh=True)

    def test_a_missing_working_directory_is_not_blamed_on_the_binary(self):
        missing_cwd = str(self.dir / "removed-worktree")
        args = [sys.executable, "-c", "pass"]
        with patch.object(C, "codex_executable") as resolve:
            with self.assertRaises(OSError) as caught:
                C.popen_codex(args, cwd=missing_cwd, stdin=subprocess.DEVNULL)
        resolve.assert_not_called()
        msg = C.codex_spawn_error(args, caught.exception, missing_cwd)
        self.assertIn("working directory does not exist", msg)
        self.assertNotIn("no Codex binary found", msg)

    def test_app_server_spawn_retries_with_the_fresh_binary(self):
        gone = str(self.dir / "gone" / "codex.exe")
        client = AppServerClient([gone, "-c", "import sys; sys.stdin.read()"], cwd=None,
                                 timeout_seconds=30, kill=C._tree_kill)
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_PATH": sys.executable}):
            with client:
                self.assertEqual(client.args[0], sys.executable)
                self.assertIsNone(client.proc.poll())
        self.assertIsNotNone(client.proc.poll())
        client.proc.stdout.close()
        client.proc.stderr.close()

    def test_app_server_error_names_the_missing_binary(self):
        gone = str(self.dir / "gone" / "codex.exe")
        with patch.object(C, "codex_executable", return_value=gone):
            with self.assertRaises(CLIError) as caught:
                with AppServerClient([gone, "app-server"], cwd=None, timeout_seconds=30, kill=C._tree_kill):
                    pass
        self.assertIn("could not start codex app-server", str(caught.exception))
        self.assertIn("no Codex binary found", str(caught.exception))


class CodexRunSpawnTests(unittest.TestCase):
    """``CodexAdapter._run``: the send path that failed in the report."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.dir / "claude"), "CODEX_HOME": str(self.dir / "codex"),
            "LOCALAPPDATA": str(self.dir / "local"),
        }))
        os.environ.pop("CLAUDE_PAIR_CODEX_PATH", None)
        self.stack.enter_context(patch.dict(C._EXE_CACHE, clear=True))
        self.spec = PairSpec(name="exe-run", backend="codex", model="gpt-6-luna", effort="low",
                             permission_mode="read-only", session_id="exe-thread", cwd=str(self.dir))
        R.add_pair(self.spec)
        self.adapter = C.CodexAdapter()
        self.log = R.logs_dir() / self.spec.name / "main.log"
        self.turns = R.logs_dir() / self.spec.name / "turns"

    def test_spawn_failure_is_logged_and_leaves_no_run_files(self):
        gone = str(self.dir / "gone" / "codex.exe")
        with patch.object(C, "codex_executable", return_value=gone), \
                patch.object(C.shutil, "which", return_value=None):
            with self.assertRaises(CLIError) as caught:
                self.adapter._run([gone, "exec", "-m", "gpt-6-luna"], self.spec, timeout_seconds=10,
                                  task_label="send", log=True, stdin_text="hi")
        self.assertIn(f"could not start codex ({gone})", str(caught.exception))
        self.assertIn("no Codex binary found", str(caught.exception))
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("=== CODEX TURN START (pair=exe-run, model=gpt-6-luna, exe=" + gone, lines[-2])
        self.assertIn("=== TURN ERROR: could not start codex", lines[-1])
        self.assertTrue(lines[-1].endswith("==="))
        self.assertEqual(list(self.turns.iterdir()), [], "no child ran: nothing to recover")

    def test_send_path_survives_the_binary_being_deleted_under_it(self):
        script = self.dir / "fake_codex.py"
        script.write_text("import json, sys\nsys.stdin.read()\n"
                          f"for e in {EVENTS!r}:\n print(json.dumps(e), flush=True)\n", encoding="utf-8")
        gone = str(self.dir / "gone" / "codex.exe")
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_PATH": sys.executable}):
            events, stderr, code, _ = self.adapter._run(
                [gone, "-u", str(script), "-m", "gpt-6-luna"], self.spec, timeout_seconds=30,
                task_label="send", log=True, stdin_text="hi")
        self.assertEqual(code, 0, stderr)
        self.assertEqual([e["type"] for e in events if not e["type"].startswith("_")],
                         [e["type"] for e in EVENTS])
        log = self.log.read_text(encoding="utf-8")
        self.assertIn(f"exe={sys.executable}) ===", log)
        self.assertIn("=== TURN COMPLETED", log)


if __name__ == "__main__":
    unittest.main()
