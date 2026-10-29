"""Offline hook integration: real isolated processes, no Claude/Codex calls."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import threading
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

    def start(self, payload, *args):
        p = subprocess.Popen([sys.executable, "-S", str(self.script), *args], stdin=subprocess.PIPE,
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
        out, err = p.communicate(timeout=20)
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

    def test_timeout_and_alert_rearm_without_inherited_home(self):
        custom_home = self.home / "custom home 'quoted'"
        pairs = custom_home / "pairs"
        tasks = pairs / "async"
        tasks.mkdir(parents=True)
        (pairs / "registry.json").write_text(
            (self.pairs / "registry.json").read_text(encoding="utf-8"), encoding="utf-8")
        (tasks / "abc-def.json").write_text(json.dumps({
            "task_id": "abc-def", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        script = W.install_wait_script(pairs / "wait.py")
        default = self.home / "fake-user"
        default_pairs = default / ".claude" / "pairs"
        default_pairs.mkdir(parents=True)
        (default_pairs / "registry.json").write_text(
            (pairs / "registry.json").read_text(encoding="utf-8"), encoding="utf-8")
        with patch.dict(os.environ):
            os.environ.pop("CLAUDE_HOME", None)
            code, output = H._wait(pairs, "scout", 0)
        self.assertEqual(code, 3, output)
        commands = [output.split("Re-arm:\n", 1)[1].strip()]
        watcher = runpy.run_path(str(script))
        with patch.dict(os.environ, {"CLAUDE_HOME": str(custom_home)}), \
                patch.object(sys, "argv", [str(script)]):
            alert = watcher["_alert_message"]("scout", 830_000, 1_000_000, 820_000,
                                               ["scout", "--timeout", "0"])
        for label in ("Keep watching:  ", "or without the alert:  "):
            commands.append(next(line.split(label, 1)[1] for line in alert.splitlines() if label in line))
        self.assertIn("--context-alert", commands[1])
        self.assertIn("--no-context-alert", commands[2])
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(command.startswith("CLAUDE_HOME="), command)
                # Apply the printed Bash assignment without requiring Bash on
                # every test host; execute the remaining argv as a real child.
                assignment, *argv = shlex.split(command)
                key, value = assignment.split("=", 1)
                env = {**self.env, "HOME": str(default), "USERPROFILE": str(default)}
                env.pop("CLAUDE_HOME", None)
                env[key] = value
                result = subprocess.run(argv, env=env, capture_output=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("still working", result.stderr)

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
        self.stub("from pathlib import Path\nimport time,os\n"
                  "def main(argv):\n"
                  " p = Path(__file__).parent\n"
                  " (p / ('started-' + str(os.getpid()))).touch()\n"
                  " while not (p / 'release').exists(): time.sleep(.02)\n"
                  " print('completed')\n return 0\n")

    def test_live_dedupe_and_session_pair_scope(self):
        self.blocking_stub()
        first = self.start({**event(), "cwd": str(self.home / "manager-project")})
        self.await_file("started-" + str(first.pid))
        files = list((self.pairs / "watches").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["pair"], "scout")
        self.assertEqual(record["session_id"], "manager-session")
        self.assertEqual(record["hook_pid"], first.pid)
        self.assertEqual(record["cwd"], str(self.home / "manager-project"))
        self.assertIsNotNone(H.datetime.fromisoformat(record["started_at"]))
        duplicate = self.start(event())
        self.assertEqual(self.finish(duplicate), (0, "", ""))
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), record)
        self.assertIsNone(first.poll())
        independent_session = self.start({**event(), "session_id": "another-manager"})
        independent_pair = self.start(event(name="other"))
        # Two fresh interpreters must start; slow CI runners took >5 s.
        deadline = time.monotonic() + 20
        # Per-PID markers avoid concurrent Windows appenders losing a line in
        # a shared readiness file. Each marker proves a distinct wait started.
        while not all((self.pairs / ("started-" + str(child.pid))).exists()
                      for child in (independent_session, independent_pair)):
            for child in (independent_session, independent_pair):
                if child.poll() is not None:
                    self.fail(f"independent watcher exited before waiting: {self.finish(child)}")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.assertEqual(len(list(self.pairs.glob("started-*"))), 3)
        (self.pairs / "release").touch()
        for child in (first, independent_session, independent_pair):
            self.assertEqual(self.finish(child)[0], 2)
        self.assertEqual(list((self.pairs / "watches").glob("*.json")), [])

    def test_display_record_failure_does_not_prevent_wake(self):
        (self.pairs / "watches").write_text("not a directory", encoding="utf-8")
        self.stub("def main(argv):\n print('completed')\n return 0\n")
        status, _, err = self.finish(self.start(event()))
        self.assertEqual(status, 2)
        self.assertIn("wait.py exit 0", err)

    def test_display_record_is_removed_when_wait_raises(self):
        self.stub("raise RuntimeError('failed watcher')\n")
        self.assertEqual(self.finish(self.start(event()))[0], 2)
        self.assertEqual(list((self.pairs / "watches").glob("*.json")), [])

    def test_killed_watcher_releases_kernel_lock(self):
        self.blocking_stub()
        first = self.start(event())
        self.await_file("started-" + str(first.pid))
        first.terminate()  # only the process started by this test
        first.communicate(timeout=10)
        self.assertEqual(len(list((self.pairs / "watches").glob("*.json"))), 1)
        (self.pairs / "release").touch()
        self.assertEqual(self.finish(self.start(event()))[0], 2)
        self.assertEqual(list((self.pairs / "watches").glob("*.json")), [])

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

    def test_server_restores_a_script_another_install_replaced(self):
        installed = Path(os.environ["CLAUDE_HOME"]) / "pairs" / "auto_watch.py"
        # A child process: importing the server starts its own machinery.
        code = ("from pathlib import Path; from claude_squared import server; "
                "Path(server._AUTO_WATCH_SCRIPT_PATH).write_text('# older hook', encoding='utf-8'); "
                "print(server._current_auto_watch_script())")
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                                timeout=60, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), installed.resolve())
        self.assertEqual(installed.read_text(encoding="utf-8"), Path(H.__file__).read_text(encoding="utf-8"))

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

    def test_completed_handoff_reply_cannot_arm_from_quoted_handle(self):
        result = SendResult(name="other", response="Example:\nAsync task: abc-def\n"
                            "⏳ BACKGROUND WORK LAUNCHED (1 Bash): example",
                            session_id="other-session", model_used="gpt-6-luna", duration_ms=1)
        text = "Handed off 'scout' -> 'other'.\n\nProbe reply:\n" + S._fmt_send_result(result)
        for body in (text, {"content": [{"type": "text", "text": text}]},
                     json.dumps({"pair": {"name": "other"}, "task": {"status": "done"}, "text": text})):
            with self.subTest(body=body):
                self.assertEqual(self.finish(self.start(event("pair_handoff", body))), (0, "", ""))
        self.assertFalse((self.pairs / "auto_watch").exists())
        # If the server appends a real running handle after the embedded
        # reply, it must still arm for that task rather than the quoted one.
        directory = self.pairs / "async"
        directory.mkdir()
        (directory / "123abc.json").write_text(json.dumps({"pair_name": "other"}), encoding="utf-8")
        self.assertEqual(H._target(event("pair_handoff", text + "\n\nAsync task: 123abc"), self.pairs),
                         ("manager-session", "other"))

    def recovery_record(self, pair="scout", session="manager-session", **changes):
        record = {"version": 1, "pair": pair, "session_id": session, "cwd": "/old-project",
                  "hook_pid": 999999999, "started_at": "2020-01-01T00:00:00+00:00"}
        record.update(changes)
        path = self.pairs / "watches" / (H._watch_key(session, pair) + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def recovery_task(self, pair="scout", status="running"):
        path = self.pairs / "async" / (pair + "-task.json")
        path.parent.mkdir(exist_ok=True)
        data = {"task_id": pair + "-task", "pair_name": pair, "status": status,
                "owner_pid": os.getpid() if status == "running" else None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": "2020-01-01T00:00:01" if status != "running" else None,
                "result": {"response": "RECOVERED-" + pair}, "error": "FAILED-" + pair}
        # Atomic like the server, so a reader never sees a torn test update.
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        temp.replace(path)

    def start_recovery(self, source="resume", session="manager-session"):
        return self.start({"hook_event_name": "SessionStart", "source": source,
                           "session_id": session, "cwd": "/resumed-project"}, "--resume")

    def await_recovery_owner(self, path, pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("hook_pid") == pid:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.02)
        self.fail("recovery did not claim the dead watch")

    def test_recovery_reports_outcome_finished_during_downtime(self):
        W.install_wait_script(self.pairs / "wait.py")
        for status, code in (("done", 0), ("failed", 1), ("stopped", 5)):
            with self.subTest(status=status):
                path = self.recovery_record()
                self.recovery_task(status=status)
                result, _, err = self.finish(self.start_recovery())
                self.assertEqual(result, 2, err)
                self.assertIn(f"wait.py exit {code}", err)
                self.assertFalse(path.exists())
                self.assertEqual(self.finish(self.start_recovery()), (0, "", ""))

    def test_recovery_claims_busy_pair_and_clear_compact_do_not_duplicate(self):
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record()
        self.recovery_task()
        first = self.start_recovery()
        self.await_recovery_owner(path, first.pid)
        record = path.read_bytes()
        for source in H.START_SOURCES:
            self.assertEqual(self.finish(self.start_recovery(source)), (0, "", ""))
        self.assertEqual(path.read_bytes(), record)
        self.recovery_task(status="done")
        code, _, err = self.finish(first)
        self.assertEqual(code, 2, err)
        self.assertIn("pair 'scout', wait.py exit 0", err)
        self.assertFalse(path.exists())

    def test_completed_pair_wakes_without_waiting_for_other_busy_pair(self):
        W.install_wait_script(self.pairs / "wait.py")
        done = self.recovery_record()
        busy = self.recovery_record("other")
        self.recovery_task(status="done")
        self.recovery_task("other")
        code, _, err = self.finish(self.start_recovery())
        self.assertEqual(code, 2, err)
        self.assertIn("pair 'scout', wait.py exit 0", err)
        self.assertFalse(done.exists())
        self.assertTrue(busy.exists())
        self.assertIn("Still-unwatched recovered pairs: other", err)
        # Reading the result re-arms the remaining pair without a Bash step.
        follow = self.start({**event("pair_poll", "done"), "tool_input": {"task_id": "scout"}})
        self.await_recovery_owner(busy, follow.pid)
        self.recovery_task("other", status="done")
        code, _, err = self.finish(follow)
        self.assertEqual(code, 2, err)
        self.assertIn("pair 'other', wait.py exit 0", err)
        self.assertNotIn("pair 'scout', wait.py exit", err)
        self.assertFalse(busy.exists())

    def test_recovery_explains_gone_pair_and_removes_record(self):
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record("gone")
        code, _, err = self.finish(self.start_recovery())
        self.assertEqual(code, 2, err)
        self.assertIn("pair 'gone' is gone from the registry", err)
        self.assertFalse(path.exists())

    def test_recovery_keeps_other_session_and_pair_session_records(self):
        W.install_wait_script(self.pairs / "wait.py")
        foreign = self.recovery_record(session="old-session")
        pair = self.recovery_record(session="pair-session")
        self.assertEqual(self.finish(self.start_recovery()), (0, "", ""))
        self.assertEqual(self.finish(self.start_recovery(session="pair-session")), (0, "", ""))
        self.assertTrue(foreign.exists())
        self.assertTrue(pair.exists())

    def test_recovery_survives_another_hook_death(self):
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record()
        self.recovery_task()
        first = self.start_recovery()
        self.await_recovery_owner(path, first.pid)
        first.terminate()  # only the hook process this test launched
        first.communicate(timeout=10)
        self.assertTrue(path.exists())
        second = self.start_recovery()
        self.await_recovery_owner(path, second.pid)
        self.recovery_task(status="done")
        self.assertEqual(self.finish(second)[0], 2)

    def test_recovery_uses_installed_home_without_environment_override(self):
        W.install_wait_script(self.pairs / "wait.py")
        self.recovery_record()
        self.recovery_task(status="failed")
        self.env.pop("CLAUDE_HOME")
        code, _, err = self.finish(self.start_recovery())
        self.assertEqual(code, 2)
        self.assertIn("FAILED-scout", err)

    def test_interrupted_watch_keeps_record_for_restart(self):
        with patch.object(H, "_wait", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            H.watch(event(), self.pairs)
        self.assertEqual(len(list((self.pairs / "watches").glob("*.json"))), 1)

    def test_recovery_timeout_keeps_resolved_home_in_rearm_command(self):
        W.install_wait_script(self.pairs / "wait.py")
        self.recovery_record()
        self.recovery_task()
        output = io.StringIO()
        with patch("sys.stderr", output):
            code = H.resume_watches({"hook_event_name": "SessionStart", "source": "resume",
                                     "session_id": "manager-session"}, self.pairs, timeout=0)
        self.assertEqual(code, 2)
        self.assertIn("wait.py exit 3", output.getvalue())
        self.assertIn("CLAUDE_HOME=", output.getvalue())
        self.assertIn(self.home.as_posix(), output.getvalue())

    def test_two_restart_hooks_claim_a_stale_record_only_once(self):
        W.install_wait_script(self.pairs / "wait.py")
        self.recovery_record()
        self.recovery_task(status="done")
        first, second = self.start_recovery(), self.start_recovery()
        results = [self.finish(first), self.finish(second)]
        self.assertEqual(sorted(result[0] for result in results), [0, 2], results)

    def test_recovery_preserves_record_when_registry_is_unreadable(self):
        W.install_wait_script(self.pairs / "wait.py")
        record = self.recovery_record()
        (self.pairs / "registry.json").write_text("broken", encoding="utf-8")
        code, _, err = self.finish(self.start_recovery())
        self.assertEqual(code, 2)
        self.assertIn("registry unavailable", err)
        self.assertTrue(record.exists())

    def test_failed_unlink_has_a_terminal_receipt_not_a_recoverable_watch(self):
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record()
        data = json.loads(path.read_text(encoding="utf-8"))
        original = Path.unlink
        def held(target, *args, **kwargs):
            if target == path:
                raise PermissionError("watch file held open")
            return original(target, *args, **kwargs)
        with patch.object(Path, "unlink", held):
            H._retire_watch(path, data)
        self.assertTrue(path.exists())
        self.assertTrue(path.with_suffix(".done").exists())
        self.assertEqual(self.finish(self.start_recovery()), (0, "", ""))
        self.assertEqual(self.finish(self.start(event("pair_poll", "done"))), (0, "", ""))
        # A later watch of this pair has its own identity, so an old receipt
        # must not suppress that new work's eventual notification.
        self.recovery_record(started_at=datetime.now(timezone.utc).isoformat())
        self.recovery_task(status="done")
        self.assertEqual(self.finish(self.start_recovery())[0], 2)

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing regression")
    def test_windows_open_handle_does_not_rewake_completed_recovery(self):
        import ctypes
        from ctypes import wintypes
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.recovery_task(status="done")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # Share read/write, but NOT delete. Both unlink and atomic replacement
        # of this watch file fail while the handle is held.
        handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            code = H.watch({"cwd": "/manager"}, self.pairs, recover=data,
                           reader=runpy.run_path(str(self.pairs / "wait.py")),
                           cancel=H.threading.Event(), output=io.StringIO())
            self.assertEqual(code, 2)
            self.assertTrue(path.exists())
            self.assertTrue(H._completed_record(path, data))
            self.assertEqual(self.finish(self.start_recovery()), (0, "", ""))
            self.assertEqual(self.finish(self.start(event("pair_poll", "done"))), (0, "", ""))
        finally:
            kernel.CloseHandle(handle)


    def test_recovery_registry_retries_and_baseexception_always_reports(self):
        W.install_wait_script(self.pairs / "wait.py")
        path = self.recovery_record()
        self.recovery_task(status="done")
        original = H._read
        attempts = []
        def retry(file):
            if file.name == "registry.json":
                attempts.append(file)
                if len(attempts) <= 2:
                    return None
            return original(file)
        payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": "manager-session"}
        with patch.object(H, "_read", side_effect=retry), patch("sys.stderr", io.StringIO()):
            self.assertEqual(H.resume_watches(payload, self.pairs), 2)
        self.assertGreaterEqual(len(attempts), 3)
        self.recovery_record()
        result = []
        errors = io.StringIO()
        with patch.object(H, "watch", side_effect=SystemExit(17)), patch("sys.stderr", errors):
            worker = threading.Thread(target=lambda: result.append(H.resume_watches(payload, self.pairs)), daemon=True)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive(), "BaseException left recovery waiting for a missing result")
        self.assertEqual(result, [2])
        self.assertIn("SystemExit", errors.getvalue())
        self.assertTrue(path.exists())

    def test_publish_retries_permission_error_and_expiry_does_not_read_json(self):
        original = Path.replace
        attempts = []
        def replace(path, target):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError("atomic replacement temporarily held")
            return original(path, target)
        key = H._watch_key("manager-session", "scout")
        with patch.object(Path, "replace", replace):
            published = H._publish_watch(self.pairs, key, "manager-session", "scout", {})
        self.assertIsNotNone(published)
        self.assertEqual(len(attempts), 3)
        path = published[0]
        os.utime(path, (time.time() - H.HOOK_TIMEOUT_SECONDS - 1,) * 2)
        original_read = H._read
        def read(file):
            self.assertNotEqual(file, path, "expired record JSON must not be read")
            return original_read(file)
        with patch.object(H, "_read", side_effect=read):
            self.assertEqual(H.resume_watches({"hook_event_name": "SessionStart", "source": "resume",
                "session_id": "manager-session"}, self.pairs), 0)
        self.assertFalse(path.exists())

    def test_host_identity_uses_ancestry_and_live_hook_with_dead_host_is_recoverable(self):
        W.install_wait_script(self.pairs / "wait.py")
        reader = runpy.run_path(str(self.pairs / "wait.py"))
        parent = os.getppid()
        with patch.object(H, "_process_parent", side_effect=[(parent + 1, "shell.exe"), (1, "claude.exe")]), \
                patch.dict(reader, {"_av_proc": lambda pid: (True, 123.0)}):
            self.assertEqual(H._host_identity(reader), {"host_pid": parent + 1, "host_started_at": 123.0})
        path = self.recovery_record(hook_pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat(),
                                    host_pid=999999999, host_started_at=123.0)
        self.assertTrue(H._dead_record(H._read(path), reader, path))
        info = H._process_parent(os.getpid())
        self.assertIsNotNone(info)
        self.assertEqual(info[0], os.getppid())

    def test_surviving_hook_exits_quietly_when_its_host_dies_and_can_be_recovered(self):
        W.install_wait_script(self.pairs / "wait.py")
        self.recovery_task()
        host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=self.env)
        def cleanup_host():
            if host.poll() is None:
                host.terminate()  # only this test's exact child PID
            host.wait(timeout=10)
        self.addCleanup(cleanup_host)
        reader = runpy.run_path(str(self.pairs / "wait.py"))
        alive, born = reader["_av_proc"](host.pid)
        self.assertTrue(alive)
        identity = {"host_pid": host.pid, "host_started_at": born}
        wrapper = self.pairs / "host-fixture.py"
        wrapper.write_text("import runpy,sys\n"
            f"d=runpy.run_path({str(self.script)!r})\n"
            f"d['watch'].__globals__['_host_identity']=lambda reader: {identity!r}\n"
            "from pathlib import Path\nimport time\n"
            "original=d['watch'].__globals__['_host_check']\n"
            "def guard(host, reader):\n"
            " try: original(host, reader)\n"
            " except d['_HostGone']:\n"
            "  Path(__file__).with_name('host-gone').touch()\n"
            "  while not Path(__file__).with_name('release-host').exists(): time.sleep(.02)\n"
            "  raise\n"
            "d['watch'].__globals__['_host_check']=guard\n"
            "sys.exit(d['main']())\n", encoding="utf-8")
        script, self.script = self.script, wrapper
        try:
            hook = self.start(event())
        finally:
            self.script = script
        key = H._watch_key("manager-session", "scout")
        self.await_file("watches/" + key + ".json")
        host.terminate()
        host.wait(timeout=10)
        self.await_file("host-gone")
        record = self.pairs / "watches" / (key + ".json")
        self.assertTrue(record.exists())
        self.assertFalse(record.with_suffix(".done").exists())
        resumed = self.start_recovery()
        gate = H._Lock(self.pairs / "auto_watch" / (key + ".gate"))
        self.addCleanup(gate.close)
        deadline = time.monotonic() + 5
        while gate.acquire():
            gate.release()
            if time.monotonic() > deadline:
                self.fail("recovery did not wait for the surviving hook's owner lock")
            time.sleep(.02)
        (self.pairs / "release-host").touch()
        self.assertEqual(self.finish(hook), (0, "", ""))
        self.await_recovery_owner(record, resumed.pid)
        self.recovery_task(status="done")
        self.assertEqual(self.finish(resumed)[0], 2)

    def test_immediate_probe_hook_uses_already_completed_fixture_without_delay(self):
        import smoke_live_sessionstart as probe
        (self.home / "finished-work.json").write_text('{"status":"done"}', encoding="utf-8")
        source = probe.hook_source("IMMEDIATE-MARKER", 99, True)
        errors = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"source":"resume","session_id":"fixture"}')), \
                patch("sys.stderr", errors), patch.object(time, "sleep", side_effect=AssertionError("must be immediate")):
            with self.assertRaises(SystemExit) as caught:
                exec(compile(source, "hook.py", "exec"), {"__file__": str(self.home / "hook.py")})
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("IMMEDIATE-MARKER", errors.getvalue())
        self.assertTrue((self.home / "hook-finished.json").exists())

    def test_empty_recovery_is_silent_without_registry_at_start_and_poll(self):
        registry = self.pairs / "registry.json"
        registry.unlink()
        payloads = [{"hook_event_name": "SessionStart", "source": source, "session_id": "manager-session"}
                    for source in H.START_SOURCES] + [event("pair_poll", "done")]
        for with_record in (False, True):
            if with_record:
                self.recovery_record()
            for payload in payloads:
                with self.subTest(with_record=with_record, payload=payload):
                    args = ("--resume",) if payload["hook_event_name"] == "SessionStart" else ()
                    self.assertEqual(self.finish(self.start(payload, *args)), (0, "", ""))

    def test_corrupt_registry_is_not_read_without_recoverable_session_records(self):
        (self.pairs / "registry.json").write_text("broken", encoding="utf-8")
        self.recovery_record(session="another-manager")
        W.install_wait_script(self.pairs / "wait.py")
        self.recovery_record(hook_pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat())
        output = io.StringIO()
        with patch.object(H, "_registry_entries", side_effect=AssertionError("unneeded registry read")), \
                patch("sys.stderr", output):
            self.assertEqual(H.resume_watches({"hook_event_name": "SessionStart", "source": "resume",
                "session_id": "manager-session"}, self.pairs), 0)
            self.assertEqual(H.resume_watches(event("pair_poll", "done"), self.pairs), 0)
        self.assertEqual(output.getvalue(), "")

    def test_host_start_shift_uses_waiters_one_sided_tolerance(self):
        W.install_wait_script(self.pairs / "wait.py")
        reader = runpy.run_path(str(self.pairs / "wait.py"))
        expected = 10000.0
        host = {"host_pid": 123, "host_started_at": expected}
        tolerance = reader["_AV_START_TOLERANCE_S"]
        for shift in (-3600, -.5, 0, .5, tolerance):
            with self.subTest(shift=shift), patch.dict(reader, {"_av_proc": lambda pid: (True, expected + shift)}):
                self.assertFalse(H._host_dead(host, reader))
                H._host_check(host, reader)
        with patch.dict(reader, {"_av_proc": lambda pid: (True, expected + tolerance + .1)}):
            self.assertTrue(H._host_dead(host, reader))
        with patch.dict(reader, {"_av_proc": lambda pid: (False, None)}):
            self.assertTrue(H._host_dead(host, reader))


if __name__ == "__main__":
    unittest.main()
