"""Offline launch-policy regressions, with real Windows hidden-desktop controls."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
HOME = tempfile.TemporaryDirectory(prefix="cs-spawn-tests-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")

from claude_squared import process_spawn as P
from claude_squared.adapters import codex as C, claude as L
from claude_squared.adapters.appserver import AppServerClient


WINDOWS = SimpleNamespace(
    CREATE_NEW_CONSOLE=0x10, CREATE_NO_WINDOW=0x08000000,
    DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200,
    STARTF_USESHOWWINDOW=1, SW_HIDE=0,
    STARTUPINFO=lambda: SimpleNamespace(dwFlags=0, wShowWindow=1),
)


class SpawnPolicyTests(unittest.TestCase):
    def test_posix_options_pass_through_without_mutating_caller(self):
        original = {"stdin": object(), "start_new_session": True}
        with patch.object(P, "os", SimpleNamespace(name="posix")):
            result = P.hidden_process_kwargs(original)
        self.assertEqual(result, original)
        self.assertIsNot(result, original)

    def test_hidden_console_keeps_handles_and_unrelated_flags(self):
        info = WINDOWS.STARTUPINFO()
        info.dwFlags = 0x100  # USESTDHANDLES must survive
        stream = object()
        original = {"startupinfo": info, "stdin": stream, "stdout": stream, "stderr": stream,
                    "creationflags": 0x4000 | 0x08000000 | 0x200 | 0x8}
        with patch.object(P, "os", SimpleNamespace(name="nt")), patch.object(P, "subprocess", WINDOWS):
            result = P.hidden_process_kwargs(original)
        self.assertEqual(result["creationflags"], 0x4000 | 0x10)
        self.assertEqual(result["startupinfo"].dwFlags, 0x101)
        self.assertEqual(result["startupinfo"].wShowWindow, 0)
        self.assertIs(result["stdin"], stream)
        self.assertIs(result["stdout"], stream)
        self.assertIs(result["stderr"], stream)
        self.assertEqual(info.dwFlags, 0x100)
        self.assertEqual(info.wShowWindow, 1)

    def test_leaf_has_no_console_and_copies_startupinfo(self):
        with patch.object(P, "os", SimpleNamespace(name="nt")), patch.object(P, "subprocess", WINDOWS):
            result = P.hidden_process_kwargs({"creationflags": 0x10 | 0x200}, console=False)
        self.assertEqual(result["creationflags"], 0x08000000)
        self.assertEqual(result["startupinfo"].dwFlags, 1)
        self.assertEqual(result["startupinfo"].wShowWindow, 0)

    def test_codex_wrappers_apply_policy_on_both_retry_attempts(self):
        for wrapper, spawn_name in ((C.run_codex, "run"), (C.popen_codex, "Popen")):
            with self.subTest(wrapper=wrapper.__name__):
                sentinel = {"creationflags": 1234}
                with patch.object(C, "hidden_process_kwargs", return_value=sentinel) as hide, \
                        patch.object(C.subprocess, spawn_name, side_effect=[FileNotFoundError(), "ok"]) as spawn, \
                        patch.object(C.os.path, "isfile", return_value=False), \
                        patch.object(C, "codex_executable", return_value="fresh"):
                    self.assertEqual(wrapper(["gone"], stdin=subprocess.DEVNULL), "ok")
                hide.assert_called_once_with({"stdin": subprocess.DEVNULL})
                self.assertEqual(len(spawn.call_args_list), 2)
                self.assertTrue(all(call.kwargs == sentinel for call in spawn.call_args_list))

    def test_codex_version_is_a_hidden_leaf(self):
        with patch.object(C.subprocess, "run", return_value=SimpleNamespace(stdout="codex 1.2.3")) as spawn, \
                patch.object(C, "hidden_process_kwargs", return_value={"creationflags": 123}) as hide:
            self.assertEqual(C._codex_version("fake"), (1, 2, 3))
        hide.assert_called_once_with(console=False)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 123)

    def test_claude_help_is_a_hidden_leaf(self):
        with patch.dict(L._PERM_CHOICES_CACHE, clear=True), \
                patch.object(L, "_claude_executable", return_value="fake"), \
                patch.object(L, "claude_env", return_value={}), \
                patch.object(L, "hidden_process_kwargs", return_value={"creationflags": 123}) as hide, \
                patch.object(L.subprocess, "run", return_value=SimpleNamespace(stdout="")) as spawn:
            L._cli_permission_choices()
        hide.assert_called_once_with(console=False)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 123)

    def test_claude_one_shot_applies_inheritable_console_policy(self):
        with patch.object(L, "_claude_executable", return_value="fake"), \
                patch.object(L, "hidden_process_kwargs", return_value={"creationflags": 123}) as hide, \
                patch.object(L.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=b'{}', stderr=b"")) as spawn:
            L.ClaudeAdapter()._run_print([], timeout_seconds=5, pair_name="fixture", env={})
        hide.assert_called_once_with()
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 123)

    def test_appserver_uses_hidden_codex_wrapper_with_pipes(self):
        proc = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                               wait=lambda timeout: 0)
        with patch.object(C, "hidden_process_kwargs", side_effect=lambda kwargs: {**kwargs, "creationflags": 123}), \
                patch.object(C.subprocess, "Popen", return_value=proc) as spawn:
            with AppServerClient(["fake"], cwd=None, timeout_seconds=5, kill=lambda p: None):
                pass
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 123)
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.PIPE)

    @unittest.skip("manual positive console probe requires an independently armed Default-desktop trace")
    def test_real_console_inheritance_pipes_cancellation_and_survival(self):
        from hidden_desktop import run

        code, output = run([sys.executable, "-B", str(ROOT / "tests" / "fixtures" / "windows_console_probe.py"),
                            "suite"], timeout=90)
        self.assertEqual(code, 0, output)
        evidence = json.loads(output.strip())
        self.assertEqual(evidence["tree_kill"], 3)
        self.assertTrue(evidence["file_backed_survival"])
        self.assertEqual(evidence["leaf_launches"], 5)
        print("Windows console controls: " + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
