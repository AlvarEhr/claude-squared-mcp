"""Offline tests for v0.19 Codex sandbox ergonomics (Windows first): the
per-pair scratch folder as TEMP, permission profiles with credential
read-denial, the capability check and the post-turn profile check, network,
developer instructions (every exec + compaction), the sandbox note, the
create-time Python check, "blocked by policy" refusals and
``pair_update(system_prompt_append)``.

Nothing here spawns ``codex``: every spawn is mocked and argv is captured.
The Windows behaviour is exercised on any OS by patching
``_windows_sandbox`` (POSIX argv is asserted unchanged separately)."""

from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "src"))
# Isolation BEFORE importing the package: importing the server runs an
# orphan sweep over the pairs dir, and the user may have live pairs.
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-sandbox-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from claude_squared import activity as A  # noqa: E402
from claude_squared import codex_models as CM  # noqa: E402
from claude_squared import connectors as N  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared.adapters import claude as CL  # noqa: E402
from claude_squared.adapters import codex as CX  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402
from claude_squared.models import CreateResult, PairSpec, SendResult  # noqa: E402

# The two stderr lines Codex 0.164.0-alpha.11.1 printed for refusals at
# approval policy never (2026-10-25 probe, copied verbatim).
BLOCKED_LINE = (
    '2026-10-25T13:30:38.510329Z ERROR codex_core::tools::router: error=exec_command failed: '
    'CreateProcess { message: "Rejected(\\"`\\\\\\"C:\\\\\\\\Windows\\\\\\\\System32\\\\\\\\WindowsPowerShell'
    '\\\\\\\\v1.0\\\\\\\\powershell.exe\\\\\\" -Command \'Remove-Item -Force victim.txt\'` rejected: '
    'blocked by policy\\")" }')
NEVER_LINE = (
    "2026-10-25T13:30:55.826159Z ERROR codex_core::tools::router: error=approval policy is Never; "
    "reject command — you cannot ask for escalated permissions if the approval policy is Never")


def tool(value):
    return getattr(value, "fn", value)


def contains_run(argv, run):
    n = len(run)
    return any(argv[i:i + n] == run for i in range(len(argv) - n + 1))


def config_values(argv):
    """Every ``-c key=value`` in argv, parsed as TOML the way codex does."""
    out = {}
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            key, _, raw = argv[i + 1].partition("=")
            out[key] = tomllib.loads(f"v = {raw}")["v"]
    return out


class SandboxCase(unittest.TestCase):
    """Per-test homes; ``codex`` never runs unless a test fakes it."""

    windows = True
    capable = True

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.codex_home = self.directory / "codex"
        self.codex_home.mkdir()
        self.secret = self.directory / "fake secrets" / "auth.json"
        self.secret.parent.mkdir()
        self.secret.write_text("{}", encoding="utf-8")
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.directory / "claude"), "CODEX_HOME": str(self.codex_home),
            "CLAUDE_PAIR_CODEX_DENY_READ": str(self.secret)}))
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
        self.exe = self.directory / "bin" / "codex.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"fake")
        self.stack.enter_context(patch.object(CX, "codex_executable", return_value=str(self.exe)))
        self.stack.enter_context(patch.object(CX, "config_readd_args", return_value=[]))
        self.stack.enter_context(patch.object(CX, "_load_user_config", return_value={}))
        self.stack.enter_context(patch.object(N, "inventory", return_value=[]))
        self.stack.enter_context(patch.object(CX, "_windows_sandbox", return_value=self.windows))
        self.stack.enter_context(patch.dict(CX._PROFILE_CACHE, clear=True))
        self.stack.enter_context(patch.dict(CX._PY_CACHE, clear=True))
        if self.capable is not None:  # None: the real profile_support
            self.capability = self.stack.enter_context(
                patch.object(CX, "profile_support", return_value=self.capable))
        runtime = SimpleNamespace(get_or_none=Mock(return_value=None), evict=Mock(return_value=[]))
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=runtime))

    def retire_activity(self):
        home = str(self.directory / "claude" / "pairs")
        with A._WRITERS_LOCK:
            writers = [A._WRITERS.pop(key) for key in list(A._WRITERS) if key[0] == home]
        for writer in writers:
            with writer._io_lock, writer._lock:
                writer._forgotten = True

    def spec(self, name="p", **changes):
        data = dict(name=name, session_id=f"{name}-tid", backend="codex", model="sol",
                    permission_mode="workspace", cwd=str(self.directory))
        data.update(changes)
        return PairSpec(**data)

    def scratch(self, name="p"):
        return R.pairs_dir() / "scratch" / name

    def args(self, spec, **kw):
        kw.setdefault("model", None)
        kw.setdefault("effort", None)
        kw.setdefault("permission_mode", None)
        return CX.CodexAdapter()._exec_args(spec, **kw)


# ---------------------------------------------------------------------------
# argv per level x platform x overrides
# ---------------------------------------------------------------------------

class ProfileArgvTests(SandboxCase):

    def test_read_only_runs_on_a_profile_without_dash_s(self):
        # -s read-only beats default_permissions (verified), so it must go.
        for level in ("read-only", "plan"):
            with self.subTest(level=level):
                argv = self.args(self.spec(permission_mode=level))
                self.assertNotIn("-s", argv)
                self.assertNotIn("--approve-for-me", argv)
                cfg = config_values(argv)
                self.assertEqual(cfg["default_permissions"], CX.PROFILE_NAME)
                self.assertEqual(cfg[f"permissions.{CX.PROFILE_NAME}.extends"], ":read-only")
                fs = cfg[f"permissions.{CX.PROFILE_NAME}.filesystem"]
                self.assertEqual(fs, {str(self.scratch()): "write", str(self.secret): "deny"})
                for var in ("TEMP", "TMP", "TMPDIR"):
                    self.assertEqual(cfg[f"shell_environment_policy.set.{var}"], str(self.scratch()))
                self.assertTrue(self.scratch().is_dir(), "scratch is created at spawn")

    def test_workspace_and_auto_extend_the_workspace_profile(self):
        ws = self.args(self.spec(permission_mode="workspace"))
        auto = self.args(self.spec(permission_mode="auto"))
        for argv in (ws, auto):
            self.assertNotIn("-s", argv)
            self.assertEqual(config_values(argv)[f"permissions.{CX.PROFILE_NAME}.extends"], ":workspace")
        self.assertNotIn("--approve-for-me", ws)
        # --approve-for-me coexists with a profile (verified: deny + guardian).
        self.assertIn("--approve-for-me", auto)

    def test_unrestricted_gets_nothing(self):
        argv = self.args(self.spec(permission_mode="unrestricted"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertFalse(any("default_permissions" in a or "TEMP" in a for a in argv))
        self.assertFalse(self.scratch().exists())

    def test_native_overrides_follow_the_effective_sandbox(self):
        cases = (
            ("workspace", {"sandbox": "read-only"}, ":read-only"),
            ("read-only", {"sandbox": "workspace-write"}, ":workspace"),
            ("auto", {"approval": "none"}, ":workspace"),
        )
        for level, bo, extends in cases:
            with self.subTest(level=level, bo=bo):
                argv = self.args(self.spec(permission_mode=level, backend_options=bo))
                self.assertEqual(config_values(argv)[f"permissions.{CX.PROFILE_NAME}.extends"], extends)
                self.assertNotIn("-s", argv)
        full = self.args(self.spec(permission_mode="workspace", backend_options={"sandbox": "danger-full-access"}))
        self.assertTrue(contains_run(full, ["-s", "danger-full-access"]))
        self.assertFalse(any("TEMP" in a for a in full))
        bypass = self.args(self.spec(permission_mode="read-only", backend_options={"approval": "bypass"}))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", bypass)

    def test_a_per_send_override_changes_the_profile(self):
        argv = self.args(self.spec(permission_mode="auto"), permission_mode="read-only")
        self.assertEqual(config_values(argv)[f"permissions.{CX.PROFILE_NAME}.extends"], ":read-only")
        self.assertNotIn("--approve-for-me", argv)

    def test_managed_keys_come_after_backend_options_config(self):
        # codex -c is last-wins (verified), so ours must be later.
        argv = self.args(self.spec(backend_options={"config": {"model_verbosity": "low"}}))
        user = argv.index("model_verbosity=\"low\"")
        ours = next(i for i, a in enumerate(argv) if a.startswith("default_permissions="))
        self.assertLess(user, ours)

    def test_toml_quoting_survives_spaces_and_backslash_u(self):
        # R3: a path with a space and a \U sequence (an invalid TOML escape
        # unless quoted) must round-trip through every new -c value.
        home = self.directory / "Users dir" / "Ux"
        with patch.dict(os.environ, {"CLAUDE_HOME": str(home)}):
            argv = self.args(self.spec())
            scratch = str(home / "pairs" / "scratch" / "p")
            cfg = config_values(argv)
        self.assertEqual(cfg["shell_environment_policy.set.TEMP"], scratch)
        self.assertIn(scratch, cfg[f"permissions.{CX.PROFILE_NAME}.filesystem"])
        if os.name == "nt":
            self.assertIn("\\U", scratch)

    def test_astral_characters_stay_valid_toml(self):
        # json.dumps' default \\ud83d\\ude00 surrogates are invalid TOML.
        text = "Ship it 🚀 — naïve café"
        self.assertEqual(tomllib.loads(f"v = {CX._toml_literal(text)}")["v"], text)

    def test_secret_list_keeps_existing_files_only(self):
        missing = self.directory / "nope.json"
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_DENY_READ": os.pathsep.join([str(self.secret), str(missing)])}):
            self.assertEqual(CX.secret_read_denials(), [str(self.secret)])
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_DENY_READ": ""}):
            self.assertEqual(CX.secret_read_denials(), [])
        env = dict(os.environ)
        env.pop("CLAUDE_PAIR_CODEX_DENY_READ")
        (R.claude_home()).mkdir(parents=True, exist_ok=True)
        (R.claude_home() / ".credentials.json").write_text("{}", encoding="utf-8")
        (R.pairs_dir() / "api_key").write_text("x", encoding="utf-8")
        (self.codex_home / "auth.json").write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, env, clear=True), patch.object(CX.Path, "home", return_value=self.directory):
            self.assertEqual(CX.secret_read_denials(), [
                str(R.claude_home() / ".credentials.json"), str(R.pairs_dir() / "api_key"),
                str(self.codex_home / "auth.json")])


class LegacyArgvTests(SandboxCase):
    capable = False

    def test_read_only_falls_back_to_plain_dash_s(self):
        # Fail closed: no profile support -> plain read-only, no scratch.
        argv = self.args(self.spec(permission_mode="read-only"))
        self.assertTrue(contains_run(argv, ["-s", "read-only"]))
        self.assertFalse(any("default_permissions" in a or "TEMP" in a for a in argv))

    def test_workspace_gets_scratch_via_add_dir(self):
        argv = self.args(self.spec(permission_mode="workspace"))
        self.assertTrue(contains_run(argv, ["-s", "workspace-write"]))
        self.assertTrue(contains_run(argv, ["--add-dir", str(self.scratch())]))
        cfg = config_values(argv)
        self.assertIs(cfg["sandbox_workspace_write.exclude_tmpdir_env_var"], True)
        self.assertEqual(cfg["shell_environment_policy.set.TMP"], str(self.scratch()))
        auto = self.args(self.spec(permission_mode="auto"))
        self.assertIn("--approve-for-me", auto)
        self.assertNotIn("-s", auto)
        self.assertTrue(contains_run(auto, ["--add-dir", str(self.scratch())]))


class PosixArgvTests(SandboxCase):
    windows = False

    def test_posix_argv_is_unchanged(self):
        for level, expected in (("read-only", ["-s", "read-only"]), ("workspace", ["-s", "workspace-write"]),
                                ("auto", ["--approve-for-me"]),
                                ("unrestricted", ["--dangerously-bypass-approvals-and-sandbox"])):
            with self.subTest(level=level):
                self.assertEqual(CX.CodexAdapter.permission_args(level, {}, pair_name="p"), expected)
                self.assertEqual(CX.CodexAdapter.permission_args(level), expected)
        self.assertFalse(self.scratch().exists())
        self.capability.assert_not_called()
        self.assertIsNone(CX.instructions_text(self.spec()))

    def test_posix_network_is_the_legacy_key_only(self):
        argv = CX.CodexAdapter.permission_args("workspace", {"network": True}, pair_name="p")
        self.assertEqual(argv, ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"])


# ---------------------------------------------------------------------------
# capability check + post-turn profile check
# ---------------------------------------------------------------------------

class CapabilityTests(SandboxCase):
    capable = None

    def completed(self, stderr, code=1):
        return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)

    def test_only_no_prompt_counts_as_support_and_is_cached(self):
        with patch.object(CX, "run_codex", return_value=self.completed("No prompt provided via stdin.")) as run:
            self.assertTrue(CX.profile_support(str(self.exe)))
            self.assertTrue(CX.profile_support(str(self.exe)))
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertIn("--strict-config", argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_unknown_keys_or_errors_fail_closed(self):
        for stderr in ("Error loading config.toml: unknown configuration field `default_permissions`",
                       "error: unexpected argument '--strict-config' found"):
            with self.subTest(stderr=stderr):
                CX._PROFILE_CACHE.clear()
                with patch.object(CX, "run_codex", return_value=self.completed(stderr)):
                    self.assertFalse(CX.profile_support(str(self.exe)))

    def test_a_spawn_failure_is_not_cached(self):
        with patch.object(CX, "run_codex", side_effect=OSError("gone")):
            self.assertFalse(CX.profile_support(str(self.exe)))
        with patch.object(CX, "run_codex", return_value=self.completed("No prompt provided via stdin.")):
            self.assertTrue(CX.profile_support(str(self.exe)))

    def test_a_marked_binary_is_never_used_again(self):
        key = CX._exe_key(str(self.exe))
        CX.mark_profile_unsupported(key, "read-only profile not confined")
        CX._PROFILE_CACHE.clear()  # another process: only the file remains
        with patch.object(CX, "run_codex", return_value=self.completed("No prompt provided via stdin.")) as run:
            self.assertFalse(CX.profile_support(str(self.exe)))
        run.assert_not_called()
        self.assertIn(key, json.loads(CX._unsupported_path().read_text(encoding="utf-8")))

    def test_a_mark_from_another_process_beats_a_cached_yes(self):
        with patch.object(CX, "run_codex", return_value=self.completed("No prompt provided via stdin.")):
            self.assertTrue(CX.profile_support(str(self.exe)))
        marks = {CX._exe_key(str(self.exe)): "marked elsewhere"}
        CX._unsupported_path().write_text(json.dumps(marks), encoding="utf-8")
        self.assertFalse(CX.profile_support(str(self.exe)))


# The read-only profile as Codex recorded it in the 2026-10-25 probe.
def _turn_context(scratch, *, extra_writes=(), deny=(), network="restricted"):
    entries = [{"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
               {"path": {"type": "path", "path": str(scratch)}, "access": "write"}]
    entries += [{"path": {"type": "path", "path": str(p)}, "access": "write"} for p in extra_writes]
    entries += [{"path": {"type": "path", "path": str(p)}, "access": "deny"} for p in deny]
    return {"sandbox_policy": {"type": "workspace-write"},
            "permission_profile": {"type": "managed", "file_system": {"type": "restricted", "entries": entries},
                                   "network": network}}


class BeltTests(SandboxCase):

    def plan(self, level="read-only"):
        return CX.sandbox_plan(level, {}, pair_name="p")

    def test_confined_read_only_passes(self):
        plan = self.plan()
        self.assertEqual(CX.profile_breach(_turn_context(plan.scratch, deny=plan.deny), plan), (None, None))

    def test_extra_write_or_network_is_a_breach(self):
        plan = self.plan()
        breach, _ = CX.profile_breach(_turn_context(plan.scratch, extra_writes=[self.directory], deny=plan.deny), plan)
        self.assertIn("not confined", breach)
        breach, _ = CX.profile_breach(_turn_context(plan.scratch, deny=plan.deny, network="enabled"), plan)
        self.assertIn("network", breach)
        breach, _ = CX.profile_breach({"sandbox_policy": {"type": "read-only"}}, plan)
        self.assertIn("no permission profile", breach)

    def test_missing_denials_only_warn(self):
        plan = self.plan("workspace")
        breach, warning = CX.profile_breach(_turn_context(plan.scratch, extra_writes=[self.directory]), plan)
        self.assertIsNone(breach)
        self.assertIn("read-denial", warning)

    def test_send_marks_the_binary_after_a_breach(self):
        spec = self.spec(permission_mode="read-only")
        rollout = self.directory / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context", "payload": _turn_context(
            self.directory, extra_writes=[self.directory / "x"])}) + "\n", encoding="utf-8")  # an OLD turn

        def fake_run(args, run_spec, **kw):
            plan = CX.sandbox_plan("read-only", {}, pair_name="p")
            with open(rollout, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "turn_context", "payload": _turn_context(
                    plan.scratch, extra_writes=[self.directory], deny=plan.deny)}) + "\n")
            return [{"type": "turn.completed", "usage": {}}], "", 0, 5

        adapter = CX.CodexAdapter()
        with patch.object(CX.CodexAdapter, "session_exists", return_value=True), \
                patch.object(CX.CodexAdapter, "transcript_path", return_value=rollout), \
                patch.object(CX.CodexAdapter, "_run", side_effect=fake_run):
            result = adapter.send(spec, "hi")
        self.assertTrue(any("SANDBOX" in n and "not confined" in n for n in result.notes), result.notes)
        self.assertIn(CX._exe_key(str(self.exe)), CX._unsupported_marks())

    def test_send_ignores_turn_contexts_from_before_the_exec(self):
        spec = self.spec(permission_mode="read-only")
        rollout = self.directory / "rollout.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context", "payload": _turn_context(
            self.directory, extra_writes=[self.directory / "x"])}) + "\n", encoding="utf-8")
        with patch.object(CX.CodexAdapter, "session_exists", return_value=True), \
                patch.object(CX.CodexAdapter, "transcript_path", return_value=rollout), \
                patch.object(CX.CodexAdapter, "_run", return_value=([{"type": "turn.failed", "error": {}}], "", 1, 5)):
            result = CX.CodexAdapter().send(spec, "hi")
        self.assertFalse(any("SANDBOX" in n for n in result.notes))
        self.assertEqual(CX._unsupported_marks(), {})


# ---------------------------------------------------------------------------
# network + backend_options validation
# ---------------------------------------------------------------------------

class NetworkAndOptionTests(SandboxCase):

    def test_network_profile_and_git_https(self):
        argv = self.args(self.spec(backend_options={"network": True}))
        cfg = config_values(argv)
        self.assertIs(cfg[f"permissions.{CX.PROFILE_NAME}.network.enabled"], True)
        self.assertEqual(cfg["shell_environment_policy.set.GIT_CONFIG_KEY_0"], "http.sslBackend")
        self.assertEqual(cfg["shell_environment_policy.set.GIT_CONFIG_VALUE_0"], "openssl")
        self.assertEqual(cfg["shell_environment_policy.set.GIT_CONFIG_COUNT"], "1")

    def test_network_legacy_key_without_profiles(self):
        with patch.object(CX, "profile_support", return_value=False):
            argv = self.args(self.spec(backend_options={"network": True}))
        self.assertIs(config_values(argv)["sandbox_workspace_write.network_access"], True)

    def test_existing_git_config_is_not_clobbered(self):
        for cfg, env in (({"shell_environment_policy": {"set": {"GIT_CONFIG_COUNT": "2"}}}, {}),
                         ({}, {"GIT_CONFIG_COUNT": "1"})):
            with self.subTest(cfg=cfg, env=env):
                with patch.object(CX, "_load_user_config", return_value=cfg), patch.dict(os.environ, env):
                    plan = CX.sandbox_plan("workspace", {"network": True}, pair_name="p")
                self.assertFalse(plan.git_env)
                self.assertFalse(any("GIT_CONFIG" in a for a in plan.args))
                self.assertTrue(any("GIT_CONFIG_* variables are already in use" in n for n in plan.notes))
        # inherit=core: the inherited variable never reaches the sandbox.
        with patch.object(CX, "_load_user_config", return_value={"shell_environment_policy": {"inherit": "core"}}), \
                patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1"}):
            self.assertTrue(CX.sandbox_plan("workspace", {"network": True}, pair_name="p").git_env)

    def test_network_is_refused_at_read_only_and_dropped_at_spawn(self):
        with self.assertRaisesRegex(ValueError, "network"):
            CX.CodexAdapter.validate_backend_options("read-only", {"network": True})
        with self.assertRaisesRegex(ValueError, "network"):
            CX.CodexAdapter.validate_backend_options("workspace", {"network": True, "sandbox": "read-only"})
        CX.CodexAdapter.validate_backend_options("auto", {"network": True})
        argv = self.args(self.spec(backend_options={"network": True}), permission_mode="read-only")
        self.assertFalse(any("network" in a for a in argv))

    def test_option_types(self):
        for bo in ({"network": "yes"}, {"sandbox_note": 0}):
            with self.subTest(bo=bo), self.assertRaisesRegex(ValueError, "true or false"):
                CX.CodexAdapter.validate_backend_options("workspace", bo)

    def test_managed_config_keys_are_refused(self):
        for key in ("default_permissions", "permissions.x.extends", "sandbox_workspace_write.network_access",
                    "developer_instructions", "shell_environment_policy.set.TEMP",
                    "shell_environment_policy.set.tmp", "shell_environment_policy.set",
                    "shell_environment_policy"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "manages"):
                CX.CodexAdapter.validate_backend_options("workspace", {"config": {key: "x"}})
        for key in ("shell_environment_policy.inherit", "shell_environment_policy.set.FOO", "model_verbosity"):
            with self.subTest(key=key):
                CX.CodexAdapter.validate_backend_options("workspace", {"config": {key: "x"}})

    def test_claude_refuses_network(self):
        with self.assertRaisesRegex(ValueError, "Codex-only"):
            CL.ClaudeAdapter.validate_backend_options({"network": True})


# ---------------------------------------------------------------------------
# developer instructions + the sandbox note
# ---------------------------------------------------------------------------

EVENTS = [{"type": "thread.started", "thread_id": "new-tid"},
          {"type": "item.completed", "item": {"type": "agent_message", "text": "pair-ready"}},
          {"type": "turn.completed", "usage": {}}]


class InstructionTests(SandboxCase):

    def capture_run(self, events=EVENTS):
        calls = []

        def fake_run(args, spec, **kw):
            calls.append((list(args), kw.get("stdin_text")))
            return list(events), "", 0, 5
        return calls, patch.object(CX.CodexAdapter, "_run", side_effect=fake_run)

    def test_sandbox_note_follows_platform_level_and_switch(self):
        self.assertIn(CX.SANDBOX_NOTE, CX.instructions_text(self.spec()))
        self.assertIsNone(CX.instructions_text(self.spec(permission_mode="unrestricted")))
        self.assertIsNone(CX.instructions_text(self.spec(backend_options={"sandbox_note": False})))
        text = CX.instructions_text(self.spec(system_prompt_append="Be terse."))
        self.assertTrue(text.startswith("Be terse."))
        self.assertLessEqual(len(CX.SANDBOX_NOTE.splitlines()), 10)
        for word in ("scratch\\", "workspace", "auto"):
            self.assertNotIn(word, CX.SANDBOX_NOTE)  # state-neutral (R10)

    def test_create_passes_developer_instructions(self):
        calls, run = self.capture_run()
        with run:
            result = CX.CodexAdapter().create(self.spec(system_prompt_append="Be terse."))
        argv, stdin = calls[0]
        self.assertTrue(config_values(argv)["developer_instructions"].startswith("Be terse."))
        self.assertEqual(stdin, CX.PROBE_PROMPT)  # no first-message wrapper
        self.assertEqual(result.instructions["mode"], "developer")
        self.assertEqual(result.instructions, CX.instructions_record("developer", CX.instructions_text(
            self.spec(system_prompt_append="Be terse."))))

    def test_over_budget_falls_back_to_the_wrapper(self):
        calls, run = self.capture_run()
        long = "x" * (CX.ARGV_BUDGET + 10)
        with run:
            result = CX.CodexAdapter().create(self.spec(system_prompt_append=long))
        argv, stdin = calls[0]
        self.assertFalse(any(a.startswith("developer_instructions=") for a in argv))
        self.assertIn("<operating instructions", stdin)
        self.assertIn(long, stdin)  # never truncated
        self.assertEqual(result.instructions["mode"], "wrapper")
        self.assertTrue(any("too long for the Codex command line" in n for n in result.notes))

    def send_args(self, spec):
        calls, run = self.capture_run([{"type": "turn.completed", "usage": {}}])
        with run, patch.object(CX.CodexAdapter, "session_exists", return_value=True), \
                patch.object(CX.CodexAdapter, "transcript_path", return_value=None):
            result = CX.CodexAdapter().send(spec, "hello")
        return calls[0][0], result

    def test_send_resends_only_in_developer_mode(self):
        rec = CX.instructions_record("developer", "old text")
        argv, _ = self.send_args(self.spec(system_prompt_append="New.", codex_instructions=rec))
        self.assertTrue(config_values(argv)["developer_instructions"].startswith("New."))
        self.assertLess(next(i for i, a in enumerate(argv) if a.startswith("developer_instructions=")),
                        argv.index("resume"))
        for record in (None, CX.instructions_record("wrapper", "x")):
            with self.subTest(record=record):
                argv, _ = self.send_args(self.spec(system_prompt_append="New.", codex_instructions=record))
                self.assertFalse(any(a.startswith("developer_instructions=") for a in argv))

    def test_send_over_budget_skips_with_a_note(self):
        rec = CX.instructions_record("developer", "x")
        argv, result = self.send_args(self.spec(system_prompt_append="y" * CX.ARGV_BUDGET, codex_instructions=rec))
        self.assertFalse(any(a.startswith("developer_instructions=") for a in argv))
        self.assertTrue(any("not re-sent" in n for n in result.notes))

    def compact_params(self, spec):
        requests = []

        class FakeClient:
            def __init__(self, *a, **kw):
                self.proc = SimpleNamespace(poll=lambda: None)
                self.deadline = float("inf")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def initialize(self):
                pass

            def request(self, method, params):
                requests.append((method, params))
                return {}

            def check_stop(self):
                pass

            def messages(self):
                return [{"method": "item/completed", "params": {"item": {"type": "contextCompaction"}}},
                        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}]

        with patch.object(CX, "AppServerClient", FakeClient):
            CX.CodexAdapter()._appserver_compact(spec, timeout_seconds=30)
        return dict(requests)["thread/resume"]

    def test_compaction_passes_developer_instructions(self):
        rec = CX.instructions_record("developer", "x")
        params = self.compact_params(self.spec(system_prompt_append="Keep me.", codex_instructions=rec,
                                               backend_options={"config": {"developer_instructions": "stale"}}))
        self.assertTrue(params["developerInstructions"].startswith("Keep me."))
        self.assertNotIn("developer_instructions", params["config"])
        params = self.compact_params(self.spec(system_prompt_append="Keep me."))
        self.assertNotIn("developerInstructions", params)

    def test_changed_text_is_detected(self):
        spec = self.spec(system_prompt_append="A")
        spec = spec.model_copy(update={"codex_instructions": CX.instructions_record("developer", CX.instructions_text(spec))})
        self.assertFalse(CX.instructions_changed(spec))
        self.assertTrue(CX.instructions_changed(spec.model_copy(update={"system_prompt_append": "B"})))


# ---------------------------------------------------------------------------
# refusals before a command runs ("blocked by policy" / approval never)
# ---------------------------------------------------------------------------

class PolicyRefusalTests(SandboxCase):

    def test_both_stderr_lines_become_denials(self):
        for line in (BLOCKED_LINE, NEVER_LINE):
            self.assertTrue(CX._DENIAL_RE.search(line), line)
        result = CX.CodexAdapter()._build_send_result(
            self.spec(), [{"type": "turn.completed", "usage": {}}], f"{BLOCKED_LINE}\n{NEVER_LINE}\n", 0, 5, 0.0,
            model_used="gpt-5.6-sol", permission_level="workspace")
        names = [d.tool_name for d in result.permission_denials]
        self.assertEqual(names, ["policy-blocked", "escalation-refused"])
        self.assertEqual(result.permission_denials[0].tool_input["command"], "Remove-Item -Force victim.txt")
        self.assertIsNone(result.safety_signal)  # the turn itself did not fail

    def test_handoff_text_says_it_never_ran(self):
        R.add_pair(self.spec())
        result = CX.CodexAdapter()._build_send_result(
            self.spec(), [{"type": "turn.completed", "usage": {}}], BLOCKED_LINE, 0, 5, 0.0,
            model_used="gpt-5.6-sol", permission_level="workspace")
        text = S._fmt_send_result(result)
        self.assertIn("never ran", text)
        self.assertIn("Remove-Item -Force victim.txt", text)
        self.assertIn("permission_mode='auto'", text)
        self.assertNotIn("Do NOT retry autonomously", text)


# ---------------------------------------------------------------------------
# the create-time Python check
# ---------------------------------------------------------------------------

class PythonCheckTests(SandboxCase):

    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(CX.shutil, "which", return_value=str(self.directory / "py" / "python.exe")))

    def test_failure_names_the_fix_and_is_cached(self):
        denied = subprocess.CompletedProcess([], 1, stdout="", stderr=(
            "windows sandbox failed: runner failed during SpawnChild: CreateProcessAsUserW failed: 5 "
            "(Access is denied.) | cwd=x"))
        with patch.object(CX, "run_codex", return_value=denied) as run:
            first = CX.python_sandbox_note("p")
            second = CX.python_sandbox_note("q")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first, second)
        self.assertIn("icacls", first)
        self.assertIn("CodexSandboxUsers:(OI)(CI)RX", first)
        self.assertIn("Only `python` on PATH was checked", first)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:6], ["sandbox", "-P", ":read-only", "-C", str(self.scratch("p"))])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.scratch("p")))  # never the repo

    def test_success_unsupported_and_spawn_failure_say_nothing(self):
        ok = subprocess.CompletedProcess([], 0, stdout="PYOK\n", stderr="")
        usage = subprocess.CompletedProcess([], 2, stdout="", stderr="error: unexpected argument '-P'\n\nUsage: codex")
        for outcome in (ok, usage):
            CX._PY_CACHE.clear()
            with patch.object(CX, "run_codex", return_value=outcome):
                self.assertIsNone(CX.python_sandbox_note("p"))
        CX._PY_CACHE.clear()
        with patch.object(CX, "run_codex", side_effect=subprocess.TimeoutExpired("codex", 30)):
            self.assertIsNone(CX.python_sandbox_note("p"))
        self.assertEqual(CX._PY_CACHE, {})


class PosixPythonCheckTests(SandboxCase):
    windows = False

    def test_no_check_off_windows(self):
        self.assertIsNone(CX.python_sandbox_note("p"))


# ---------------------------------------------------------------------------
# MCP tools: create / clear / update / info / forget
# ---------------------------------------------------------------------------

class ServerTests(SandboxCase):

    def fake_create(self, mode="developer"):
        def create(spec, initial_message=None):
            return CreateResult(name=spec.name, session_id=f"{spec.name}-new",
                                instructions=CX.instructions_record(mode, CX.instructions_text(spec)))
        return patch.object(CX.CodexAdapter, "create", side_effect=create)

    def test_create_records_instructions_and_reports_the_sandbox(self):
        with self.fake_create(), patch.object(CX, "python_sandbox_note", return_value="PYTHON-NOTE"):
            report = tool(S.pair_create)("cx", backend="codex", model="sol", permission_mode="workspace",
                                         cwd=str(self.directory), backend_options={"network": True})
        stored = R.get_pair("cx")
        self.assertEqual(stored.codex_instructions["mode"], "developer")
        self.assertIn("permission profile", report)
        self.assertIn("network on", report)
        self.assertIn("PYTHON-NOTE", report)

    def test_clear_moves_a_wrapper_thread_to_developer_mode(self):
        R.add_pair(self.spec("cx", system_prompt_append="A"))
        with self.fake_create():
            tool(S.pair_clear)("cx", archive_old=False)
        self.assertEqual(R.get_pair("cx").codex_instructions["mode"], "developer")

    def test_update_system_prompt_append_both_backends(self):
        R.add_pair(self.spec("cx", codex_instructions=CX.instructions_record("developer", "x")))
        report = tool(S.pair_update)("cx", system_prompt_append="Review only.")
        self.assertEqual(R.get_pair("cx").system_prompt_append, "Review only.")
        self.assertIn("next compaction", report)
        R.add_pair(self.spec("old"))
        self.assertIn("pair_clear('old') starts a thread", tool(S.pair_update)("old", system_prompt_append="X"))
        R.add_pair(PairSpec(name="cl", session_id="cl-sid", backend="claude", model="opus", cwd=str(self.directory)))
        report = tool(S.pair_update)("cl", system_prompt_append="Review only.")
        self.assertIn("pinned to existing session", report)
        tool(S.pair_update)("cl", system_prompt_append="")
        self.assertIsNone(R.get_pair("cl").system_prompt_append)

    def test_update_refuses_network_at_read_only(self):
        R.add_pair(self.spec("cx", backend_options={"network": True}))
        with self.assertRaisesRegex(PairError, "network"):
            tool(S.pair_update)("cx", permission_mode="read-only")

    def test_info_shows_sandbox_and_changed_instructions(self):
        spec = self.spec("cx", system_prompt_append="A")
        R.add_pair(spec.model_copy(update={
            "codex_instructions": CX.instructions_record("developer", CX.instructions_text(spec))}))
        info = tool(S.pair_info)("cx")
        self.assertIn("permission profile", info)
        self.assertIn("developer instructions", info)
        self.assertNotIn("changed since", info)
        tool(S.pair_update)("cx", system_prompt_append="B")
        self.assertIn("changed since", tool(S.pair_info)("cx"))

    def test_forget_removes_scratch_and_reports_leftovers(self):
        R.add_pair(self.spec("cx"))
        self.args(self.spec("cx"))
        self.assertTrue(self.scratch("cx").is_dir())
        out = tool(S.pair_forget)("cx", archive=False)
        self.assertFalse(self.scratch("cx").exists())
        self.assertNotIn("scratch", out)
        R.add_pair(self.spec("cy"))
        self.args(self.spec("cy"))
        with patch.object(CX.shutil, "rmtree"):
            out = tool(S.pair_forget)("cy", archive=False)
        self.assertIn("could not be fully removed", out)
        self.assertNotIn("cy", R.load().pairs)  # the forget itself succeeded


if __name__ == "__main__":
    unittest.main()
