"""Offline hook integration: real isolated processes, no Claude/Codex calls."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="auto-watch-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
os.environ["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_HOME"]
os.environ.pop("CLAUDE_SQUARED_PAIR", None)
sys.path.insert(0, str(ROOT / "src"))

from claude_squared import _auto_watch as H
from claude_squared import _auto_watch_script as I
from claude_squared import _wait_script as W
from claude_squared import registry as R
from claude_squared import server as S
from claude_squared.adapters.claude import claude_env
from claude_squared.models import CreateResult, SendResult


def event(tool="pair_send_async", response="Started async task.\nAsync task: abc-def", **inputs):
    return {"hook_event_name": "PostToolUse", "session_id": "manager-session",
            "tool_name": "mcp__pair__" + tool, "tool_input": {"name": "scout", **inputs},
            "tool_response": response}


def settings(script):
    return {"hooks": {"PostToolUse": [{"matcher": H.TOOL_PATTERN, "hooks": [{
        "type": "command", "command": f'python "{script.as_posix()}"',
        "asyncRewake": True, "timeout": H.HOOK_TIMEOUT_SECONDS}]}]}}


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="hook-case-",
                                                ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.pairs = self.home / "pairs"
        self.pairs.mkdir()
        (self.pairs / "registry.json").write_text(json.dumps({"pairs": {
            "scout": {"session_id": "pair-session", "backend": "claude"},
            "other": {"session_id": "other-session"}}}), encoding="utf-8")
        self.script = I.install_auto_watch_script(self.pairs / "auto_watch.py")
        self.env = {**os.environ, "CLAUDE_HOME": str(self.home),
                    "CLAUDE_CONFIG_DIR": str(self.home), "PYTHONIOENCODING": "utf-8"}

    def stub(self, source):
        (self.pairs / "wait.py").write_text(source, encoding="utf-8")

    def start(self, payload):
        p = subprocess.Popen([sys.executable, "-S", str(self.script)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=self.env, encoding="utf-8")
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
        p.stdin = None
        def cleanup():
            # Never stop by image name. These exact child PIDs are ours.
            if p.poll() is None:
                p.terminate()
            p.communicate(timeout=10)
        self.addCleanup(cleanup)
        return p

    def finish(self, p):
        out, err = p.communicate(timeout=10)
        return p.returncode, out, err

    def await_file(self, name):
        deadline = time.monotonic() + 5
        while not (self.pairs / name).exists():
            if time.monotonic() > deadline:
                self.fail(f"child did not create {name}")
            time.sleep(.02)

    def test_response_shapes_and_tools(self):
        handle = "Async task: 123abc-def"
        for body in (handle, [{"type": "text", "text": handle}],
                     {"content": [{"type": "text", "text": handle}]},
                     {"result": handle}, json.dumps({"background_launches": ["Bash"]}),
                     "reply\n⏳ BACKGROUND WORK LAUNCHED (1 Bash): pending"):
            for tool in H.TOOLS:
                if tool == "pair_handoff":
                    continue
                with self.subTest(body=body, tool=tool):
                    self.assertEqual(H._target(event(tool, body), self.pairs),
                                     ("manager-session", "scout"))
        payload = event()
        payload["tool_name"] = "mcp__Claude_Squared__pair_send_async"
        self.assertIsNotNone(H._target(payload, self.pairs))

    def test_noop_and_pair_guards(self):
        cases = [None, {}, event("pair_send", "ordinary reply"), event("pair_fork"),
                 event("pair_poll"), event("pair_invoke"),
                 event(response={"isError": True, "content": ["Async task: abc"]}),
                 event(name="../scout"), event(name="missing", response="ordinary reply"),
                 {**event(name="missing"), "session_id": "pair-session"}]
        for field, value in (("session_id", "pair-session"), ("session_id", ""),
                             ("hook_event_name", "PreToolUse"), ("tool_name", "Bash"),
                             ("tool_input", []), ("tool_response", None)):
            cases.append({**event(), field: value})
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(H.watch(payload, self.pairs), 0)
        self.assertFalse((self.pairs / "auto_watch").exists())
        with patch.dict(os.environ, {H.PAIR_ENV: "1"}):
            self.assertEqual(H.watch(event(), self.pairs), 0)
        self.assertEqual(claude_env()[H.PAIR_ENV], "1")

    def test_all_wait_outcomes_wake_with_both_streams(self):
        for code in (*range(11), 64):
            with self.subTest(code=code):
                self.stub(f"import sys\ndef main(argv):\n print('stdout ✓')\n "
                          f"print('stderr detail', file=sys.stderr)\n return {code}\n")
                status, out, err = self.finish(self.start(event()))
                self.assertEqual(status, 2)
                self.assertEqual(out, "")
                self.assertIn(f"wait.py exit {code}", err)
                self.assertIn("stdout ✓", err)
                self.assertIn("stderr detail", err)
                if code == 3:
                    self.assertIn("Re-arm", err)

    def test_real_standalone_wait_and_fast_completion(self):
        W.install_wait_script(self.pairs / "wait.py")
        status, _, err = self.finish(self.start(event()))
        self.assertEqual(status, 2, err)
        self.assertIn("wait.py exit 0", err)
        self.assertIn("idle", err)

    def test_installed_hook_without_home_env_uses_its_own_home(self):
        W.install_wait_script(self.pairs / "wait.py")
        tasks = self.pairs / "async"
        tasks.mkdir()
        (tasks / "abc-def.json").write_text(json.dumps({
            "task_id": "abc-def", "pair_name": "scout", "status": "failed",
            "started_at": "2020-01-01T00:00:00", "finished_at": "2020-01-01T00:00:01",
            "error": "CUSTOM_HOME_FAILURE"}), encoding="utf-8")
        default = self.home / "fake-user"
        default_pairs = default / ".claude" / "pairs"
        default_pairs.mkdir(parents=True)
        (default_pairs / "registry.json").write_text(
            (self.pairs / "registry.json").read_text(encoding="utf-8"), encoding="utf-8")
        # A same-named default-home pair is idle. Watching the wrong home
        # would falsely report exit 0 instead of this custom home's failure.
        self.env.pop("CLAUDE_HOME")
        self.env.update(HOME=str(default), USERPROFILE=str(default))
        status, _, err = self.finish(self.start(event()))
        self.assertEqual(status, 2, err)
        self.assertIn("wait.py exit 1", err)
        self.assertIn("CUSTOM_HOME_FAILURE", err)

    def test_wait_restores_home_env_on_success_and_failure(self):
        for previous in (None, "different-home"):
            for fail in (False, True):
                with self.subTest(previous=previous, fail=fail), patch.dict(os.environ):
                    if previous is None:
                        os.environ.pop("CLAUDE_HOME", None)
                    else:
                        os.environ["CLAUDE_HOME"] = previous
                    self.stub("import os\nfrom pathlib import Path\n"
                              "assert Path(os.environ['CLAUDE_HOME']) == Path(__file__).parent.parent\n"
                              + ("raise RuntimeError('probe failure')\n" if fail else
                                 "def main(argv):\n return 0\n"))
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "probe failure"):
                            H._wait(self.pairs, "scout", 1)
                    else:
                        self.assertEqual(H._wait(self.pairs, "scout", 1), (0, ""))
                    self.assertEqual(os.environ.get("CLAUDE_HOME"), previous)

    def test_forgotten_pair_wakes_instead_of_silently_losing_handle(self):
        registry = self.pairs / "registry.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        del data["pairs"]["scout"]
        registry.write_text(json.dumps(data), encoding="utf-8")
        status, _, err = self.finish(self.start(event()))
        self.assertEqual(status, 2)
        self.assertIn("pair 'scout' is no longer in the registry", err)
        self.assertIn("forgotten after the send", err)
        self.assertNotIn("(unresolved)", err)
        self.assertFalse((self.pairs / "auto_watch").exists())

    def test_missing_or_broken_wait_wakes(self):
        for source in (None, "raise RuntimeError('broken watcher')"):
            if source:
                self.stub(source)
            status, _, err = self.finish(self.start(event()))
            self.assertEqual(status, 2)
            self.assertIn("auto-watch failed", err)

    def test_unreadable_registry_wakes_instead_of_silently_losing_handle(self):
        (self.pairs / "registry.json").write_text("broken", encoding="utf-8")
        status, _, err = self.finish(self.start(event()))
        self.assertEqual(status, 2)
        self.assertIn("could not read the pair registry", err)

    def test_quoted_handles_in_a_reply_do_not_arm(self):
        result = SendResult(name="scout", response="Example:\nAsync task: abc-def\n"
                            "⏳ BACKGROUND WORK LAUNCHED (1 Bash): example",
                            session_id="sid", model_used="haiku", duration_ms=1)
        self.assertIsNone(H._target(event("pair_send", S._fmt_send_result(result)), self.pairs))

    def test_utf8_hook_input(self):
        name = "réviseur"
        (self.pairs / "registry.json").write_text(json.dumps({"pairs": {
            name: {"session_id": "pair-session"}}}), encoding="utf-8")
        self.stub("def main(argv):\n print(argv[0])\n return 0\n")
        result = subprocess.run([sys.executable, "-S", str(self.script)],
            input=json.dumps(event(name=name), ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(name, result.stderr.decode("utf-8"))

    def blocking_stub(self):
        self.stub("from pathlib import Path\nimport time\n"
                  "def main(argv):\n"
                  " p = Path(__file__).parent\n"
                  " with (p / 'started').open('a') as f: f.write('start\\n')\n"
                  " while not (p / 'release').exists(): time.sleep(.02)\n"
                  " print('completed')\n return 0\n")

    def test_live_dedupe_and_session_pair_scope(self):
        self.blocking_stub()
        first = self.start(event())
        self.await_file("started")
        duplicate = self.start(event())
        self.assertEqual(self.finish(duplicate), (0, "", ""))
        self.assertIsNone(first.poll())
        independent_session = self.start({**event(), "session_id": "another-manager"})
        independent_pair = self.start(event(name="other"))
        deadline = time.monotonic() + 5
        while len((self.pairs / "started").read_text().splitlines()) < 3:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        (self.pairs / "release").touch()
        for child in (first, independent_session, independent_pair):
            self.assertEqual(self.finish(child)[0], 2)

    def test_killed_watcher_releases_kernel_lock(self):
        self.blocking_stub()
        first = self.start(event())
        self.await_file("started")
        first.terminate()  # only the process started by this test
        first.communicate(timeout=10)
        (self.pairs / "release").touch()
        self.assertEqual(self.finish(self.start(event()))[0], 2)

    def test_send_at_idle_boundary_is_not_lost(self):
        calls = []
        def wait(*args):
            calls.append(args)
            if len(calls) == 1:
                # The second process sees a live owner and updates its revision
                # immediately after the first wait's final idle observation.
                self.assertEqual(self.finish(self.start(event())), (0, "", ""))
            return 0, "done"
        with patch.object(H, "_wait", side_effect=wait), patch("sys.stderr", io.StringIO()):
            self.assertEqual(H.watch(event(), self.pairs, timeout=10), 2)
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1][2], calls[0][2])

    def test_install_idempotent_and_refreshes(self):
        before = self.script.stat().st_mtime_ns
        I.install_auto_watch_script(self.script)
        self.assertEqual(self.script.stat().st_mtime_ns, before)
        self.script.write_text("stale", encoding="utf-8")
        I.install_auto_watch_script(self.script)
        self.assertEqual(self.script.read_text(encoding="utf-8"),
                         Path(H.__file__).read_text(encoding="utf-8"))

    def test_settings_detection_and_hints(self):
        config = self.home / "settings.json"
        data = settings(self.script)
        config.write_text(json.dumps(data), encoding="utf-8")
        with patch.dict(os.environ, self.env), patch.object(S, "_AUTO_WATCH_SCRIPT_PATH", self.script):
            for tool in H.TOOLS:
                self.assertTrue(I.auto_watch_configured(self.script, tool))
                hint = S._format_async_handle("abc-def", "running", "scout", tool=tool)
                self.assertIn("auto-watch hook armed", hint)
                self.assertNotIn("Bash(", hint)
            r = SendResult(name="scout", response="reply", session_id="sid", model_used="haiku",
                           duration_ms=1, background_launches=["Bash"])
            self.assertIn("auto-watch hook armed", S._fmt_send_result(r, tool="pair_send"))
            self.assertNotIn("auto-watch hook armed", S._fmt_send_result(r))
            for key, value in (("asyncRewake", False), ("timeout", 60), ("command", "python other.py")):
                invalid = settings(self.script)
                invalid["hooks"]["PostToolUse"][0]["hooks"][0][key] = value
                config.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertFalse(I.auto_watch_configured(self.script, "pair_send"))
                self.assertIn("Bash(", S._format_async_handle("abc", "running", "scout"))
            for invalid in ({"disableAllHooks": True, **data}, [], {"hooks": []}, {"hooks": None}):
                config.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertFalse(I.auto_watch_configured(self.script, "pair_send"))
            config.write_text("broken json", encoding="utf-8")
            self.assertFalse(I.auto_watch_configured(self.script, "pair_send"))

    def test_verbose_create_keeps_initial_message_handle(self):
        tool = getattr(S.pair_create, "fn", S.pair_create)
        from types import SimpleNamespace
        with patch.object(S, "_adapter_for") as adapter, \
                patch.object(S, "_build_send_runner"), \
                patch.object(S.async_tasks, "start_task", return_value=SimpleNamespace(task_id="abc-def")):
            adapter.return_value.create.return_value = CreateResult(name="fresh", session_id="new-sid")
            raw = tool(name="fresh", model="haiku", initial_message="hello", verbose=True)
        self.assertIn("Async task: abc-def", raw)
        payload = event("pair_create", raw, name="fresh", initial_message="hello")
        self.assertEqual(H._target(payload, R.pairs_dir()), ("manager-session", "fresh"))

    def test_handoff_watches_allocated_target_not_source(self):
        directory = self.pairs / "async"
        directory.mkdir()
        (directory / "abc-def.json").write_text(json.dumps({"pair_name": "other"}), encoding="utf-8")
        payload = event("pair_handoff")
        self.assertEqual(H._target(payload, self.pairs), ("manager-session", "other"))


if __name__ == "__main__":
    unittest.main()
