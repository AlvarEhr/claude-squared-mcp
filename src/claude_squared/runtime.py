"""Long-running stream-json subprocess per pair.

Avoids the ~3s cold-start + --resume overhead on every send by keeping a single
`claude --print --input-format stream-json` process alive across turns. The first
send to a pair pays the resume cost once; subsequent sends are just stdin/stdout.

Lifecycle:
- Lazy spawn on first use
- Idle eviction after IDLE_TIMEOUT_SECONDS (default 10 minutes)
- Persistent pairs (spec.persistent=True) are exempt from eviction
- atexit graceful shutdown

Logging layout (per pair):
- ~/.claude/pairs/logs/<pair_name>/main.log     ← pair's own thinking/tool_use/text
- ~/.claude/pairs/logs/<pair_name>/subagent-N-<type>.log  ← extracted post-completion

Sub-agent extraction is one-shot at tool_result time (NOT live tail) to avoid
spawning a thread per Agent invocation.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from claude_squared.cli_paths import encode_cwd_for_project as _encode_cwd_for_project
from claude_squared.errors import CLIError, CommandTimeout
from claude_squared import async_tasks as _async_tasks
from claude_squared.models import AsyncTaskState, PairSpec
from claude_squared.registry import claude_home, logs_dir
from claude_squared.registry import get_pair as _reg_get_pair, update_pair as _reg_update_pair

if TYPE_CHECKING:
    from claude_squared.adapters.claude import ClaudeAdapter


IDLE_TIMEOUT_SECONDS = 600  # 10 minutes
EVICTOR_INTERVAL_SECONDS = 60
INIT_TIMEOUT_SECONDS = 30

# v0.9.8: error message prefix used when the pair runtime subprocess
# (claude.exe) exits mid-turn. Parallel to async_tasks.ORPHAN_ERROR_PREFIX
# (which fires when the OWNING MCP server dies). wait.py recognizes both
# prefixes and exits with distinct codes (4=orphan, 6=crash). The constant
# is duplicated in async_tasks.py and _wait_script.py rather than centralized
# in models.py so wait.py — which is stdlib-only by design — can stay
# self-contained. Any change must be made in all three places in sync.
CRASHED_ERROR_PREFIX = "CRASHED: "


def _claude_executable() -> str:
    env = os.environ.get("CLAUDE_PAIR_CLI_PATH")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "claude.exe",
              Path.home() / ".local" / "bin" / "claude"):
        if c.exists():
            return str(c)
    return "claude"


# ---------- Event-to-text formatting (shared between main + sub-agent logs) ----------

class ToolCounter:
    """Per-log tool_use counter that assigns sequential T-N IDs and persists the mapping.

    Caller calls `next_id_for(tool_use_id, tool_name)` when a tool_use is observed →
    returns 'T-N' AND appends to `<index_path>` so pair_tool_detail can resolve the
    canonical tool_use_id later (robust against the JSONL not containing some live
    events — e.g. cancelled/failed sub-agent calls or retries).
    Caller calls `id_for(tool_use_id)` when the matching tool_result arrives →
    returns 'T-N' (look-up only; doesn't bump counter).

    Index file format (JSON dict):
        {"T-1": {"tool_use_id": "toolu_xxx", "tool_name": "Read", "ts": "10:39:01"}, ...}
    """

    def __init__(self, index_path: Path | None = None) -> None:
        self.counter = 0
        self.id_map: dict[str, int] = {}  # tool_use_id -> N
        self.index_path = index_path
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Restore counter + id_map from the index file (no-op if no path or no file)."""
        if self.index_path is None or not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for k, v in data.items():
                if k.startswith("T-") and isinstance(v, dict):
                    n = int(k[2:])
                    self.counter = max(self.counter, n)
                    tu_id = v.get("tool_use_id")
                    if tu_id:
                        self.id_map[tu_id] = n
        except Exception:
            pass

    def reload(self) -> None:
        """Re-read the index from disk and reset in-memory state to match.

        Critical for cross-process correctness: when MCP subprocess A's runtime
        already wrote T-N entries while subprocess B's runtime had a stale
        in-memory counter, B must resync before assigning new T-N or it'll
        collide with A's tags. Call at the start of every send under lock.
        """
        # Reset before reload so deletions (rare but possible) propagate.
        self.counter = 0
        self.id_map = {}
        self._load_from_disk()

    def next_id_for(self, tool_use_id: str, tool_name: str = "?") -> str:
        self.counter += 1
        n = self.counter
        if tool_use_id:
            self.id_map[tool_use_id] = n
        if self.index_path is not None:
            self._persist(n, tool_use_id, tool_name)
        return f"T-{n}"

    def id_for(self, tool_use_id: str) -> str:
        n = self.id_map.get(tool_use_id)
        return f"T-{n}" if n is not None else "T-?"

    def _persist(self, n: int, tool_use_id: str, tool_name: str) -> None:
        try:
            data: dict = {}
            if self.index_path.exists():
                try:
                    data = json.loads(self.index_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[f"T-{n}"] = {
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "ts": datetime.now().strftime("%H:%M:%S"),
            }
            tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception:
            pass


def lookup_tool_in_index(index_path: Path, tool_id: str) -> dict | None:
    """Resolve 'T-N' (or '12') from the index file. Returns the metadata dict or None."""
    if not index_path.exists():
        return None
    n_str = tool_id[2:] if tool_id.upper().startswith("T-") else tool_id
    key = f"T-{n_str}"
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            entry = data.get(key)
            if isinstance(entry, dict):
                return entry
    except Exception:
        return None
    return None


def find_tool_use_in_jsonl(jsonl_path: Path, tool_use_id: str) -> tuple[dict | None, dict | None, bool]:
    """Walk a session JSONL looking for the tool_use with the given id and its tool_result.

    Returns (tool_use_block, tool_result_content_block, is_error).
    Either may be None if not found.
    """
    tool_use: dict | None = None
    tool_result_block: dict | None = None
    if not jsonl_path.exists():
        return None, None, False
    for raw in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if tool_use is None and ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("id") == tool_use_id):
                    tool_use = block
                    break
        if tool_result_block is None and ev.get("type") == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_result"
                            and block.get("tool_use_id") == tool_use_id):
                        tool_result_block = block
                        break
        if tool_use is not None and tool_result_block is not None:
            break
    is_err = bool(tool_result_block.get("is_error")) if tool_result_block else False
    return tool_use, tool_result_block, is_err


def format_event(ev: dict, *, label_prefix: str = "",
                 tool_counter: ToolCounter | None = None,
                 skip_tool_names: set[str] | None = None,
                 skip_user_tool_results: bool = False) -> list[str]:
    """Convert a stream-json or session-JSONL event into formatted log lines.

    `label_prefix`: optional string prepended to non-tool labels (e.g. "sub-").
    `tool_counter`: per-log counter; when provided, tool_use lines get [T-N] tags
        and matching tool_result lines reuse the same N. When None, no T-N tags.
    `skip_tool_names`: set of tool names to skip in tool_use blocks (caller will
        handle them, e.g. for Agent special-casing in main.log).
    `skip_user_tool_results`: if True, skip tool_result blocks in user events
        (caller will handle them).
    Returns a list of full log lines (each already includes timestamp).
    """
    skip_tool_names = skip_tool_names or set()
    ts = datetime.now().strftime("%H:%M:%S")
    out: list[str] = []
    t = ev.get("type")
    msg = ev.get("message") or {}
    if t == "system":
        sub = ev.get("subtype")
        if sub == "init":
            tools_n = len(ev.get("tools") or [])
            out.append(f"[{ts}] === SESSION INIT (model={ev.get('model')}, tools={tools_n}) ===")
        elif sub == "post_turn_summary":
            detail = (ev.get("status_detail") or "").strip()
            if detail:
                out.append(f"[{ts}] [{label_prefix}summary] {detail[:200]}")
        elif sub == "compact_boundary":
            meta = ev.get("compact_metadata", {})
            out.append(f"[{ts}] === COMPACTED {meta.get('pre_tokens', '?')} -> "
                       f"{meta.get('post_tokens', '?')} tokens ({meta.get('trigger', '?')}) ===")
        elif sub == "status":
            status = ev.get("status")
            if status:
                out.append(f"[{ts}] [{label_prefix}status] {status}")
    elif t == "assistant":
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    out.append(f"[{ts}] [{label_prefix}text] {txt[:400]}")
            elif bt == "thinking":
                th = (block.get("thinking") or "").strip()
                if th:
                    out.append(f"[{ts}] [{label_prefix}thinking] {th[:300]}")
            elif bt == "tool_use":
                tool = block.get("name") or "?"
                if tool in skip_tool_names:
                    continue
                inp = block.get("input") or {}
                inp_preview = json.dumps(inp, default=str)[:120]
                if tool_counter is not None:
                    tag = tool_counter.next_id_for(block.get("id") or "", tool_name=tool)
                    out.append(f"[{ts}] [{tag}] [{label_prefix}tool_use] {tool}({inp_preview})")
                else:
                    out.append(f"[{ts}] [{label_prefix}tool_use] {tool}({inp_preview})")
    elif t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            out.append(f"[{ts}] [{label_prefix}user] {content[:400]}")
        elif isinstance(content, list) and not skip_user_tool_results:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    res = block.get("content")
                    preview = (str(res) if not isinstance(res, list)
                               else json.dumps(res, default=str))[:120]
                    is_err = block.get("is_error")
                    err_tag = " ERR" if is_err else ""
                    if tool_counter is not None:
                        tag = tool_counter.id_for(block.get("tool_use_id") or "")
                        out.append(f"[{ts}] [{tag}] [{label_prefix}tool_result{err_tag}] {preview}")
                    else:
                        out.append(f"[{ts}] [{label_prefix}tool_result{err_tag}] {preview}")
    elif t == "result":
        sub = ev.get("subtype")
        if sub:
            dur = ev.get("duration_ms", 0)
            out.append(f"[{ts}] === TURN {sub.upper()} ({dur}ms) ===")
    return out


def extract_subagent_jsonl_to_log(
    jsonl_path: Path,
    log_path: Path,
    label_prefix: str = "",
    index_path: Path | None = None,
) -> int:
    """Parse a sub-agent's session JSONL one-shot and append to a log file.

    Each sub-agent log has its own T-N counter (separate from main.log's). The optional
    `index_path` persists the T-N → tool_use_id mapping for later pair_tool_detail lookups.
    Returns number of lines written.
    """
    if not jsonl_path.exists():
        return 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    counter = ToolCounter(index_path=index_path)
    written = 0
    with open(log_path, "a", encoding="utf-8") as out:
        for raw in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            for line in format_event(ev, label_prefix=label_prefix, tool_counter=counter):
                out.write(line + "\n")
                written += 1
    return written


def _wait_for_file_stable(path: Path, max_wait_s: float = 10.0, stable_s: float = 1.5,
                          poll_interval_s: float = 0.25) -> None:
    """Block until `path`'s size+mtime hasn't changed for `stable_s`, or `max_wait_s` total."""
    deadline = time.monotonic() + max_wait_s
    last_sig: tuple | None = None
    last_change_at = time.monotonic()
    while time.monotonic() < deadline:
        try:
            st = path.stat()
            sig = (st.st_size, int(st.st_mtime * 1000))
        except OSError:
            sig = None
        now = time.monotonic()
        if sig != last_sig:
            last_sig = sig
            last_change_at = now
        elif now - last_change_at >= stable_s:
            return
        time.sleep(poll_interval_s)


def find_subagent_jsonls_after(
    parent_cwd: str | None,
    parent_session_id: str,
    after_ts: float,
    seen: set[str],
) -> list[Path]:
    """Find sub-agent JSONL files for the parent session that appeared/grew after a timestamp.

    Looks in `~/.claude/projects/<encoded-cwd>/<parent_session_id>/subagents/`.
    Returns sorted list of paths, excluding any names already in `seen`.
    """
    cwd = Path(parent_cwd) if parent_cwd else Path.cwd()
    encoded = _encode_cwd_for_project(cwd)
    sub_dir = claude_home() / "projects" / encoded / parent_session_id / "subagents"
    if not sub_dir.exists():
        return []
    fresh: list[Path] = []
    for p in sub_dir.glob("agent-*.jsonl"):
        if p.name in seen:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= after_ts:
            fresh.append(p)
    return sorted(fresh, key=lambda p: p.stat().st_mtime)


class TurnLogScope:
    """Tracks which lines in main.log a single turn produced + the sub-agent logs it created."""

    def __init__(self, main_log_path: Path, start_line: int):
        self.main_log_path = main_log_path
        self.start_line = start_line
        self.end_line = start_line
        self.subagent_logs: list[str] = []  # paths to sub-agent log files this turn produced
        # v0.12.0: background launches observed in this turn ("agent" / "bash" /
        # "workflow") — the turn may end before they finish (placeholder reply).
        self.background_launches: list[str] = []
        # v0.12.0: reader-side snapshot taken the instant this turn's result
        # event was attributed (exact end_line). send() prefers it over the
        # live fields, which a self-woken continuation may already have moved.
        self.result_snapshot: dict[str, Any] | None = None


class PairRuntime:
    """A single long-running stream-json subprocess for a specific pair."""

    def __init__(self, spec: PairSpec, adapter: "ClaudeAdapter"):
        self.spec = spec
        self.adapter = adapter
        self.proc: subprocess.Popen | None = None
        self.last_activity: datetime = datetime.utcnow()
        self.send_lock = threading.Lock()
        self.started_at: datetime | None = None
        self._stdout_q: queue.Queue[str] = queue.Queue()
        self._stderr_buf: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._init_event: dict | None = None

        # JSONL stale-mtime tracking — used to detect when ANOTHER process wrote
        # to the underlying session JSONL while this runtime was warm but idle.
        # Without this, a warm subprocess holds a stale in-memory view of the
        # session and would write a turn against memory that doesn't reflect the
        # other writer's appended turn → JSONL turn-lineage divergence.
        self._jsonl_path: Path = self._compute_jsonl_path(spec)
        self._last_seen_jsonl_mtime: float | None = None

        # Per-pair log folder layout
        self.log_dir: Path = logs_dir() / spec.name
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.main_log_path: Path = self.log_dir / "main.log"

        # Total lines written to main.log so far (for line-range tracking)
        self._main_log_lines = self._count_existing_lines(self.main_log_path)
        self._main_log_lock = threading.Lock()
        # When was the last log line written? Fed to ``pair_status`` so an
        # observing agent can tell "actively working" (recent activity) vs
        # "potentially hung" (no log activity for minutes despite alive runtime).
        self._last_log_activity_at: datetime | None = None

        # Sub-agent tracking. v0.12.0: the counter resumes from the highest
        # existing subagent-N-*.log in this pair's log dir, so a respawn (idle
        # eviction, pair_update, stale-JSONL respawn) no longer restarts the
        # numbering at 1 and appends a second "#1" onto an older file.
        self._subagent_counter = 0
        try:
            for _p in self.log_dir.glob("subagent-*-*.log"):
                _n = _p.name.split("-", 2)[1]
                if _n.isdigit():
                    self._subagent_counter = max(self._subagent_counter, int(_n))
        except Exception:
            pass
        # tool_use_id -> {"n": int, "type": str, "started_ts": float, "description": str}
        self._pending_subagents: dict[str, dict] = {}
        self._subagent_seen_jsonls: set[str] = set()

        # Tool-use ID counter for main.log (each tool_use gets a sequential T-N tag).
        # Persisted to <log_dir>/main.idx.json so pair_tool_detail can resolve T-N → tool_use_id
        # robustly across MCP restarts and even when the JSONL doesn't contain a live event
        # (e.g. cancelled/failed sub-agents).
        self._main_index_path = self.log_dir / "main.idx.json"
        self._main_tool_counter = ToolCounter(index_path=self._main_index_path)

        # Per-turn scope (set on send entry; updated as events arrive; consumed at result)
        self._current_scope: TurnLogScope | None = None

        # v0.11.0 "newer model available" drift check: gate the parent-JSONL read
        # to ONCE per runtime spawn (first send after a cold start), not per send.
        # The server's send runner flips this true after checking; a respawn (new
        # PairRuntime) resets it. Anti-nag dedup across spawns lives in the
        # persisted spec.last_drift_notice, not here.
        self._drift_checked: bool = False

        # v0.12.0 self-woken turns. The pair can resume ON ITS OWN after
        # background work it launched (Agent run_in_background / Bash
        # run_in_background / Workflow) completes — Claude Code's
        # notify-and-resume fires because this persistent runtime keeps stdin
        # open. Those turns have no send() behind them; the reader thread
        # tracks them as an "implicit" scope + an on-disk async task so every
        # consumer (pair_status, pair_poll, wait.py, pair_stop, orphan reaping,
        # other MCP processes) sees them unchanged.
        #
        # ``_scope_lock`` guards ONLY the pointer flips of _current_scope /
        # _implicit_scope / _implicit_task / _completed_since_send. It is never
        # held across I/O: stop() runs under the global RuntimeRegistry lock and
        # must not stall every pair behind a reader doing file writes. Lock
        # order where both are needed: _scope_lock → _main_log_lock, never the
        # reverse.
        self._scope_lock = threading.Lock()
        self._implicit_scope: TurnLogScope | None = None
        self._implicit_task: AsyncTaskState | None = None
        self._implicit_done = threading.Event()
        self._implicit_done.set()  # set == no self-woken turn in progress
        # Completed self-woken turns are NOT kept here — they're parked on the
        # spec (PairSpec.self_woken_pending) via _record_self_woken, because the
        # next send respawns this runtime (see is_stale) and the records must
        # survive that, and because another MCP process should see them too.
        # Bumped by the reader on EVERY result event (solicited or not) — the
        # ack signal send_interrupt waits for. Log growth alone is ambiguous
        # during a self-woken turn, which produces log lines continuously.
        self._result_seq: int = 0
        # v0.12.1: background tasks the CLI has told us about and not yet
        # reported finished (task_started → task_notification/task_updated).
        # task_id → {kind, description, started_at}. Lets pair_status say
        # "idle, but N background tasks may still be running" and lets a
        # by-name pair_poll wait for the wake-up. Guarded by _scope_lock.
        self._bg_tasks: dict[str, dict[str, Any]] = {}

    def pending_background_tasks(self) -> list[dict[str, Any]]:
        """Background tasks started and not yet reported finished (v0.12.1)."""
        with self._scope_lock:
            return [dict(v, task_id=k) for k, v in self._bg_tasks.items()]

    @staticmethod
    def _count_existing_lines(p: Path) -> int:
        if not p.exists():
            return 0
        try:
            with open(p, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    @staticmethod
    def _compute_jsonl_path(spec: PairSpec) -> Path:
        """Mirror ClaudeAdapter's path computation so the runtime knows which
        file to mtime-check for cross-process write detection."""
        cwd = Path(spec.cwd) if spec.cwd else Path.cwd()
        encoded = _encode_cwd_for_project(cwd)
        return claude_home() / "projects" / encoded / f"{spec.session_id}.jsonl"

    def _current_jsonl_mtime(self) -> float | None:
        """Return current mtime of the session JSONL, or None if missing."""
        try:
            return self._jsonl_path.stat().st_mtime
        except OSError:
            return None

    def is_stale(self) -> bool:
        """True if the session JSONL has been written by another process since
        our last successful send. Triggers eviction + respawn so the next send
        starts from a fresh ``--resume`` that reads the up-to-date file.

        Returns False on the first send (no prior mtime to compare against) and
        when the file is missing (degenerate; let normal flow surface the issue).
        """
        # v0.12.0: a self-woken turn IN PROGRESS is writing the JSONL right now
        # — that's us, not another process. Pre-v0.12.0 this returned True
        # here, the adapter evicted, and evict's tree-kill (/T) took the pair's
        # in-flight background work with it. The adapter now waits for the turn
        # BEFORE calling this (wait_for_implicit_idle); this guard is the belt.
        #
        # The watermark IS refreshed when a self-woken turn finalizes, so the
        # warm runtime survives (a respawn would tree-kill background
        # sub-agents still running). That would swallow a coexisting MCP
        # process's write made DURING the turn — so the server makes that
        # impossible instead: another process's send waits for our on-disk
        # self-woken task before it spawns on the JSONL
        # (server._wait_for_foreign_self_woken). The sequential case this
        # check exists for (idle runtime, other process writes, we resume
        # later) is unchanged. Historian-caught, 2026-08-22.
        if self._implicit_scope is not None:
            return False
        if self._last_seen_jsonl_mtime is None:
            return False
        cur = self._current_jsonl_mtime()
        if cur is None:
            return False
        # Use a small epsilon to tolerate filesystem mtime resolution (Windows ~10ms,
        # most Linux ~1ns, macOS HFS+ ~1s).
        return cur > self._last_seen_jsonl_mtime + 0.001

    # ---- lifecycle ----

    def start(self) -> None:
        cli = _claude_executable()
        args = [cli] + self.adapter._common_create_args(self.spec)  # noqa: SLF001
        args += [
            "--print", "--verbose",
            "--resume", self.spec.session_id,
            "--model", self.spec.model,
        ]
        # Skip --effort for haiku (no effort knob) or any spec where effort
        # was coerced to None for the model's capability.
        if self.spec.effort is not None:
            args += ["--effort", self.spec.effort]
        args += [
            "--permission-mode", self.spec.permission_mode,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]
        # Spawn in a NEW process group/session so we can send Ctrl+C / SIGINT
        # for the soft-stop path without affecting our own MCP process. Without
        # this flag, GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0) would target the
        # console group we're in.
        popen_kwargs: dict = {
            "cwd": self.spec.cwd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(args, **popen_kwargs)
        self.started_at = datetime.utcnow()
        self._reader_thread = threading.Thread(target=self._stdout_reader, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self._stderr_thread.start()
        # Seed an init banner manually so users tailing know a fresh runtime started
        self._append_main_log_line(
            f"[{datetime.now().strftime('%H:%M:%S')}] === RUNTIME START "
            f"(pair={self.spec.name}, model={self.spec.model}, cwd={self.spec.cwd}) ==="
        )

    def _stdout_reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace")
                # v0.12.0: attribute/log FIRST, queue SECOND. Otherwise send()
                # can pull a result off the queue, return, and clear
                # _current_scope before the reader has attributed that same
                # line — manufacturing a phantom "self-woken" completion.
                # Processing first means every queued line is already
                # attributed to the scope that was open when it arrived.
                try:
                    self._on_event_for_log(line)
                except Exception:
                    pass
                # Only a SOLICITED turn has a consumer for the queue. Lines
                # arriving with no send() scope open belong to self-woken turns
                # (already logged + tracked above); queueing them would grow
                # memory without bound on a pair that wakes itself for hours,
                # and send() would only drain them at entry anyway.
                with self._scope_lock:
                    wanted = self._current_scope is not None
                if wanted:
                    self._stdout_q.put(line)
            except Exception:
                pass

    def _stderr_reader(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            try:
                with self._stderr_lock:
                    if len(self._stderr_buf) >= 200:
                        self._stderr_buf.pop(0)
                    self._stderr_buf.append(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send_interrupt(self, *, wait_for_result_seconds: float = 2.5) -> bool:
        """Send the claude CLI's in-band ``control_request: interrupt`` over stdin.

        This is the equivalent of pressing the UI stop button: the CLI cancels the
        current turn (any in-flight tool_use, e.g. a Bash sleep), emits a result
        event with ``subtype: "error_during_execution"``, AND **stays alive** for
        the next send. No process kill, no orphan children — claude.exe cleans up
        its own tool subprocesses (verified empirically with a python sleep(30)
        Bash task: the orphan disappeared the moment interrupt was acknowledged).

        Wire format (from claude-agent-sdk-python):
            {"type": "control_request", "request_id": "req_<n>_<hex>",
             "request": {"subtype": "interrupt"}}

        Returns True if the CLI emitted a ``result`` event within
        ``wait_for_result_seconds`` of the request (interrupt acknowledged).
        Returns False if no result event arrived in time — caller should escalate
        to ``stop()`` for a tree-kill.

        Must be called WHILE the runtime is alive and (typically) mid-turn.
        Caller is responsible for any per-pair locking; we serialize on
        ``self.send_lock`` so we don't race with an in-flight ``send()``.
        """
        if not self.is_alive() or self.proc is None or self.proc.stdin is None:
            return False
        request_id = f"req_int_{int(time.time())}_{os.urandom(3).hex()}"
        payload = json.dumps({
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "interrupt"},
        }) + "\n"
        try:
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return False
        # Wait for a RESULT event (any subtype — typically error_during_execution).
        # v0.12.0: the reader bumps ``_result_seq`` on every result, solicited
        # or self-woken, so we don't have to fight send() for the stdout queue.
        # The pre-v0.12.0 heuristic — "main.log grew by a line" — returned True
        # unconditionally during a self-woken turn (which logs continuously),
        # so pair_stop reported "interrupt acked" even when the CLI dropped it.
        deadline = time.monotonic() + wait_for_result_seconds
        seq0 = self._result_seq
        while time.monotonic() < deadline:
            if not self.is_alive():
                # Subprocess died — interrupt may have triggered crash, not graceful
                return False
            if self._result_seq > seq0:
                # The ack result arrived; let it settle one tick so the turn
                # marker / task finalization land before the caller reports.
                time.sleep(0.3)
                return True
            time.sleep(0.1)
        # Timed out waiting for a result — interrupt may not have been received
        # OR the runtime is in a non-responsive state (true wedge).
        return False

    def stop(self) -> str:
        """Tear down the runtime subprocess. Tree-kills descendants so the pair's
        Bash tool subprocesses (which would otherwise orphan when claude.exe dies)
        get cleaned up too.

        This is the HARD termination path, used by RuntimeRegistry.evict and by
        ``pair_stop(force=True)``. For the soft, UI-stop-button-equivalent path
        that keeps the runtime alive, use ``send_interrupt()`` instead.

        Returns a short outcome string for telemetry.
        """
        # v0.12.0: finalize any self-woken turn FIRST — after the tree-kill no
        # result event will ever arrive to close it, and its task would sit
        # "running" forever (owner alive → orphan reaping never touches it).
        # Before the proc check on purpose: a runtime whose proc already died
        # can still hold an open implicit scope.
        self._abort_implicit_turn(
            "runtime stopped/evicted while a self-woken turn was in progress — "
            "its background work was tree-killed with it"
        )
        if self.proc is None:
            return "no runtime"
        pid = self.proc.pid
        # Polite stdin close — gives claude.exe an EOF signal before we tree-kill.
        # In practice claude.exe won't shut down on EOF mid-turn (it's writing,
        # not reading), but this is harmless and may let it flush state.
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        outcome = "tree-killed"
        if os.name == "nt":
            try:
                # /F = force, /T = tree. Critical: the pair's Bash tool spawns
                # multiple wrapper shells + child commands; without /T those
                # orphan when claude.exe is TerminateProcess'd alone. Empirically
                # observed: a python time.sleep(300) survived a `Stop-Process
                # -Force` on its claude.exe parent. /T prevents this.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10, check=False,
                )
            except Exception:
                try:
                    self.proc.kill()
                    outcome = "fallback-kill"
                except Exception:
                    outcome = "kill-failed"
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.proc.kill()
                    outcome = "fallback-kill"
                except Exception:
                    outcome = "kill-failed"
        try:
            self.proc.wait(timeout=3)
        except Exception:
            pass
        self.proc = None
        return outcome

    # ---- log writing ----

    def _append_main_log_line(self, line: str) -> None:
        """Write one line to main.log and increment the line counter (thread-safe).

        Stamps BOTH timestamps:
          - ``_last_log_activity_at`` — used by ``pair_status``'s active/slow/
            likely-hung heuristic.
          - ``last_activity`` — used by the idle evictor AND ``pair_runtimes()``
            display. v0.9.8: bumping per-log-line — not just at send entry /
            result — is defense-in-depth for the mid-turn eviction fix
            (Crack 3 from the v0.9.8 design pass). The primary protection is
            ``_evict_idle``'s ``_current_scope`` skip, but if the scope is ever
            stale for any reason (a leaked turn, an unhandled exception in
            send before the try/finally cleanup), a runtime that's still
            producing log lines is observably alive and shouldn't be killed.
            Side benefit: ``pair_runtimes`` now reports an accurate "last
            activity" mid-turn instead of a stale send-entry timestamp.
        """
        with self._main_log_lock:
            try:
                with open(self.main_log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._main_log_lines += 1
                now = datetime.utcnow()
                self._last_log_activity_at = now
                self.last_activity = now
                if self._current_scope is not None:
                    self._current_scope.end_line = self._main_log_lines
                # v0.12.0: a self-woken turn's scope tracks its range the same way.
                if self._implicit_scope is not None:
                    self._implicit_scope.end_line = self._main_log_lines
            except Exception:
                pass

    def _on_event_for_log(self, raw_line: str) -> None:
        """Parse a stream-json line; write user-friendly lines to main.log; manage sub-agents."""
        line = raw_line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except Exception:
            return

        # v0.12.0 self-woken turn tracking. Only the PAIR's OWN events count:
        # events produced inside a sub-agent stream through with
        # parent_tool_use_id set (verified CLI 2.1.224) and must NOT open a
        # turn — they arrive between turns constantly and have no result of
        # their own (→ a phantom task that never closes, forever in-flight).
        # system.init is deliberately NOT part of the predicate: a self-woken
        # continuation doesn't necessarily start with one (par6 log: the parent
        # kept working for 10 min after a result with no init in between).
        ev_type = ev.get("type")
        if ev_type in ("assistant", "user") and not ev.get("parent_tool_use_id"):
            with self._scope_lock:
                needs_open = self._current_scope is None and self._implicit_scope is None
            if needs_open:
                self._open_implicit_turn()
        elif ev_type == "system" and ev.get("subtype") in (
            "task_started", "task_notification", "task_updated",
        ):
            # Structured background-task lifecycle events (CLI 2.1.224). Logged
            # so main.log shows WHY a pair woke up, and tracked so pair_status
            # knows what's still out. Best-effort field reads — the shape isn't
            # contractual.
            self._log_task_lifecycle_event(ev)

        # Detect sub-agent SPAWN (assistant.tool_use of Agent) — special-cased for the
        # bookend label + log extraction. The T-N tag still gets assigned (so main.log
        # carries the unified ID for all tool uses including Agent).
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent":
                    tool_use_id = block.get("id") or ""
                    inp = block.get("input") or {}
                    self._subagent_counter += 1
                    n = self._subagent_counter
                    sub_type = (inp.get("subagent_type") or "general-purpose")
                    desc = (inp.get("description") or "")[:200]
                    self._pending_subagents[tool_use_id] = {
                        "n": n,
                        "type": sub_type,
                        "started_ts": time.time(),
                        "description": desc,
                    }
                    sub_log_path = self.log_dir / f"subagent-{n}-{sub_type}.log"
                    if self._current_scope is not None:
                        self._current_scope.subagent_logs.append(str(sub_log_path))
                    ts = datetime.now().strftime("%H:%M:%S")
                    # Reserve a T-N for the Agent invocation itself (so it has the same
                    # tag in main.log as any other tool_use). Persisted to the index file.
                    tag = self._main_tool_counter.next_id_for(tool_use_id, tool_name="Agent")
                    self._append_main_log_line(
                        f"[{ts}] [{tag}] [-> sub-agent #{n}: {sub_type}] launching: {desc}"
                    )
                    self._append_main_log_line(
                        f"           live log will be written to: {sub_log_path}"
                    )

        # Detect sub-agent RETURN (user.tool_result for an Agent tool_use_id)
        if ev.get("type") == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    use_id = block.get("tool_use_id") or ""
                    if use_id in self._pending_subagents:
                        info = self._pending_subagents.pop(use_id)
                        n = info["n"]
                        sub_type = info["type"]
                        started_ts = info["started_ts"]
                        res = block.get("content")
                        preview = (str(res) if not isinstance(res, list)
                                   else json.dumps(res, default=str))[:200]
                        ts = datetime.now().strftime("%H:%M:%S")
                        tag = self._main_tool_counter.id_for(use_id)
                        self._append_main_log_line(
                            f"[{ts}] [{tag}] [<- sub-agent #{n} returned] {preview}"
                        )
                        self._note_background_launch(res, tag)
                        try:
                            self._extract_subagent_log(n, sub_type, started_ts)
                        except Exception as e:
                            self._append_main_log_line(
                                f"           (sub-agent log extraction failed: {e})"
                            )
                        continue
                    # Non-Agent tool_result: tag with T-N if we have it
                    res = block.get("content")
                    preview = (str(res) if not isinstance(res, list)
                               else json.dumps(res, default=str))[:120]
                    is_err = block.get("is_error")
                    err_tag = " ERR" if is_err else ""
                    ts = datetime.now().strftime("%H:%M:%S")
                    tag = self._main_tool_counter.id_for(use_id)
                    self._append_main_log_line(
                        f"[{ts}] [{tag}] [tool_result{err_tag}] {preview}"
                    )
                    self._note_background_launch(res, tag)
                return

        # Default formatting for everything else (Agent tool_uses + their tool_results
        # are handled above; tell format_event to skip them so we don't double-log).
        for line_out in format_event(
            ev, tool_counter=self._main_tool_counter,
            skip_tool_names={"Agent"}, skip_user_tool_results=True,
        ):
            self._append_main_log_line(line_out)

        # v0.12.0: attribute the result AFTER its TURN marker has been written,
        # so a self-woken turn's recorded log range includes the marker.
        if ev_type == "result":
            self._after_result(ev)

    def _extract_subagent_log(self, n: int, sub_type: str, started_ts: float) -> None:
        """Find the sub-agent's JSONL and schedule deferred extraction.

        Deferred (background thread, polling for write-stability) handles the race where
        the parent's tool_result event arrives before the sub-agent's JSONL is fully
        flushed to disk. We wait for the file's mtime to stop changing, then extract.
        """
        candidates = find_subagent_jsonls_after(
            self.spec.cwd, self.spec.session_id, started_ts - 1.0,
            self._subagent_seen_jsonls,
        )
        if not candidates:
            return
        jsonl = candidates[-1]
        self._subagent_seen_jsonls.add(jsonl.name)
        sub_log_path = self.log_dir / f"subagent-{n}-{sub_type}.log"

        # Write the header immediately so users tailing see the sub-agent invocation appeared
        try:
            with open(sub_log_path, "a", encoding="utf-8") as f:
                f.write(f"=== sub-agent #{n} ({sub_type}) ===\n")
                f.write(f"=== source jsonl: {jsonl} ===\n")
        except Exception:
            pass

        # Per-subagent index file: subagent-N-<type>.idx.json
        sub_idx_path = self.log_dir / f"subagent-{n}-{sub_type}.idx.json"

        # Background extraction: poll until the JSONL stabilizes, then dump
        def _bg_extract() -> None:
            try:
                _wait_for_file_stable(jsonl, max_wait_s=10.0, stable_s=1.5)
                extract_subagent_jsonl_to_log(jsonl, sub_log_path,
                                              label_prefix="sub-",
                                              index_path=sub_idx_path)
            except Exception as e:
                try:
                    with open(sub_log_path, "a", encoding="utf-8") as f:
                        f.write(f"=== extraction error: {e} ===\n")
                except Exception:
                    pass

        threading.Thread(target=_bg_extract, daemon=True,
                         name=f"subagent-extract-{self.spec.name}-{n}").start()

    # ---- v0.12.0 self-woken turns ----

    _BG_LAUNCH_MARKERS = (
        # (lowercased tool_result text marker, kind). All three are verbatim
        # CLI-generated tool_result text — the model never writes tool_results,
        # so they can't be paraphrased. Counted across the user's pair logs on
        # 3+ dates: 105 / 2 / 2 occurrences, never varied. Matched on the first
        # 400 chars of the result, lowercased.
        ("async agent launched successfully", "agent"),
        ("command running in background with id", "bash"),
        ("workflow launched in background", "workflow"),
    )

    def _note_background_launch(self, res: Any, tag: str) -> None:
        """Reader thread. Record a background launch on whichever scope is
        open and say so in main.log — this is the turn that will end with a
        placeholder reply."""
        try:
            raw = res if isinstance(res, str) else json.dumps(res, default=str)
        except Exception:
            return
        head = raw[:400].lower()
        kind = next((k for marker, k in self._BG_LAUNCH_MARKERS if marker in head), None)
        if kind is None:
            return
        with self._scope_lock:
            scope = self._current_scope or self._implicit_scope
            if scope is not None:
                scope.background_launches.append(kind)
        ts = datetime.now().strftime("%H:%M:%S")
        self._append_main_log_line(
            f"[{ts}] [{tag}] [background launch: {kind}] runs past the end of this "
            f"turn — the pair will wake itself when it completes"
        )

    _TASK_TERMINAL_STATUSES = ("completed", "failed", "killed", "cancelled", "canceled", "stopped", "error")

    def _log_task_lifecycle_event(self, ev: dict) -> None:
        """Reader thread. One main.log line per background-task lifecycle
        event (CLI 2.1.224 ``system`` sub-events) + pending-task tracking;
        best-effort field reads."""
        ts = datetime.now().strftime("%H:%M:%S")
        full_tid = str(ev.get("task_id") or "")
        tid = (full_tid or "?")[:12]
        sub = ev.get("subtype")
        if sub == "task_started":
            kind = ev.get("task_type") or ev.get("subagent_type") or "task"
            desc = str(ev.get("description") or "")[:120].replace("\n", " ")
            if full_tid:
                with self._scope_lock:
                    self._bg_tasks[full_tid] = {
                        "kind": str(kind), "description": desc,
                        "started_at": datetime.utcnow().isoformat(),
                    }
            self._append_main_log_line(f"[{ts}] [system:task_started] {kind} {tid}: {desc}")
            return
        if sub == "task_updated":
            # Quiet bookkeeping only (no log line — these are frequent).
            patch = ev.get("patch") if isinstance(ev.get("patch"), dict) else {}
            if full_tid and str(patch.get("status") or "").lower() in self._TASK_TERMINAL_STATUSES:
                with self._scope_lock:
                    self._bg_tasks.pop(full_tid, None)
            return
        # task_notification
        status = ev.get("status") or "?"
        if full_tid and str(status).lower() in self._TASK_TERMINAL_STATUSES:
            with self._scope_lock:
                self._bg_tasks.pop(full_tid, None)
        summary = str(ev.get("summary") or "")[:160].replace("\n", " ")
        # No "the pair will now resume" claim here: this fires for blocking
        # sub-agents too. A self-woken turn announces itself with its own
        # SELF-WOKEN marker line.
        self._append_main_log_line(
            f"[{ts}] [system:task_notification] task {tid} {status}"
            f"{': ' + summary if summary else ''}"
        )

    def _open_implicit_turn(self) -> None:
        """Reader thread. Open a self-woken turn: log scope + on-disk async task."""
        with self._scope_lock:
            if self._current_scope is not None or self._implicit_scope is not None:
                return
            with self._main_log_lock:
                start_line = self._main_log_lines + 1
            scope = TurnLogScope(self.main_log_path, start_line)
            self._implicit_scope = scope
            self._implicit_task = None
            self._implicit_done.clear()
        # Registration is file I/O — outside the lock (see __init__ note).
        task: AsyncTaskState | None = None
        try:
            task = _async_tasks.register_external_task(
                self.spec.name, _async_tasks.SELF_WOKEN_MESSAGE,
            )
        except Exception:
            task = None
        with self._scope_lock:
            still_ours = self._implicit_scope is scope
            if still_ours:
                self._implicit_task = task
        if not still_ours:
            # stop()/abort raced us between the pointer flip and registration.
            if task is not None:
                try:
                    _async_tasks.finalize_external_task(
                        task, status="stopped",
                        error="stopped: runtime stopped while the self-woken turn was starting",
                    )
                except Exception:
                    pass
            return
        ts = datetime.now().strftime("%H:%M:%S")
        tid = task.task_id[:8] if task is not None else "untracked"
        # Deliberately does NOT match the ``=== TURN <x> (Nms) ===`` regex that
        # slices turns for pair_poll — this line OPENS a turn, the result's
        # marker closes it.
        self._append_main_log_line(
            f"[{ts}] === SELF-WOKEN TURN (task {tid}) — the pair resumed on its own "
            f"after background work completed; no pair_send behind this turn ==="
        )

    def _record_self_woken(self, record: dict[str, Any]) -> None:
        """Park a completed/aborted self-woken turn on the spec for the next
        send's ⏮ footer. Registry RMW under its filelock; newest 25 kept.
        Never raises (reader thread / stop path)."""
        try:
            cur = _reg_get_pair(self.spec.name)
            pending = list(cur.self_woken_pending or []) + [record]
            _reg_update_pair(self.spec.name, self_woken_pending=pending[-25:])
        except Exception:
            pass

    def _abort_implicit_turn(self, reason: str, *, status: str = "stopped") -> bool:
        """Finalize an open self-woken turn WITHOUT a result event (stop/evict,
        or the reaper). Safe from any thread. Returns True if there was one."""
        with self._scope_lock:
            scope, task = self._implicit_scope, self._implicit_task
            if scope is None:
                return False
            self._implicit_scope = None
            self._implicit_task = None
        if task is not None:
            try:
                _async_tasks.finalize_external_task(task, status=status, error=reason)
            except Exception:
                pass
        self._record_self_woken({
            "task_id": task.task_id if task is not None else None,
            "status": status,
            "log_line_start": scope.start_line,
            "log_line_end": scope.end_line,
            "response_preview": "",
            "cost_usd": 0.0,
            "subtype": None,
            "note": reason,
        })
        self._implicit_done.set()
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self._append_main_log_line(
                f"[{ts}] === SELF-WOKEN TURN {status.upper()}: {reason} ==="
            )
        except Exception:
            pass
        return True

    def _after_result(self, ev: dict) -> None:
        """Reader thread, on EVERY result event. Solicited → snapshot the exact
        log range for send(). Otherwise → close the self-woken turn (or record
        an unattributed completion). Never CREATES state on the result path."""
        self._result_seq += 1
        with self._scope_lock:
            solicited = self._current_scope
            if solicited is not None:
                solicited.result_snapshot = {
                    "end_line": solicited.end_line,
                    "subagent_logs": list(solicited.subagent_logs),
                    "background_launches": list(solicited.background_launches),
                }
                return
            scope, task = self._implicit_scope, self._implicit_task
            self._implicit_scope = None
            self._implicit_task = None
        self._close_implicit_turn(ev, scope, task)

    def _close_implicit_turn(
        self, ev: dict, scope: TurnLogScope | None, task: AsyncTaskState | None,
    ) -> None:
        # The pair wrote its OWN session JSONL during this turn. Refresh the
        # stale-mtime watermark so the next send keeps the warm runtime (its
        # in-memory view IS current — same claude.exe) instead of evicting,
        # which would tree-kill any background sub-agents still running.
        # Sound because another MCP process can't have written meanwhile: its
        # send waits for our on-disk self-woken task first
        # (server._wait_for_foreign_self_woken). See is_stale().
        self._last_seen_jsonl_mtime = self._current_jsonl_mtime()
        self.last_activity = datetime.utcnow()
        cost = float(ev.get("total_cost_usd") or 0.0)
        subtype = ev.get("subtype") or ""
        record: dict[str, Any] = {
            "task_id": task.task_id if task is not None else None,
            # A result with no open scope at all: a turn that produced no
            # top-level content (nothing to finalize). Recorded so the next
            # send's footer still mentions it.
            "status": "unattributed",
            "log_line_start": scope.start_line if scope is not None else self._main_log_lines,
            "log_line_end": scope.end_line if scope is not None else self._main_log_lines,
            "response_preview": str(ev.get("result") or "")[:200],
            "cost_usd": cost,
            "subtype": subtype,
        }
        if scope is not None:
            ev["_log_scope"] = {
                "log_path": str(scope.main_log_path),
                "start_line": scope.start_line,
                "end_line": scope.end_line,
                "subagent_logs": list(scope.subagent_logs),
                "background_launches": list(scope.background_launches),
            }
            result = None
            error: str | None = None
            try:
                result = self.adapter._build_send_result(self.spec, ev)  # noqa: SLF001
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            status = "done"
            if (task is not None and _async_tasks.was_stopped(task.task_id)
                    and subtype == "error_during_execution"):
                # pair_stop interrupted THIS turn (the CLI acked with
                # error_during_execution). A stop marker on a turn that still
                # completed normally (drain_queue racing a fresh wake) stays
                # "done" — the work happened.
                status, error = "stopped", "stopped by pair_stop"
            elif error is not None:
                status = "failed"
            if task is not None:
                try:
                    _async_tasks.finalize_external_task(
                        task, status=status, result=result, error=error,
                    )
                except Exception:
                    pass
            record["status"] = status
            # Registry totals — the server's send runner does this for
            # solicited turns; nobody else will for this one. A self-woken
            # turn can't overlap a solicited turn's read-modify-write (send()
            # queues behind it), so the plain RMW is safe in practice.
            try:
                cur = _reg_get_pair(self.spec.name)
                _reg_update_pair(
                    self.spec.name,
                    turn_count=cur.turn_count + 1,
                    total_cost_usd=cur.total_cost_usd + cost,
                    last_active_at=datetime.utcnow(),
                )
            except Exception:
                pass
        self._record_self_woken(record)
        self._implicit_done.set()

    def wait_for_implicit_idle(
        self, timeout_seconds: int | None, should_stop: "Callable[[], bool] | None",
    ) -> float:
        """Block until no self-woken turn is in progress. Returns seconds waited.

        Called by the adapter BEFORE its stale check (so a self-woken turn is
        never evicted mid-flight) and again at send() entry as the belt. Bounded
        exactly like the read loop: timeout_seconds → CommandTimeout, should_stop
        → in-band interrupt then CLIError once the turn has ended, proc death →
        the implicit task is finalized CRASHED and CLIError is raised.

        Why wait rather than interleave: a message written while the CLI is
        mid self-woken turn is processed AFTER that turn, and its result would
        arrive after the self-woken turn's — send() would return the wrong one
        (the original placeholder handback, inverted).
        """
        t0 = time.monotonic()
        end = (t0 + timeout_seconds) if timeout_seconds is not None else None
        last_stop_check = 0.0
        interrupted = False
        while True:
            with self._scope_lock:
                open_now = self._implicit_scope is not None
            if not open_now:
                if interrupted:
                    raise CLIError(
                        "send cancelled by pair_stop before the message was written",
                        stderr=self._collect_stderr(),
                    )
                return time.monotonic() - t0
            if end is not None and time.monotonic() >= end:
                raise CommandTimeout(self.spec.name, timeout_seconds)
            if not self.is_alive():
                self._abort_implicit_turn(
                    f"{CRASHED_ERROR_PREFIX}pair runtime exited mid self-woken turn",
                    status="failed",
                )
                raise CLIError(
                    f"{CRASHED_ERROR_PREFIX}pair runtime exited during a self-woken turn "
                    f"(before this message was written)",
                    stderr=self._collect_stderr(),
                )
            if should_stop is not None:
                now_m = time.monotonic()
                if now_m - last_stop_check >= 1.0:
                    last_stop_check = now_m
                    try:
                        if should_stop() and not interrupted:
                            interrupted = True
                            self._write_interrupt_nowait()
                    except Exception:
                        pass
            self._implicit_done.wait(timeout=1.0)

    def _prepare_solicited_turn(
        self, timeout_seconds: int | None, should_stop: "Callable[[], bool] | None",
    ) -> float:
        """send() entry. Wait out any self-woken turn (belt — the adapter
        already did), then drain stale stdout and open this turn's scope
        atomically w.r.t. the reader's scope transitions. Returns seconds
        waited. Loops because a new self-woken turn can open between the
        wait returning and the lock being taken."""
        waited = 0.0
        while True:
            waited += self.wait_for_implicit_idle(timeout_seconds, should_stop)
            with self._scope_lock:
                if self._implicit_scope is not None:
                    continue  # opened in the gap — wait again
                # Belt: every queued line was attributed by the reader before
                # it was queued (and lines with no scope open aren't queued at
                # all), so anything here belongs to a finished turn.
                while True:
                    try:
                        self._stdout_q.get_nowait()
                    except queue.Empty:
                        break
                with self._main_log_lock:
                    self._current_scope = TurnLogScope(
                        self.main_log_path, self._main_log_lines + 1,
                    )
                return waited

    # ---- send ----

    def _write_interrupt_nowait(self) -> None:
        """Write the CLI's in-band ``control_request: interrupt`` to stdin and
        return IMMEDIATELY — do NOT wait for the ack.

        v0.10.0 (terminal stop). Unlike ``send_interrupt`` (called from a
        DIFFERENT thread, with a wait loop), this is called from the read-loop
        thread itself; the loop that follows catches the resulting
        ``error_during_execution`` result. Waiting here would deadlock (the same
        thread can't both wait for the ack AND be the one draining stdout for
        it) — Crack A from the v0.10.0 stop-design review. A tiny stdin write
        never blocks (pipe buffer), so there's no deadlock the other way."""
        if not self.is_alive() or self.proc is None or self.proc.stdin is None:
            return
        request_id = f"req_int_{int(time.time())}_{os.urandom(3).hex()}"
        payload = json.dumps({
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "interrupt"},
        }) + "\n"
        try:
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def send(self, message: str, timeout_seconds: int | None = 300,
             on_event: Callable[[dict], None] | None = None,
             should_stop: "Callable[[], bool] | None" = None) -> dict[str, Any]:
        """Push a user message; return the result event dict (mirrors --print JSON).

        Augments the result dict with `_log_scope` containing this turn's main.log
        line range and sub-agent log paths — adapter peels those out into SendResult.

        Args:
            timeout_seconds: Hard ceiling on how long to wait for a result event.
                ``None`` means no auto-kill — the read loop will wait indefinitely
                (use ``pair_stop`` for manual cancellation). When set and hit, raises
                ``CommandTimeout`` AND the registry should evict the runtime so we
                don't carry stale stdout into the next turn.
            should_stop: Optional callback polled ~once/sec during the turn
                (v0.10.0 terminal stop). When it returns True (the server's
                marker-check closure found a fresh stop request and has already
                marked the task stopped), we write an in-band interrupt and let
                the loop catch the ack. Default None = no stop polling (zero
                behavior change to existing sends).
        """
        with self.send_lock:
            if not self.is_alive():
                self.start()
            self.last_activity = datetime.utcnow()
            assert self.proc is not None and self.proc.stdin is not None

            # Cross-process correctness: another MCP subprocess may have written T-N
            # entries to main.idx.json since we last touched it. Resync from disk
            # under the (caller's) cross-process file lock so our T-N assignments
            # don't collide with theirs.
            self._main_tool_counter.reload()

            # v0.12.0: queue FIFO behind any self-woken turn in progress, drain
            # stale stdout, then open this turn's scope atomically w.r.t. the
            # reader's scope transitions. Returns how long we waited + the
            # self-woken turns that completed since the previous send (footer).
            waited_s = self._prepare_solicited_turn(timeout_seconds, should_stop)

            payload = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": message},
            }) + "\n"
            # Scope cleanup is via the outer try/finally below — every exit path
            # (normal return, CLIError, CommandTimeout, any unhandled exception)
            # is guaranteed to clear ``_current_scope``. Without this, an
            # uncaught exception would leave the scope dangling, and the
            # v0.9.8 evictor (which skips runtimes whose scope is set) would
            # permanently protect a zombie runtime from idle eviction. Crack 1
            # from the v0.9.8 design pass.
            try:
                try:
                    self.proc.stdin.write(payload.encode("utf-8"))
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError) as e:
                    raise CLIError(
                        f"pair runtime stdin closed: {e}",
                        stderr=self._collect_stderr(),
                    )

                # timeout_seconds=None → no auto-kill ceiling. The read loop still polls
                # every second via the inner queue.get(timeout=1) for liveness; if the
                # subprocess dies we'll detect it. The pair just runs as long as it
                # needs (or until pair_stop is called).
                end: float | None = (time.monotonic() + timeout_seconds) if timeout_seconds is not None else None
                _last_stop_check = 0.0
                while end is None or time.monotonic() < end:
                    # v0.10.0 terminal stop: poll the should_stop marker check at
                    # most ~once/sec. On a hit, write the interrupt without
                    # waiting (the loop below catches the ack). should_stop has
                    # already marked the task stopped server-side, so the worker
                    # reports "stopped". Any exception here must NEVER kill the
                    # turn — swallow and keep looping.
                    if should_stop is not None:
                        _now_m = time.monotonic()
                        if _now_m - _last_stop_check >= 1.0:
                            _last_stop_check = _now_m
                            try:
                                if should_stop():
                                    self._write_interrupt_nowait()
                            except Exception:
                                pass
                    try:
                        line = self._stdout_q.get(timeout=1)
                    except queue.Empty:
                        if not self.is_alive():
                            # Race-safe snapshot: a concurrent ``stop()`` / ``evict``
                            # in another thread can null ``self.proc`` between
                            # is_alive returning False and our error construction
                            # (the source of the pre-v0.9.8 opaque "(code ?)"
                            # reports). Capture proc + stderr ONCE here so the
                            # error always carries a concrete code when one exists.
                            proc_snapshot = self.proc
                            returncode = (
                                proc_snapshot.returncode
                                if proc_snapshot is not None else None
                            )
                            stderr_snapshot = self._collect_stderr()
                            code_str = (
                                str(returncode) if returncode is not None
                                else "unknown — runtime cleaned up concurrently"
                            )
                            # CRASHED: prefix is the parallel of ORPHANED: —
                            # lets wait.py dispatch "claude.exe died mid-turn"
                            # to exit code 6, distinct from generic work errors
                            # (exit 1). v0.9.8 Bug 4 + Bug 5 fix.
                            raise CLIError(
                                f"{CRASHED_ERROR_PREFIX}pair runtime exited mid-turn (exit {code_str})",
                                stderr=stderr_snapshot,
                                exit_code=returncode,
                            )
                        continue
                    try:
                        ev = json.loads(line.strip())
                    except Exception:
                        continue
                    if on_event is not None:
                        try:
                            on_event(ev)
                        except Exception:
                            pass
                    if ev.get("type") == "result":
                        self.last_activity = datetime.utcnow()
                        # Record the JSONL mtime AFTER our own write completed —
                        # any future mtime greater than this means SOMEONE ELSE wrote.
                        self._last_seen_jsonl_mtime = self._current_jsonl_mtime()
                        # Capture scope BEFORE the finally clears it (we need it
                        # for ev["_log_scope"]).
                        scope = self._current_scope
                        if scope is not None:
                            # Prefer the reader's snapshot (taken the instant the
                            # result was attributed) — by now a self-woken
                            # continuation may already have moved the live fields.
                            snap = scope.result_snapshot or {}
                            ev["_log_scope"] = {
                                "log_path": str(scope.main_log_path),
                                "start_line": scope.start_line,
                                "end_line": snap.get("end_line", scope.end_line),
                                "subagent_logs": list(snap.get("subagent_logs", scope.subagent_logs)),
                                "background_launches": list(
                                    snap.get("background_launches", scope.background_launches)
                                ),
                            }
                        # Completed self-woken turns are popped from the spec by
                        # the server's send runner, not carried here (respawn-safe).
                        ev["_self_woken"] = {"waited_s": waited_s}
                        return ev
                raise CommandTimeout(self.spec.name, timeout_seconds)
            finally:
                # Defensive scope cleanup: clear ``_current_scope`` on EVERY
                # exit path. The v0.9.8 evictor skips runtimes whose scope is
                # set; without this finally, an unhandled exception escaping
                # the read loop would leave the scope dangling and permanently
                # protect a zombie runtime from idle eviction.
                with self._scope_lock:
                    self._current_scope = None

    def _collect_stderr(self) -> str:
        with self._stderr_lock:
            return "".join(self._stderr_buf[-50:])


class RuntimeRegistry:
    def __init__(self, idle_timeout_seconds: int = IDLE_TIMEOUT_SECONDS):
        self._runtimes: dict[str, PairRuntime] = {}
        self._lock = threading.Lock()
        self._idle_timeout = idle_timeout_seconds
        self._evictor: threading.Thread | None = None
        self._stop_evictor = threading.Event()

    def get_or_start(self, spec: PairSpec, adapter: "ClaudeAdapter") -> PairRuntime:
        with self._lock:
            rt = self._runtimes.get(spec.name)
            if rt is not None and rt.is_alive():
                if (rt.spec.session_id != spec.session_id
                        or rt.spec.model != spec.model
                        or rt.spec.cwd != spec.cwd
                        or rt.spec.permission_mode != spec.permission_mode):
                    self._stop_unlocked(spec.name)
                    rt = None
            if rt is None or not rt.is_alive():
                rt = PairRuntime(spec, adapter)
                self._runtimes[spec.name] = rt
        if not rt.is_alive():
            rt.start()
        return rt

    def get_or_none(self, name: str) -> "PairRuntime | None":
        """Return the existing runtime for ``name`` without spawning one.

        Used by ``pair_stop`` and ``pair_status`` to inspect/operate on a live
        runtime without paying the spawn cost when there isn't one.
        """
        with self._lock:
            return self._runtimes.get(name)

    def evict(self, name: str) -> None:
        with self._lock:
            self._stop_unlocked(name)

    def _stop_unlocked(self, name: str) -> None:
        rt = self._runtimes.pop(name, None)
        if rt is not None:
            rt.stop()

    def stop_all(self) -> None:
        with self._lock:
            names = list(self._runtimes.keys())
            for n in names:
                self._stop_unlocked(n)
        self._stop_evictor.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                name: {
                    "alive": rt.is_alive(),
                    "started_at": rt.started_at.isoformat() if rt.started_at else None,
                    "last_activity": rt.last_activity.isoformat(),
                    "persistent": rt.spec.persistent,
                    "model": rt.spec.model,
                    "log_dir": str(rt.log_dir),
                }
                for name, rt in self._runtimes.items()
            }

    def start_evictor(self) -> None:
        if self._evictor is not None and self._evictor.is_alive():
            return

        def _loop() -> None:
            while not self._stop_evictor.wait(EVICTOR_INTERVAL_SECONDS):
                try:
                    self._evict_idle()
                except Exception:
                    pass

        self._evictor = threading.Thread(target=_loop, daemon=True, name="pair-runtime-evictor")
        self._evictor.start()

    def _evict_idle(self) -> None:
        cutoff = datetime.utcnow() - timedelta(seconds=self._idle_timeout)
        to_evict: list[str] = []
        with self._lock:
            for name, rt in self._runtimes.items():
                # v0.12.0 reaper: a self-woken turn with no log line for a full
                # idle period is finalized as ABANDONED. Its task would
                # otherwise sit "running" forever — the owner is alive, so
                # orphan reaping never touches it — and pair_status would show
                # it in flight indefinitely. Persistent runtimes too. The abort
                # does NOT kill the runtime: if the background work resumes
                # later, a fresh self-woken turn simply opens.
                if (rt._implicit_scope is not None  # noqa: SLF001
                        and rt._last_log_activity_at is not None  # noqa: SLF001
                        and rt._last_log_activity_at < cutoff):  # noqa: SLF001
                    rt._abort_implicit_turn(  # noqa: SLF001
                        f"{_async_tasks.ABANDONED_ERROR_PREFIX}no log activity for "
                        f"{self._idle_timeout}s during a self-woken turn — finalized by "
                        f"the reaper; the pair's background work may still be running",
                        status="failed",
                    )
                if rt.spec.persistent:
                    continue
                # v0.9.8: skip runtimes that are mid-turn. The pre-v0.9.8
                # evictor was the root cause of the "exited mid-turn (code ?)"
                # cluster — ``last_activity`` was only bumped at send entry
                # and result, so a turn lasting >10 min looked idle and got
                # taskkill'd. Four real failures observed in one week, all
                # between 10:21 and 10:54 (matching IDLE_TIMEOUT_SECONDS=600s
                # + the 60s evictor cycle).
                #
                # Two protections, belt-and-suspenders:
                #   1. ``_current_scope`` is set for the duration of any turn
                #      (cleared by send's try/finally on EVERY exit path) →
                #      this is the primary signal. GIL makes the attribute
                #      read atomic; worst case is one cycle of lag.
                #   2. ``last_activity`` is now also bumped on every log line
                #      via ``_append_main_log_line``, so a turn that produces
                #      log activity won't look idle even if scope is somehow
                #      stale. Defense in depth (Crack 3 from design pass).
                #
                # Trade-off: a genuinely wedged turn (claude.exe stuck in an
                # infinite loop with no output) will no longer be auto-rescued
                # at 10 min. ``hard_timeout_seconds`` (None by default) or
                # explicit ``pair_stop`` are the intended recovery paths;
                # ``pair_status`` reports likely-hung at 120s+.
                if rt._current_scope is not None:
                    continue
                if rt.last_activity < cutoff or not rt.is_alive():
                    to_evict.append(name)
            for name in to_evict:
                self._stop_unlocked(name)


_REGISTRY: RuntimeRegistry | None = None


def registry() -> RuntimeRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = RuntimeRegistry(idle_timeout_seconds=IDLE_TIMEOUT_SECONDS)
        _REGISTRY.start_evictor()
        atexit.register(_REGISTRY.stop_all)
    return _REGISTRY
