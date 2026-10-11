"""Offline tests for v0.18.0: per-pair API-key billing (``billing`` /
``pair_billing``), the one Claude env builder, the key file's Read deny rule,
per-turn cost, credit errors, Haiku 5.x effort, and the shutdown fix A
(Incident 2026-10-04).

Nothing here spawns ``claude`` or ``codex``, and nothing reads the real key
file: CLAUDE_HOME points at a temp dir before the package is imported, and
every test writes a fake key there when it needs one."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Isolation BEFORE importing the package: the import runs an orphan sweep over
# the pairs dir, and the real key file must never be in reach.
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-billing-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import activity as A  # noqa: E402
from claude_squared import async_tasks as AT  # noqa: E402
from claude_squared import connectors as N  # noqa: E402
from claude_squared import models as M  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as RT  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared import triage as TR  # noqa: E402
from claude_squared.adapters import claude as CL  # noqa: E402
from claude_squared.errors import (  # noqa: E402
    NOT_DELIVERED_MESSAGE, BillingError, CLIError, PairError, ServerShuttingDown)
from claude_squared.models import AsyncTaskState, PairSpec, SendResult  # noqa: E402
from claude_squared.runtime import PairRuntime, RuntimeRegistry  # noqa: E402

FAKE_KEY = "sk-ant-api03-" + "f" * 40 + "WXYZ"
OTHER_KEY = "sk-ant-api03-" + "e" * 40 + "QRST"
# Variables the env builder reads; cleared in every test so the developer's
# own environment can't leak in.
BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
               "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
               "CLAUDE_CODE_PROMPT_CACHE_TTL")


def tool(value):
    return getattr(value, "fn", value)


class _AliveProc:
    """A stand-in claude process: alive, never really killed."""
    pid = 0
    returncode = None
    stdin = None

    def poll(self):
        return None


class BillingCase(unittest.TestCase):
    """Per-test CLAUDE_HOME, a clean billing environment, every CLI spawn
    refused unless a test patches it in, and a fresh shutdown flag."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory / "claude")}))
        self.addCleanup(self.retire_activity)
        for var in BILLING_ENV:
            os.environ.pop(var, None)  # patch.dict restores the whole mapping
        # The real key file is never in reach.
        self.assertTrue(str(CL.api_key_path()).startswith(str(self.directory)))
        self.popen = Mock(side_effect=AssertionError("unexpected Popen"))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("unexpected run")))
        self.stack.enter_context(patch.object(subprocess, "Popen", self.popen))
        self.stack.enter_context(patch.object(CL, "_claude_executable", return_value="claude"))
        self.stack.enter_context(patch.object(RT, "_claude_executable", return_value="claude"))
        self.stack.enter_context(patch.object(CL, "_cli_permission_choices", return_value=("manual", "auto")))
        self.stack.enter_context(patch.object(N, "inventory", return_value=[]))
        self.stack.enter_context(patch.object(RT, "_SHUTTING_DOWN", threading.Event()))
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

    def write_key(self, text=FAKE_KEY, *, raw: bytes | None = None):
        path = CL.api_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def spec(self, name="p", **changes):
        data = dict(name=name, session_id=f"{name}-sid", model="opus", cwd=str(self.directory))
        data.update(changes)
        return PairSpec(**data)

    def add(self, name="p", **changes):
        spec = self.spec(name, **changes)
        R.add_pair(spec)
        return spec


# ---------------------------------------------------------------------------
# The key file
# ---------------------------------------------------------------------------

class KeyFileTests(BillingCase):

    def test_a_bom_and_surrounding_whitespace_are_ignored(self):
        self.write_key(raw=("﻿  " + FAKE_KEY + "\r\n\n").encode("utf-8"))
        self.assertEqual(CL.read_api_key(), FAKE_KEY)

    def test_a_missing_file_names_the_path_and_the_way_back(self):
        with self.assertRaises(BillingError) as caught:
            CL.read_api_key()
        self.assertIn(str(CL.api_key_path()), str(caught.exception))
        self.assertIn('billing="plan"', str(caught.exception))
        self.assertIn("key file missing", CL.api_key_status())

    def test_a_malformed_file_never_echoes_its_content(self):
        for raw in (b"not-a-key-SECRETPART", (FAKE_KEY + "\n" + OTHER_KEY).encode(),
                    b"sk-ant-api03-SECRET\xff\xfePART", b""):
            with self.subTest(raw=raw[:12]):
                self.write_key(raw=raw)
                with self.assertRaises(BillingError) as caught:
                    CL.read_api_key()
                text = str(caught.exception)
                self.assertNotIn("SECRET", text)
                self.assertNotIn(FAKE_KEY[-12:], text)
                self.assertIn("key file unusable", CL.api_key_status())

    def test_any_console_api_key_passes_and_non_api_kinds_are_refused(self):
        # A personal key (sk-ant-usr…) answered and was covered (manager,
        # 2026-10-11); service-account and legacy workspace keys work too.
        for key in ("sk-ant-usr01-" + "a" * 40, "sk-ant-api03-" + "b" * 40, "sk-ant-svc01-" + "c" * 40):
            with self.subTest(key=key[:12]):
                self.write_key(key)
                self.assertEqual(CL.read_api_key(), key)
        for key, kind in (("sk-ant-oat01-" + "d" * 40, "OAuth access token"),
                          ("sk-ant-ort01-" + "e" * 40, "OAuth refresh token"),
                          ("sk-ant-admin01-" + "f" * 40, "Admin API key")):
            with self.subTest(key=key[:12]):
                self.write_key(key)
                with self.assertRaisesRegex(BillingError, kind) as caught:
                    CL.read_api_key()
                self.assertNotIn(key[-12:], str(caught.exception))

    def test_status_shows_only_the_last_four_characters(self):
        self.write_key()
        self.assertEqual(CL.api_key_status(), "key …WXYZ")

    def test_the_fingerprint_follows_the_key(self):
        self.write_key()
        first = CL.current_key_fingerprint()
        self.write_key(OTHER_KEY)
        self.assertNotEqual(CL.current_key_fingerprint(), first)
        self.assertNotIn(FAKE_KEY, first)
        CL.api_key_path().unlink()
        self.assertIsNone(CL.current_key_fingerprint())


# ---------------------------------------------------------------------------
# The env builder
# ---------------------------------------------------------------------------

class EnvBuilderTests(BillingCase):

    def test_plan_strips_an_inherited_key_and_keeps_the_rest(self):
        # In -p a key beats the claude.ai login: an inherited one would bill
        # every pair. Plan is plan by construction.
        os.environ.update(ANTHROPIC_API_KEY="sk-ant-api03-inherited", CLAUDE_CODE_ENTRYPOINT="claude-desktop",
                          ANTHROPIC_BASE_URL="https://api.anthropic.com", MY_VAR="kept")
        env = CL.claude_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_ENTRYPOINT"], "claude-desktop")
        self.assertEqual(env["MY_VAR"], "kept")
        self.assertEqual(CL.claude_env("plan"), env)
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-api03-inherited")  # untouched

    def test_api_uses_the_key_without_what_would_ignore_or_outrank_it(self):
        self.write_key()
        os.environ.update(ANTHROPIC_API_KEY="sk-ant-api03-inherited", CLAUDE_CODE_ENTRYPOINT="claude-desktop",
                          ANTHROPIC_AUTH_TOKEN="token", ANTHROPIC_BASE_URL="https://api.anthropic.com/",
                          MY_VAR="kept")
        env = CL.claude_env("api")
        self.assertEqual(env["ANTHROPIC_API_KEY"], FAKE_KEY)
        for var in ("CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(var, env)
        # Review round 1: on 2.1.296 the scrub forces permission mode to
        # "default" (Write denied on an acceptEdits pair) — never set it.
        self.assertNotIn("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", env)
        self.assertEqual(env["CLAUDE_CODE_PROMPT_CACHE_TTL"], "1h")
        self.assertEqual(env["MY_VAR"], "kept")
        # The key never enters this process's own environment.
        self.assertNotIn(FAKE_KEY, os.environ.values())

    def test_api_refuses_an_environment_that_sends_the_key_elsewhere(self):
        self.write_key()
        for var, value in (("ANTHROPIC_BASE_URL", "https://gateway.example.com"),
                           ("CLAUDE_CODE_USE_BEDROCK", "1"), ("CLAUDE_CODE_USE_VERTEX", "true"),
                           ("CLAUDE_CODE_USE_FOUNDRY", "yes")):
            with self.subTest(var=var):
                with patch.dict(os.environ, {var: value}):
                    with self.assertRaisesRegex(BillingError, var):
                        CL.claude_env("api")
                    CL.claude_env("plan")  # plan pairs are not affected
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_BASE_URL": ""}):
            self.assertEqual(CL.claude_env("api")["ANTHROPIC_API_KEY"], FAKE_KEY)

    def test_api_without_a_key_refuses_never_falls_back(self):
        with self.assertRaises(BillingError):
            CL.claude_env("api")


# ---------------------------------------------------------------------------
# Spawn sites
# ---------------------------------------------------------------------------

class SpawnSiteTests(BillingCase):

    def start_runtime(self, spec):
        proc = SimpleNamespace(pid=0, returncode=None, stdin=io.BytesIO(), stdout=io.BytesIO(),
                               stderr=io.BytesIO(), poll=lambda: None)
        self.popen.side_effect = None
        self.popen.return_value = proc
        rt = PairRuntime(spec, CL.ClaudeAdapter())
        with patch.object(PairRuntime, "_stdout_reader", lambda self, gen=None: None), \
                patch.object(PairRuntime, "_stderr_reader", lambda self: None):
            rt.start()
        return rt, self.popen.call_args.kwargs["env"]

    def test_the_warm_runtime_spawns_with_the_pairs_env(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-inherited"
        rt, env = self.start_runtime(self.add())
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIsNone(rt.key_fingerprint)
        self.write_key()
        rt, env = self.start_runtime(self.add("api", billing="api"))
        self.assertEqual(env["ANTHROPIC_API_KEY"], FAKE_KEY)
        self.assertEqual(rt.key_fingerprint, CL.current_key_fingerprint())
        log = rt.main_log_path.read_text(encoding="utf-8")
        self.assertIn("billing=api", log)
        self.assertNotIn(FAKE_KEY, log)
        self.assertFalse(any(FAKE_KEY in a for a in self.popen.call_args.args[0]))  # never on argv

    def test_the_warm_runtime_refuses_to_start_without_a_key(self):
        rt = PairRuntime(self.add(billing="api"), CL.ClaudeAdapter())
        with self.assertRaises(BillingError):
            rt.start()
        self.popen.assert_not_called()

    def test_one_shot_paths_use_the_pairs_env(self):
        self.write_key()
        spec = self.add(billing="api")
        seen = []

        def fake_run(args, **kwargs):
            seen.append(kwargs.get("env"))
            out = {"type": "result", "subtype": "success", "session_id": "fork-sid", "result": "ok"}
            return SimpleNamespace(returncode=0, stdout=json.dumps(out).encode(), stderr=b"")
        adapter = CL.ClaudeAdapter()
        with patch.object(subprocess, "run", side_effect=fake_run), \
                patch.object(CL.ClaudeAdapter, "session_exists", return_value=True), \
                patch.object(CL.ClaudeAdapter, "_build_send_result", return_value=None), \
                patch.object(AT, "mark_task_executing", lambda task_id: None):
            adapter.send(spec, "hello", effort="low")
            adapter.fork(spec, "SENTINEL")
            adapter._run_stream_json(spec, ["/context"], timeout_seconds=5)
            adapter.create(self.spec("fresh"))
        self.assertEqual([env.get("ANTHROPIC_API_KEY") for env in seen], [FAKE_KEY] * 3 + [None])

    def test_triage_and_the_connector_listing_stay_on_the_plan(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-inherited"
        envs = []

        def fake_popen(argv, **kwargs):
            envs.append(kwargs["env"])
            raise OSError("captured")
        self.popen.side_effect = fake_popen
        self.assertIsNone(TR.classify_with_haiku("progress"))

        def fake_run(args, **kwargs):
            envs.append(kwargs["env"])
            return SimpleNamespace(stdout="", returncode=0)
        with patch.object(subprocess, "run", side_effect=fake_run), patch.dict(N._CACHE, clear=True):
            N._list_output("claude", str(self.directory))
        self.assertEqual(len(envs), 2)
        for env in envs:
            self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_the_key_file_is_denied_to_read_on_every_spawn(self):
        rule = CL.key_file_deny_rule()
        path = Path(os.path.abspath(CL.api_key_path()))
        if path.drive:  # Windows: //c/Users/... (probed 2026-10-11)
            self.assertEqual(rule, f"Read(//{path.drive[0].lower()}{path.as_posix()[2:]})")
        else:
            self.assertEqual(rule, f"Read(/{path.as_posix()})")
        for spec in (self.spec(), self.spec(billing="api"), self.spec(mcp_whitelist=["probe"])):
            args = CL.ClaudeAdapter()._mcp_args(spec)
            i = args.index(rule)
            self.assertEqual(args[i - 1], "--disallowed-tools")


class RotationTests(BillingCase):

    def live(self, spec, fingerprint=None):
        rt = PairRuntime(spec, CL.ClaudeAdapter())
        rt.proc = _AliveProc()
        rt.key_fingerprint = fingerprint
        rt.stop = Mock(return_value="")
        return rt

    def test_a_rotated_or_removed_key_respawns_the_runtime(self):
        self.write_key()
        spec = self.add(billing="api")
        reg = RuntimeRegistry()
        old = self.live(spec, CL.current_key_fingerprint())
        reg._runtimes["p"] = old
        with patch.object(PairRuntime, "start") as start:
            self.assertIs(reg.get_or_start(spec, CL.ClaudeAdapter()), old)  # unchanged key
            start.assert_not_called()
            self.write_key(OTHER_KEY)
            new = reg.get_or_start(spec, CL.ClaudeAdapter())
        self.assertIsNot(new, old)
        old.stop.assert_called_once()
        start.assert_called_once()

    def test_a_rotation_waits_while_work_is_pending(self):
        # A respawn kills background work and an open self-woken turn; the
        # new key applies once the runtime is idle.
        self.write_key()
        spec = self.add(billing="api")
        reg = RuntimeRegistry()
        busy = self.live(spec, CL.current_key_fingerprint())
        reg._runtimes["p"] = busy
        self.write_key(OTHER_KEY)
        with patch.object(PairRuntime, "start") as start, \
                patch.object(busy, "pending_work", return_value=[{"kind": "bash"}]):
            self.assertIs(reg.get_or_start(spec, CL.ClaudeAdapter()), busy)
        start.assert_not_called()
        busy.stop.assert_not_called()

    def test_plan_pairs_never_read_the_key_file(self):
        spec = self.add()
        reg = RuntimeRegistry()
        rt = self.live(spec)
        reg._runtimes["p"] = rt
        with patch.object(CL, "read_api_key", side_effect=AssertionError("key read")):
            self.assertIs(reg.get_or_start(spec, CL.ClaudeAdapter()), rt)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolTests(BillingCase):

    def test_pair_billing_refuses_without_a_key_file(self):
        self.add()
        with self.assertRaisesRegex(PairError, "does not exist"):
            tool(S.pair_billing)("p", "api")
        self.assertEqual(R.get_pair("p").billing, "plan")
        self.runtime.evict.assert_not_called()

    def test_pair_billing_switches_to_api_and_back(self):
        self.write_key()
        self.add()
        out = tool(S.pair_billing)("p", "api")
        self.assertEqual(R.get_pair("p").billing, "api")
        self.runtime.evict.assert_called_once()
        self.assertIn("key …WXYZ", out)
        self.assertIn("uncached", out)
        self.assertIn("can see the key in their environment", out)
        self.assertNotIn("SCRUB", out)
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("already uses api", tool(S.pair_billing)("p", "API"))
        self.assertEqual(self.runtime.evict.call_count, 1)  # no change, no respawn
        self.assertIn("back on the plan", tool(S.pair_billing)("p", "plan"))
        self.assertEqual(R.get_pair("p").billing, "plan")

    def test_the_tool_description_carries_the_binding_rule(self):
        doc = S.pair_billing.__doc__ if not hasattr(S.pair_billing, "fn") else S.pair_billing.fn.__doc__
        self.assertIn("ONLY when the user has expressly asked, in their own", doc)
        self.assertIn("Never choose it yourself", doc)

    def test_pair_billing_refuses_while_background_work_is_pending(self):
        self.write_key()
        self.add()

        def refuse(name, action, force):
            if not force:
                raise PairError(f"background pending; {action}")
        with patch.object(S, "_refuse_if_background", side_effect=refuse):
            with self.assertRaisesRegex(PairError, "switching billing to api"):
                tool(S.pair_billing)("p", "api")
            self.assertEqual(R.get_pair("p").billing, "plan")
            tool(S.pair_billing)("p", "api", force=True)
        self.assertEqual(R.get_pair("p").billing, "api")

    def test_pair_billing_warns_that_claude_ai_connectors_go_missing(self):
        self.write_key()
        self.add(mcp_whitelist=["claude.ai Gmail", "probe"])
        out = tool(S.pair_billing)("p", "api")
        self.assertIn("claude.ai connectors don't load on an API key: claude.ai Gmail", out)

    def test_billing_is_claude_only_and_validated(self):
        R.add_pair(PairSpec(name="cx", session_id="t", backend="codex", model="sol", cwd=str(self.directory)))
        self.write_key()
        for call in (lambda: tool(S.pair_billing)("cx", "api"), lambda: tool(S.pair_update)("cx", billing="plan")):
            with self.assertRaisesRegex(PairError, "Claude-only"):
                call()
        self.add()
        with self.assertRaisesRegex(PairError, "must be one of plan, api"):
            tool(S.pair_billing)("p", "credits")

    def test_pair_update_moves_to_the_plan_but_never_to_the_api(self):
        self.write_key()
        self.add(billing="api")
        with self.assertRaisesRegex(PairError, r"pair_billing\('p', 'api'\).*expressly asked"):
            tool(S.pair_update)("p", billing="api")
        self.runtime.evict.assert_not_called()
        self.assertIn("billing='plan'", tool(S.pair_update)("p", billing="plan"))
        self.assertEqual(R.get_pair("p").billing, "plan")
        self.runtime.evict.assert_called_once()  # a spawn-pinned field

    def test_no_billing_parameter_on_create_or_the_defaults(self):
        import inspect
        for fn in (S.pair_create, S.pair_settings_set):
            self.assertNotIn("billing", inspect.signature(tool(fn)).parameters)
        self.assertNotIn("billing", M.PairSpec(name="x", session_id="s").model_dump(exclude_defaults=True))
        self.assertIn("billing", M.SPAWN_PINNED_FIELDS)

    def test_the_registry_writes_billing_only_when_api(self):
        self.add("plan_pair")
        self.add("api_pair", billing="api", api_cost_usd=1.5)
        raw = json.loads(R.registry_path().read_text(encoding="utf-8"))["pairs"]
        self.assertNotIn("billing", raw["plan_pair"])
        self.assertNotIn("api_cost_usd", raw["plan_pair"])
        self.assertEqual(raw["api_pair"]["billing"], "api")
        self.assertEqual(raw["api_pair"]["api_cost_usd"], 1.5)
        # An unknown spelling is not silently read as either mode.
        self.assertEqual(PairSpec(name="x", session_id="s", billing=" API ").billing, "api")
        with self.assertRaises(ValueError):
            PairSpec(name="x", session_id="s", billing="credits")

    def test_a_fork_starts_on_the_plan(self):
        self.write_key()
        self.add(billing="api", api_cost_usd=3.0)
        seen = []

        def fake_fork(spec, sentinel, **_kw):
            seen.append(spec.billing)
            return "fork-sid"
        with patch.object(CL.ClaudeAdapter, "fork", side_effect=fake_fork):
            tool(S.pair_fork)("p")
        fork = R.get_pair("p-fork")
        self.assertEqual((fork.billing, fork.api_cost_usd), ("plan", 0.0))
        self.assertEqual(seen, ["plan"])  # the fork-init turn too
        self.assertEqual(R.get_pair("p").billing, "api")  # the source is untouched

    def test_pair_info_shows_billing_and_costs_but_never_the_key(self):
        self.write_key()
        self.add(billing="api", total_cost_usd=2.5, api_cost_usd=1.25)
        info = tool(S.pair_info)("p")
        self.assertIn("billing: api (key …WXYZ)", info)
        self.assertIn("cost: $2.50 at list prices, $1.25 of it billed to the API key", info)
        for shown in (info, tool(S.pair_info)("p", verbose=True)):
            self.assertNotIn(FAKE_KEY, shown)
        CL.api_key_path().unlink()
        self.assertIn("billing: api (key file missing", tool(S.pair_info)("p"))
        self.add("plain")
        self.assertIn("billing: plan", tool(S.pair_info)("plain"))
        listing = tool(S.pair_list)()
        self.assertIn("p (opus, API billing", listing)
        self.assertIn("plain (opus, 0 turns", listing)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

class CostTests(BillingCase):

    def test_each_result_gets_its_step_of_the_running_total(self):
        # Haiku probe 2026-10-11: total_cost_usd 0.0078 → 0.0105 → 0.0119.
        rt = PairRuntime(self.add(), CL.ClaudeAdapter())
        shares = []
        for total in (0.0078, 0.0105, 0.0119):
            ev = {"type": "result", "subtype": "success", "total_cost_usd": total}
            rt._after_result(ev)
            shares.append(round(ev["_turn_cost_usd"], 6))
        self.assertEqual(shares, [0.0078, 0.0027, 0.0014])
        rt._cost_seen = 0.0  # what start() does for a fresh process
        ev = {"type": "result", "subtype": "success", "total_cost_usd": 0.002}
        rt._after_result(ev)
        self.assertEqual(ev["_turn_cost_usd"], 0.002)
        # A lower total means the counter restarted: all of it is this turn's.
        ev = {"type": "result", "subtype": "success", "total_cost_usd": 0.0015}
        rt._after_result(ev)
        self.assertEqual(ev["_turn_cost_usd"], 0.0015)

    def test_missing_or_zero_totals_say_nothing(self):
        # Review: 1.0, none, 0, 1.25 booked $2.25 — the missing / zero totals
        # re-booked the running total. Only a positive lower total restarts.
        rt = PairRuntime(self.add(), CL.ClaudeAdapter())
        shares = []
        for total in (1.0, None, 0, 1.25):
            ev = {"type": "result", "subtype": "success"}
            if total is not None:
                ev["total_cost_usd"] = total
            rt._after_result(ev)
            shares.append(ev["_turn_cost_usd"])
        self.assertEqual(shares, [1.0, 0.0, 0.0, 0.25])
        self.assertEqual(R.get_pair("p").total_cost_usd, 1.25)  # no turn was open: all booked as is

    def test_a_result_with_no_turn_open_still_books_its_cost(self):
        self.add(billing="api")
        rt = PairRuntime(R.get_pair("p"), CL.ClaudeAdapter())
        rt._after_result({"type": "result", "subtype": "success", "total_cost_usd": 0.5})
        spec = R.get_pair("p")
        self.assertEqual((spec.total_cost_usd, spec.api_cost_usd), (0.5, 0.5))

    def test_compaction_and_invoke_spend_is_booked(self):
        self.write_key()
        spec = self.add(billing="api", total_cost_usd=1.0, api_cost_usd=1.0)
        events = [{"type": "system", "subtype": "init"},
                  {"type": "result", "subtype": "success", "total_cost_usd": 0.25}]
        out = "\n".join(json.dumps(e) for e in events)
        with patch.object(subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=out.encode(), stderr=b"")):
            CL.ClaudeAdapter()._run_stream_json(spec, ["/compact"], timeout_seconds=5)
        spec = R.get_pair("p")
        self.assertEqual((spec.total_cost_usd, spec.api_cost_usd), (1.25, 1.25))

    def test_the_send_result_prefers_the_turns_share(self):
        spec = self.add()
        base = {"type": "result", "subtype": "success", "result": "ok", "session_id": "p-sid",
                "total_cost_usd": 0.5}
        adapter = CL.ClaudeAdapter()
        with patch.object(adapter, "_read_last_turn_context_fill", return_value=None):
            self.assertEqual(adapter._build_send_result(spec, dict(base, _turn_cost_usd=0.1)).cost_usd, 0.1)
            self.assertEqual(adapter._build_send_result(spec, dict(base)).cost_usd, 0.5)  # one-shot

    def run_send(self, spec, cost):
        adapter = CL.ClaudeAdapter()
        result = SendResult(name=spec.name, response="ok", session_id=spec.session_id,
                            model_used="claude-opus-5-5", cost_usd=cost, duration_ms=1)
        runner = S._build_send_runner(spec.name, "hi", hard_timeout_seconds=5, override_model=None,
                                      override_effort=None, override_permission_mode=None)
        with patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(adapter, "send", return_value=result):
            return runner(None)

    def test_the_registry_adds_turn_shares_and_the_api_total(self):
        self.add("plan_pair")
        self.add("api_pair", billing="api")
        for name in ("plan_pair", "api_pair"):
            for cost in (0.25, 0.5):
                result = self.run_send(R.get_pair(name), cost)
        plan, api = R.get_pair("plan_pair"), R.get_pair("api_pair")
        self.assertEqual((plan.total_cost_usd, plan.api_cost_usd), (0.75, 0.0))
        self.assertEqual((api.total_cost_usd, api.api_cost_usd), (0.75, 0.75))
        self.assertEqual((result.billing, result.api_cost_usd), ("api", 0.75))
        footer = S._fmt_send_result(result)
        self.assertIn("API $0.5000 (pair total $0.75)", footer)
        plain = self.run_send(R.get_pair("plan_pair"), 0.25)
        self.assertNotIn("API $", S._fmt_send_result(plain))

    def test_a_self_woken_turn_adds_its_share(self):
        self.add(billing="api")
        rt = PairRuntime(R.get_pair("p"), CL.ClaudeAdapter())
        rt._drift_checked = True
        rt._after_result({"type": "result", "subtype": "success", "total_cost_usd": 1.0})  # earlier turn
        rt._open_implicit_turn()
        with patch.object(rt, "_close_activity_turn_with_triage", lambda *a, **kw: None):
            rt._after_result({"type": "result", "subtype": "success", "result": "woke",
                              "session_id": "p-sid", "total_cost_usd": 1.25})
        spec = R.get_pair("p")
        # 1.0 from the earlier result (no turn open, booked as is) + this turn's 0.25.
        self.assertEqual((spec.total_cost_usd, spec.api_cost_usd), (1.25, 1.25))
        self.assertEqual(spec.self_woken_pending[-1]["cost_usd"], 0.25)


# ---------------------------------------------------------------------------
# Credit errors
# ---------------------------------------------------------------------------

class CreditTests(BillingCase):

    def build(self, spec, **result):
        ev = {"type": "result", "subtype": "success", "session_id": spec.session_id, **result}
        adapter = CL.ClaudeAdapter()
        with patch.object(adapter, "_read_last_turn_context_fill", return_value=None):
            return adapter._build_send_result(spec, ev)

    def test_a_credit_error_on_an_api_pair_reads_like_a_usage_limit(self):
        spec = self.add(billing="api")
        for result in ({"result": "Credit balance too low · Add funds to continue", "is_error": True},
                       {"result": "Your credit balance is too low to access the Anthropic API.",
                        "api_error_status": 400}):
            with self.subTest(result=result["result"][:30]):
                r = self.build(spec, **result)
                self.assertEqual(r.safety_kind, "credit_exhausted")
                text = S._fmt_send_result(r)
                self.assertIn("API CREDITS EXHAUSTED", text)
                self.assertIn("did NOT do the work", text)
                self.assertIn("pair_update('p', billing=\"plan\")", text)

    def test_plan_pairs_and_real_answers_are_not_credit_or_key_errors(self):
        # Only an ERROR result (is_error / api_error_status) of an API pair
        # counts — an answer quoting the texts is an answer (review round 1).
        api = self.add("api_pair", billing="api")
        self.assertIsNone(self.build(self.add("plan_pair"), result="Credit balance too low",
                                     is_error=True).safety_kind)
        for answer in ("Credit balance too low · Add funds",
                       "Fixed the credit balance check in billing.py; it now says your credit "
                       "balance is too low when it is. " + "Details follow. " * 40,
                       "The CLI prints 'Invalid API key · Fix external API key' when the key "
                       "is wrong; I added a test for it. " + "More. " * 40):
            with self.subTest(answer=answer[:30]):
                r = self.build(api, result=answer)
                self.assertIsNone(r.safety_kind)
                self.assertNotIn("EXHAUSTED", S._fmt_send_result(r))
                self.assertNotIn("REJECTED", S._fmt_send_result(r))

    def test_switched_off_keys_and_orgs_read_as_rejected(self):
        api = self.add(billing="api")
        for text in ("API key authentication is disabled for this organization",
                     "This organization has been disabled."):
            with self.subTest(text=text):
                self.assertEqual(self.build(api, result=text, is_error=True,
                                            api_error_status=403).safety_kind, "api_key_rejected")

    def test_failed_requests_cost_nothing(self):
        api = self.add(billing="api")
        for result in ({"result": "Invalid API key · Fix external API key", "api_error_status": 401},
                       {"result": "Credit balance too low", "is_error": True, "num_turns": 1}):
            self.assertEqual(self.build(api, total_cost_usd=0.0024, _turn_cost_usd=0.0024,
                                        **result).cost_usd, 0.0)

    def test_a_turn_that_runs_out_partway_keeps_its_cost(self):
        r = self.build(self.add(billing="api"), result="Credit balance too low", is_error=True,
                       num_turns=4, total_cost_usd=0.3, _turn_cost_usd=0.3)
        self.assertEqual((r.safety_kind, r.cost_usd), ("credit_exhausted", 0.3))

    def test_an_unscoped_identity_key_names_the_fix(self):
        text = ("anthropic-workspace-id is required when authenticating with an identity-linked "
                "API key that is not scoped to a single workspace")
        r = self.build(self.add(billing="api"), result=text, is_error=True, api_error_status=400)
        self.assertEqual(r.safety_kind, "api_key_rejected")
        self.assertIn("scoped to a single workspace", S._fmt_send_result(r))
        self.assertIsNone(self.build(self.add("plain_api", billing="api"), result=text).safety_kind)

    def test_only_the_last_result_of_a_failed_process_is_matched(self):
        # Review: an error_max_turns failure whose tool output had quoted the
        # key error came back as "the API rejected the key".
        self.write_key()
        spec = self.add(billing="api")
        events = [{"type": "user", "message": {"content": [{"type": "tool_result",
                                                            "content": "Invalid API key · Fix external API key"}]}},
                  {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""}]
        out = "\n".join(json.dumps(e) for e in events)
        with patch.object(subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=out.encode(), stderr=b"")):
            with self.assertRaises(CLIError) as caught:
                CL.ClaudeAdapter()._run_stream_json(spec, ["/compact"], timeout_seconds=5)
            self.assertNotIn("rejected the key", str(caught.exception))
            with self.assertRaises(CLIError) as caught:
                CL.ClaudeAdapter()._run_print(["--print", "-p", "x"], timeout_seconds=5, pair_name="p",
                                              env_spec=spec)
            self.assertNotIn("rejected the key", str(caught.exception))
        last = json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Fix external API key"})
        with patch.object(subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=(out + "\n" + last).encode(), stderr=b"")):
            with self.assertRaisesRegex(CLIError, "rejected the key"):
                CL.ClaudeAdapter()._run_stream_json(spec, ["/compact"], timeout_seconds=5)

    def test_stream_json_failures_map_like_one_shots(self):
        # Compaction, /context and pair_invoke on an API pair (review round 1).
        self.write_key()
        spec = self.add(billing="api")
        for text, pattern in (("Credit balance too low · Add funds", "credits are used up"),
                              ("Invalid API key · Fix external API key", "rejected the key")):
            out = json.dumps({"type": "result", "is_error": True, "result": text})
            with self.subTest(text=text), patch.object(subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stdout=out.encode(), stderr=b"")):
                with self.assertRaisesRegex(CLIError, pattern):
                    CL.ClaudeAdapter()._run_stream_json(spec, ["/compact"], timeout_seconds=5)

    def test_a_rejected_key_is_not_a_transient_error(self):
        # The fake-key probe's exact result text (2026-10-11).
        r = self.build(self.add(billing="api"), result="Invalid API key · Fix external API key",
                       is_error=True, api_error_status=401)
        self.assertEqual(r.safety_kind, "api_key_rejected")
        text = S._fmt_send_result(r)
        self.assertIn("API KEY REJECTED", text)
        self.assertIn(str(CL.api_key_path()), text)
        self.assertNotIn("retry usually resolves it", text)
        self.assertNotEqual(self.build(self.add("plan_pair"), result="Invalid API key",
                                       is_error=True).safety_kind, "api_key_rejected")

    def test_a_one_shot_credit_failure_names_the_way_back(self):
        self.write_key()
        spec = self.add(billing="api")
        out = {"type": "result", "is_error": True, "result": "Credit balance too low · Add funds"}
        with patch.object(subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=json.dumps(out).encode(), stderr=b"")):
            with self.assertRaisesRegex(CLIError, r"credits are used up.*billing=\"plan\""):
                CL.ClaudeAdapter()._run_print(["--print", "-p", "x"], timeout_seconds=5, pair_name="p",
                                              env_spec=spec)

    def test_a_self_woken_turn_reports_it_too(self):
        self.add(billing="api")
        rt = PairRuntime(R.get_pair("p"), CL.ClaudeAdapter())
        rt._drift_checked = True
        rt._open_implicit_turn()
        task = rt._implicit_task
        with patch.object(rt, "_close_activity_turn_with_triage", lambda *a, **kw: None), \
                patch.object(CL.ClaudeAdapter, "_read_last_turn_context_fill", return_value=None):
            rt._after_result({"type": "result", "subtype": "success", "session_id": "p-sid",
                              "result": "Credit balance too low · Add funds", "is_error": True})
        self.assertEqual(AT.load_task(task.task_id).result.safety_kind, "credit_exhausted")


# ---------------------------------------------------------------------------
# Haiku effort
# ---------------------------------------------------------------------------

class HaikuEffortTests(unittest.TestCase):

    def test_haiku_5_takes_every_level_and_haiku_4_5_none(self):
        for model in ("haiku", "claude-haiku-5-5", "haiku-5-5"):
            with self.subTest(model=model):
                for effort in ("low", "medium", "high", "xhigh", "max"):
                    self.assertEqual(M.coerce_effort_for_model(model, effort), (effort, None))
                self.assertEqual(M.default_effort_for_model(model), "high")
        for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                self.assertEqual(M.coerce_effort_for_model(model, "low")[0], None)
                self.assertIsNone(M.default_effort_for_model(model))
        self.assertEqual(PairSpec(name="h", session_id="s", model="haiku", effort="low").effort, "low")


# ---------------------------------------------------------------------------
# Shutdown fix A (Incident 2026-10-04)
# ---------------------------------------------------------------------------

class ShutdownTests(BillingCase):

    def test_after_the_exit_handler_nothing_spawns(self):
        with patch.object(RT, "_REGISTRY", None), patch.object(RT._activity, "shutdown", lambda: None):
            RT._shutdown_runtimes()
        self.assertTrue(RT.shutting_down())
        spec = self.add()
        with self.assertRaisesRegex(ServerShuttingDown, "NOT DELIVERED"):
            RuntimeRegistry().get_or_start(spec, CL.ClaudeAdapter())
        with self.assertRaises(ServerShuttingDown):
            PairRuntime(spec, CL.ClaudeAdapter()).start()
        self.popen.assert_not_called()

    def test_a_send_queued_behind_a_self_woken_turn_is_not_delivered(self):
        # The incident: the exit handler stops the runtime, which ends the
        # self-woken turn and releases the queued send. It must not spawn a
        # new runtime; it fails as NOT DELIVERED (never "may have completed").
        spec = self.add("pair")
        adapter = CL.ClaudeAdapter()
        rt = PairRuntime(spec, adapter)
        rt._drift_checked = True
        rt.proc = _AliveProc()
        real_stop = rt.stop

        def safe_stop(reason=None):
            rt.proc = None  # never reach the taskkill path with a fake pid
            return real_stop(reason)
        rt.stop = safe_stop
        reg = RuntimeRegistry()
        reg._runtimes["pair"] = rt
        rt._open_implicit_turn()
        implicit = rt._implicit_task
        waiting = threading.Event()
        original_wait = rt.wait_for_implicit_idle

        def wait(*args):
            waiting.set()
            return original_wait(*args)
        runner = S._build_send_runner("pair", "next", hard_timeout_seconds=5, override_model=None,
                                      override_effort=None, override_permission_mode=None)
        self.popen.side_effect = None
        with patch.object(RT, "registry", return_value=reg), patch.object(RT, "_REGISTRY", reg), \
                patch.object(RT._activity, "shutdown", lambda: None), \
                patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(adapter, "session_exists", return_value=True), \
                patch.object(rt, "wait_for_implicit_idle", side_effect=wait):
            task = AT.start_task("pair", "next", runner, queued=True)
            self.assertTrue(waiting.wait(5))
            RT._shutdown_runtimes()
            done = AT.wait_for_task(task.task_id, 5)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.error, NOT_DELIVERED_MESSAGE)
        self.assertTrue(done.queued)
        self.popen.assert_not_called()
        self.assertEqual(AT.load_task(implicit.task_id).status, "stopped")

    def test_a_live_runtime_releasing_a_queued_send_writes_nothing(self):
        # Review round 1: the process can still be alive when the wait ends
        # (the exit handler ended the self-woken turn first). The send must
        # refuse before it is promoted — nothing reaches stdin.
        spec = self.add("pair")
        adapter = CL.ClaudeAdapter()
        rt = PairRuntime(spec, adapter)
        rt._drift_checked = True
        stdin = Mock()
        rt.proc = SimpleNamespace(pid=0, returncode=None, stdin=stdin, poll=lambda: None)
        rt._open_implicit_turn()
        waiting = threading.Event()
        original_wait = rt.wait_for_implicit_idle

        def wait(*args):
            waiting.set()
            return original_wait(*args)
        runner = S._build_send_runner("pair", "next", hard_timeout_seconds=5, override_model=None,
                                      override_effort=None, override_permission_mode=None)
        reg = SimpleNamespace(get_or_start=lambda *a: rt, get_or_none=lambda name: rt)
        with patch.object(RT, "registry", return_value=reg), \
                patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(adapter, "session_exists", return_value=True), \
                patch.object(rt, "wait_for_implicit_idle", side_effect=wait):
            task = AT.start_task("pair", "next", runner, queued=True)
            self.assertTrue(waiting.wait(5))
            RT._SHUTTING_DOWN.set()  # the exit handler's first step …
            rt._abort_implicit_turn("the owning MCP process exited")  # … then the turn ends
            done = AT.wait_for_task(task.task_id, 5)
        self.assertEqual((done.status, done.error, done.queued), ("failed", NOT_DELIVERED_MESSAGE, True))
        stdin.write.assert_not_called()
        self.popen.assert_not_called()

    def test_the_watchers_say_not_delivered_for_a_queued_task(self):
        import contextlib
        from claude_squared import _wait_script as W
        from claude_squared import __main__ as MAIN
        g = {"__name__": "offline_wait"}
        exec(compile(W.WAIT_SCRIPT_SOURCE, "wait.py", "exec"), g)
        g["_av_task_owner_alive"] = lambda *a: False
        data = {"task_id": "t", "pair_name": "pair", "owner_pid": 4, "queued": True,
                "started_at": datetime.utcnow().isoformat(), "status": "running"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(g["_dead_owner_exit"](data, g["_av_now"]()), 4)
        self.assertIn("not delivered", err.getvalue())
        self.assertNotIn("completion", err.getvalue())
        task_id = self.task_file(queued=True, owner_pid=4)
        err = io.StringIO()
        with patch.object(AT, "_is_pid_alive", return_value=False), contextlib.redirect_stderr(err):
            self.assertEqual(MAIN._cmd_wait([task_id]), 4)
        self.assertIn("not delivered", err.getvalue())
        self.assertNotIn("may have completed", err.getvalue())

    def task_file(self, *, queued: bool, owner_pid: int):
        if "pair" not in R.load().pairs:
            self.add("pair")
        state = AsyncTaskState(task_id=f"t-{queued}", pair_name="pair", message="m", status="running",
                               started_at=datetime.utcnow(), owner_pid=owner_pid, queued=queued,
                               execution_started_at=None if queued else datetime.utcnow())
        AT._save(state)
        return state.task_id

    def test_a_successor_never_says_a_queued_send_may_have_completed(self):
        queued, started = self.task_file(queued=True, owner_pid=4), self.task_file(queued=False, owner_pid=4)
        with patch.object(AT, "owner_alive", return_value=False):
            AT.reap_orphan(queued, legacy=True)
            AT.reap_orphan(started, legacy=True)
        self.assertEqual(AT.load_task(queued).error, NOT_DELIVERED_MESSAGE)
        self.assertTrue(AT.load_task(started).error.startswith(AT.ORPHAN_ERROR_PREFIX))

    def test_the_own_exit_sweep_says_not_delivered_for_a_queued_send(self):
        queued = self.task_file(queued=True, owner_pid=os.getpid())
        started = self.task_file(queued=False, owner_pid=os.getpid())
        AT._cleanup_orphaned_running_tasks()
        self.assertEqual(AT.load_task(queued).error, NOT_DELIVERED_MESSAGE)
        self.assertIn("may have completed", AT.load_task(started).error)


if __name__ == "__main__":
    unittest.main()
