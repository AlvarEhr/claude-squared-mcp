"""v0.12.0 smoke: self-woken turns (implicit tasks), background-launch
detection, usage-limit signal, and the send-entry FIFO/drain path.

No CLI, no network, no subprocess: feeds synthetic stream-json events straight
into ``PairRuntime._on_event_for_log`` (the reader-thread entry point) and
inspects the on-disk task store + main.log + SendResult/footer rendering.
Runs against a throwaway CLAUDE_HOME so it never touches real pairs.

Run:  PYTHONIOENCODING=utf-8 python -u tests/smoke_selfwoken.py
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="cs-selfwoken-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = _TMP.name  # MUST precede the package import
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_squared import async_tasks, registry as reg_mod  # noqa: E402
from claude_squared import runtime as runtime_mod  # noqa: E402
from claude_squared import server  # noqa: E402  (installs wait.py under CLAUDE_HOME)
from claude_squared.adapters.claude import ClaudeAdapter  # noqa: E402
from claude_squared.errors import CLIError  # noqa: E402
from claude_squared.models import PairSpec, SendResult  # noqa: E402
from claude_squared.runtime import PairRuntime, RuntimeRegistry  # noqa: E402

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def tool(fn):
    """FastMCP may wrap @mcp.tool functions; reach the plain callable."""
    return getattr(fn, "fn", fn)


SID = str(uuid.uuid4())
CWD = os.path.join(_TMP.name, "work")
os.makedirs(CWD, exist_ok=True)
spec = PairSpec(name="swt", session_id=SID, purpose="self-woken smoke", cwd=CWD)
reg_mod.add_pair(spec)
adapter = ClaudeAdapter()


def ev_assistant(text, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "session_id": SID,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def ev_agent_launch(tool_id):
    return {"type": "assistant", "parent_tool_use_id": None, "session_id": SID,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                        "name": "Agent", "input": {"description": "bg recon",
                        "subagent_type": "general-purpose", "run_in_background": True}}]}}


def ev_tool_result(tool_id, content, parent=None):
    return {"type": "user", "parent_tool_use_id": parent, "session_id": SID,
            "message": {"role": "user", "content": [{"type": "tool_result",
                        "tool_use_id": tool_id, "content": content}]}}


def ev_result(text, subtype="success", cost=0.42):
    return {"type": "result", "subtype": subtype, "is_error": False, "duration_ms": 1234,
            "result": text, "session_id": SID, "total_cost_usd": cost,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "modelUsage": {"claude-opus-5": {"contextWindow": 1_000_000}},
            "terminal_reason": "completed", "stop_reason": "end_turn", "permission_denials": []}


def feed(rt, ev):
    rt._on_event_for_log(json.dumps(ev))


AGENT_MARK = [{"type": "text", "text": "Async agent launched successfully. (This tool result is "
              "internal metadata — never quote it)\nagentId: a1b2"}]
BASH_MARK = "Command running in background with ID: btt2s2s6s. Output is being written to: x"
WF_MARK = "Workflow launched in background. Task ID: wpza7ry4a"

print("=== 1. a solicited task first (so 'latest solicited' has something to find) ===")
sol = async_tasks.start_task("swt", "hello", lambda tid: SendResult(
    name="swt", response="hi back", session_id=SID, model_used="claude-opus-5",
    cost_usd=0.01, duration_ms=5))
sol_final = async_tasks.wait_for_task(sol.task_id, timeout_s=10)
check("solicited task done", sol_final is not None and sol_final.status == "done")

print("\n=== 2. leaked sub-agent events must NOT open a self-woken turn ===")
rt = PairRuntime(spec, adapter)
feed(rt, ev_assistant("I am a sub-agent", parent="toolu_sub"))
feed(rt, ev_tool_result("toolu_sub_t", "sub result", parent="toolu_sub"))
check("no implicit scope on leaked events", rt._implicit_scope is None)
check("no running task registered", async_tasks.list_running_task_ids_for_pair("swt") == [])

print("\n=== 3. a top-level event with no send() scope OPENS a self-woken turn ===")
feed(rt, ev_assistant("Resuming after background work."))
check("implicit scope open", rt._implicit_scope is not None)
check("implicit_done cleared", not rt._implicit_done.is_set())
running = async_tasks.list_running_task_ids_for_pair("swt")
check("exactly one running task on disk", len(running) == 1)
imp_id = running[0] if running else None
imp_state = async_tasks.load_task(imp_id) if imp_id else None
check("task is self-woken", imp_state is not None and async_tasks.is_self_woken_task(imp_state))
check("owner_pid is this process", imp_state is not None and imp_state.owner_pid == os.getpid())
log_text = rt.main_log_path.read_text(encoding="utf-8")
check("main.log has SELF-WOKEN marker", "=== SELF-WOKEN TURN (task" in log_text)
import re as _re  # noqa: E402
check("SELF-WOKEN marker does NOT match the TURN regex",
      not _re.search(r"=== TURN .* \(\d+ms\) ===", log_text))

print("\n=== 4. background-launch markers are recorded on the open scope ===")
feed(rt, ev_agent_launch("toolu_a1"))
feed(rt, ev_tool_result("toolu_a1", AGENT_MARK))          # Agent-return branch
feed(rt, ev_tool_result("toolu_b1", BASH_MARK))           # non-Agent branch
feed(rt, ev_tool_result("toolu_w1", WF_MARK))
feed(rt, ev_tool_result("toolu_n1", "plain result, not a launch"))
check("launch kinds recorded", rt._implicit_scope.background_launches == ["agent", "bash", "workflow"])
check("main.log notes the launches",
      rt.main_log_path.read_text(encoding="utf-8").count("[background launch:") == 3)

print("\n=== 5. pair_status / pair_poll see the self-woken turn as in-flight ===")
runtime_mod.registry()._runtimes["swt"] = rt
st = tool(server.pair_status)("swt")
check("pair_status says self-woken turn in progress", "self-woken turn in progress" in st)
check("pair_status lists the task", imp_id is not None and imp_id[:8] in st)
pp = tool(server.pair_poll)("swt")
check("pair_poll(name) resolves to the self-woken task", imp_id is not None and imp_id[:8] in pp)
check("pair_poll headline says SELF-WOKEN", "SELF-WOKEN turn" in pp)
check("pair_poll names the latest pair_send task", sol.task_id[:8] in pp)
check("latest_task_id_for_pair(solicited_only) skips it",
      async_tasks.latest_task_id_for_pair("swt", solicited_only=True) == sol.task_id)

print("\n=== 6. the result event CLOSES it: task done, registry totals, handoff record ===")
before = reg_mod.get_pair("swt")
rt._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
rt._jsonl_path.write_text("{}", encoding="utf-8")  # the turn "wrote" the session JSONL
rt._last_seen_jsonl_mtime = 0.0
feed(rt, ev_result("Synthesized: all three reports folded in."))
check("implicit scope cleared", rt._implicit_scope is None)
check("implicit_done set", rt._implicit_done.is_set())
done_state = async_tasks.load_task(imp_id)
check("task done on disk", done_state.status == "done")
check("task carries the response", done_state.result is not None and "Synthesized" in done_state.result.response)
check("task result has the log range", done_state.result.log_line_start is not None
      and done_state.result.log_line_end >= done_state.result.log_line_start)
check("task result carries background_launches", done_state.result.background_launches == ["agent", "bash", "workflow"])
after = reg_mod.get_pair("swt")
check("registry turn_count +1", after.turn_count == before.turn_count + 1)
check("registry cost += 0.42", abs(after.total_cost_usd - before.total_cost_usd - 0.42) < 1e-9)
check("result_seq bumped", rt._result_seq == 1)
check("jsonl watermark refreshed → warm runtime kept (is_stale False)",
      rt._last_seen_jsonl_mtime == rt._current_jsonl_mtime() and rt.is_stale() is False)
pend = reg_mod.get_pair("swt").self_woken_pending
check("record parked on the spec (self_woken_pending)", len(pend) == 1
      and pend[0]["task_id"] == imp_id and pend[0]["status"] == "done")
pp_done = tool(server.pair_poll)(imp_id, with_turn_log=True)
check("pair_poll(done) labels SELF-WOKEN + shows the reply",
      "(SELF-WOKEN turn" in pp_done and "Synthesized" in pp_done)
check("pair_poll(done) turn log includes the SELF-WOKEN marker line", "SELF-WOKEN TURN (task" in pp_done)

print("\n=== 7. send-entry: drain + handoff, then solicited scope takes precedence ===")
rt._stdout_q.put("stale line")
waited = rt._prepare_solicited_turn(None, None)
check("no wait when nothing is open", waited < 0.5)
taken = server._take_self_woken_pending("swt")
check("server pops the pending record and clears it", len(taken) == 1
      and taken[0]["task_id"] == imp_id and reg_mod.get_pair("swt").self_woken_pending == [])
check("stale queue drained", rt._stdout_q.empty())
check("solicited scope open", rt._current_scope is not None)
feed(rt, ev_assistant("solicited turn content"))
check("top-level event does NOT open an implicit turn while solicited scope is open",
      rt._implicit_scope is None)
feed(rt, ev_tool_result("toolu_a2", AGENT_MARK))
feed(rt, ev_result("placeholder: recon is out, I'll synthesize when it lands"))
snap = rt._current_scope.result_snapshot
check("reader snapshotted the solicited result", snap is not None and snap["background_launches"] == ["agent"])
check("no self-woken record manufactured for a solicited result",
      reg_mod.get_pair("swt").self_woken_pending == [])
with rt._scope_lock:
    rt._current_scope = None  # what send()'s finally does
feed(rt, ev_assistant("continuation after the placeholder"))
check("after the solicited scope clears, the continuation opens a self-woken turn",
      rt._implicit_scope is not None)

print("\n=== 8. is_stale() is False while a self-woken turn is in progress ===")
rt._last_seen_jsonl_mtime = 0.0  # JSONL exists from §6 → would be stale if unguarded
check("is_stale guarded during implicit turn", rt.is_stale() is False)

print("\n=== 9. send-entry with an implicit turn open + dead proc → CRASHED, task failed ===")
try:
    rt._prepare_solicited_turn(5, None)
    check("raised CLIError", False)
except CLIError as e:
    check("raised CLIError with CRASHED prefix", str(e).startswith(async_tasks.CRASHED_ERROR_PREFIX))
check("implicit turn aborted", rt._implicit_scope is None and rt._implicit_done.is_set())
last = async_tasks.latest_task_id_for_pair("swt")
last_state = async_tasks.load_task(last)
check("its task is failed/CRASHED", last_state.status == "failed"
      and (last_state.error or "").startswith(async_tasks.CRASHED_ERROR_PREFIX))
check("is_stale back to normal after an ABORTED turn (no watermark refresh on abort)",
      rt.is_stale() is True)
pend9 = reg_mod.get_pair("swt").self_woken_pending
check("CRASHED abort parked on the spec", len(pend9) == 1 and pend9[0]["status"] == "failed")
server._take_self_woken_pending("swt")
check("wait_for_implicit_idle returns immediately when idle", rt.wait_for_implicit_idle(1, None) < 0.5)

print("\n=== 9b. cross-process FIFO: a send waits for ANOTHER live process's self-woken task ===")
foreign = async_tasks.register_external_task("swt", async_tasks.SELF_WOKEN_MESSAGE)
foreign.owner_pid = os.getppid()  # some other process that is alive right now
async_tasks._save(foreign)
check("foreign task detected", server._foreign_self_woken_task_ids("swt") == [foreign.task_id])
t0 = datetime.utcnow()
try:
    server._wait_for_foreign_self_woken("swt", 2, None)
    check("raised PairError after the timeout", False)
except server.PairError as e:
    check("raised PairError after the timeout", "another MCP process" in str(e))
check("…and actually waited ~2s", (datetime.utcnow() - t0).total_seconds() >= 1.5)
async_tasks.finalize_external_task(foreign, status="done")
check("no wait once it finalizes", server._wait_for_foreign_self_woken("swt", 2, None) < 0.5)
dead = async_tasks.register_external_task("swt", async_tasks.SELF_WOKEN_MESSAGE)
dead.owner_pid = 999_999_999
async_tasks._save(dead)
check("a dead owner's task is NOT waited for", server._foreign_self_woken_task_ids("swt") == [])
async_tasks.finalize_external_task(dead, status="failed", error="test cleanup")
own = async_tasks.register_external_task("swt", async_tasks.SELF_WOKEN_MESSAGE)  # owner = us
check("own-process task is NOT foreign", server._foreign_self_woken_task_ids("swt") == [])
async_tasks.finalize_external_task(own, status="done")
server._take_self_woken_pending("swt")

print("\n=== 10. stop() finalizes an open self-woken turn as stopped ===")
rt2 = PairRuntime(spec, adapter)
feed(rt2, ev_assistant("woke up"))
tid2 = async_tasks.list_running_task_ids_for_pair("swt")
check("one running task before stop", len(tid2) == 1)
out = rt2.stop()
check("stop() returns 'no runtime' (no proc)", out == "no runtime")
st2 = async_tasks.load_task(tid2[0]) if tid2 else None
check("task stopped on disk", st2 is not None and st2.status == "stopped")
check("nothing left running", async_tasks.list_running_task_ids_for_pair("swt") == [])
check("stop() is idempotent", rt2.stop() == "no runtime")

print("\n=== 11. reaper: idle self-woken turn → ABANDONED (failed) ===")
rt3 = PairRuntime(spec, adapter)
feed(rt3, ev_assistant("woke up, then went quiet"))
rt3._last_log_activity_at = datetime.utcnow() - timedelta(hours=2)
rt3.last_activity = datetime.utcnow() - timedelta(hours=2)
tid3 = async_tasks.list_running_task_ids_for_pair("swt")
reg = RuntimeRegistry(idle_timeout_seconds=600)
reg._runtimes["swt"] = rt3
reg._evict_idle()
st3 = async_tasks.load_task(tid3[0]) if tid3 else None
check("task failed with ABANDONED prefix", st3 is not None and st3.status == "failed"
      and (st3.error or "").startswith(async_tasks.ABANDONED_ERROR_PREFIX))
check("runtime evicted (not alive)", "swt" not in reg._runtimes)

print("\n=== 12. usage-limit result is labeled, not mistaken for an answer ===")
lim = adapter._build_send_result(spec, ev_result("You've hit your session limit · resets 2pm (Europe/London)"))
check("safety_kind == usage_limit", lim.safety_kind == "usage_limit")
check("curly apostrophe also matches",
      adapter._build_send_result(spec, ev_result("You’ve hit your weekly limit · resets Monday")).safety_kind == "usage_limit")
check("normal reply NOT flagged", adapter._build_send_result(spec, ev_result("All good.")).safety_kind is None)
check("terminal_reason captured", lim.terminal_reason == "completed")
check("footer shows ⏸ USAGE LIMIT", "⏸ USAGE LIMIT" in server._fmt_send_result(lim))

print("\n=== 13. footer rendering of the new signals ===")
r = SendResult(name="swt", response="recon is out", session_id=SID, model_used="claude-opus-5",
               cost_usd=0.1, duration_ms=10, background_launches=["agent", "agent", "workflow"],
               self_woken_completed=[{"task_id": imp_id, "status": "done", "log_line_start": 3,
                                      "log_line_end": 9, "response_preview": "Synthesized", "cost_usd": 0.42}],
               self_woken_waited_s=12.0, terminal_reason="completed")
ftxt = server._fmt_send_result(r)
check("⏳ background launched with counts", "⏳ BACKGROUND WORK LAUNCHED (2 agent, 1 workflow)" in ftxt)
check("⏮ self-woken completed listed", "⏮ 1 SELF-WOKEN TURN completed" in ftxt and "main.log:3-9" in ftxt)
check("waited note", "waited 12s" in ftxt)
check("terminal_reason=completed stays silent", "terminal_reason" not in ftxt)
plain = server._fmt_send_result(SendResult(name="swt", response="x", session_id=SID,
                                           model_used="claude-opus-5", cost_usd=0.0, duration_ms=1))
check("plain reply has none of the new lines", "⏳" not in plain and "⏮" not in plain and "waited" not in plain)

print("\n=== 14. wait.py resolves the name to the latest task and labels self-woken ===")
wait_py = Path(_TMP.name) / "pairs" / "wait.py"
check("wait.py installed under CLAUDE_HOME", wait_py.exists())
if wait_py.exists():
    # latest task for 'swt' is rt3's ABANDONED (failed) one → exit 1, with the self-woken note on stderr
    pr = subprocess.run([sys.executable, str(wait_py), "swt", "--timeout", "3"],
                        capture_output=True, text=True, env=dict(os.environ, CLAUDE_HOME=_TMP.name))
    check("exit 1 (failed task)", pr.returncode == 1)
    check("stderr labels SELF-WOKEN", "SELF-WOKEN" in pr.stderr)
    check("stderr carries ABANDONED error", "ABANDONED" in pr.stderr)

print("\n=== 15. wait_for_task drops the event for 'stopped' too ===")
async_tasks._get_or_create_event(tid2[0])
async_tasks.wait_for_task(tid2[0], timeout_s=0.1)
check("event dropped after observing stopped", tid2[0] not in async_tasks._task_events)

print("\n=== 16. v0.12.1: runtime tracks pending background tasks from system events ===")
rt6 = PairRuntime(spec, adapter)
feed(rt6, {"type": "system", "subtype": "task_started", "task_id": "bgA", "task_type": "local_agent",
           "description": "recon"})
feed(rt6, {"type": "system", "subtype": "task_started", "task_id": "bgB", "task_type": "local_bash",
           "description": "sleep"})
check("two pending", sorted(p["task_id"] for p in rt6.pending_background_tasks()) == ["bgA", "bgB"])
feed(rt6, {"type": "system", "subtype": "task_notification", "task_id": "bgA", "status": "completed",
           "summary": "done"})
check("notification clears one", [p["task_id"] for p in rt6.pending_background_tasks()] == ["bgB"])
feed(rt6, {"type": "system", "subtype": "task_updated", "task_id": "bgB", "patch": {"status": "completed"}})
check("task_updated clears the other", rt6.pending_background_tasks() == [])
check("no implicit turn opened by system events", rt6._implicit_scope is None)

print("\n=== 17. v0.12.1: pair_status says so when idle with background work out (disk heuristic) ===")
runtime_mod.registry()._runtimes.pop("swt", None)
bg_send = async_tasks.start_task("swt", "launch recon", lambda tid: SendResult(
    name="swt", response="BG-LAUNCHED", session_id=SID, model_used="claude-opus-5",
    cost_usd=0.01, duration_ms=5, background_launches=["agent"]))
async_tasks.wait_for_task(bg_send.task_id, timeout_s=10)
server._take_self_woken_pending("swt")
kinds, src = server._background_pending("swt")
check("disk heuristic sees the pending launch", kinds == ["agent"] and src == "disk")
st17 = tool(server.pair_status)("swt")
check("pair_status mentions the background work", "launched background work" in st17)

print("\n=== 18. v0.12.1: by-name pair_poll waits for the wake-up task ===")
t0 = datetime.utcnow()
out18a = tool(server.pair_poll)("swt", wait_seconds=2)
check("nothing-yet path waited ~2s", (datetime.utcnow() - t0).total_seconds() >= 1.5)
check("nothing-yet note", "nothing yet" in out18a and "BG-LAUNCHED" in out18a)
import threading as _th  # noqa: E402


def _wake():
    t = async_tasks.register_external_task("swt", async_tasks.SELF_WOKEN_MESSAGE)
    async_tasks.finalize_external_task(t, status="done", result=SendResult(
        name="swt", response="WOKE: synthesized", session_id=SID, model_used="claude-opus-5",
        cost_usd=0.2, duration_ms=7))


_th.Timer(1.5, _wake).start()
t0 = datetime.utcnow()
out18b = tool(server.pair_poll)("swt", wait_seconds=8)
el = (datetime.utcnow() - t0).total_seconds()
check("returned once the wake-up landed (not the full window)", 1.0 <= el < 7.0)
check("shows the self-woken reply", "WOKE: synthesized" in out18b and "SELF-WOKEN" in out18b)
check("wake note present", "waited" in out18b and "wake-up" in out18b)
check("explicit-id poll is unchanged (no wake wait)",
      "BG-LAUNCHED" in tool(server.pair_poll)(bg_send.task_id, wait_seconds=2))

print(f"\n{passed} passed, {failed} failed")
try:
    runtime_mod.registry()._runtimes.pop("swt", None)
except Exception:
    pass
sys.exit(1 if failed else 0)
