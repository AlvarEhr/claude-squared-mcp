"""CodexAdapter: wraps the OpenAI Codex CLI (``codex exec``) as a pair backend.

Phase 1 = one-shot subprocess per turn (no warm runtime, no self-woken
machinery): every send is ``codex exec --json … resume <thread-id> <message>``
with stdin CLOSED (exec blocks on an open stdin even with a prompt argument),
options BEFORE the subcommand, and ``--skip-git-repo-check -C <cwd>`` on every
call. Model + effort + sandbox are per-invocation in Codex (never inherited
from the thread), so they are re-passed every time — same discipline as
``claude --model``.

Everything empirical here was verified against codex-cli 0.153.4 on Windows
with ChatGPT-plan auth on 2026-09-07 — see HANDOFF.md "Codex backend".
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from claude_squared.adapters.base import PairAdapter
from claude_squared.codex_models import (
    CODEX_1M_CONFIG,
    codex_home,
    context_windows,
    resolve_codex_model,
)
from claude_squared.errors import CLIError, CommandTimeout, SessionMissing
from claude_squared.models import (
    CODEX_PERMISSION_MAP,
    CompactResult,
    ContextReport,
    ContextStatus,
    CreateResult,
    PairSpec,
    PermissionDenial,
    SendResult,
    normalize_permission,
)
from claude_squared.registry import logs_dir
from claude_squared.runtime import ToolCounter
from claude_squared.tool_details import save_codex_item


WARNING_THRESHOLD = 0.60
STRONG_WARNING_THRESHOLD = 0.85
THREAD_SOURCE = "claude-squared"   # tags our threads in ~/.codex sqlite + the Desktop UI
PROBE_PROMPT = "Reply with exactly: pair-ready"

# User-config keys re-added under ``--ignore-user-config``. The flag is
# required (the user's config mounts claude-squared itself as an MCP server →
# recursion; plus Desktop plugins/hooks a pair shouldn't inherit), but it also
# drops settings the sandbox NEEDS: on Windows, without ``windows.sandbox =
# "elevated"`` a ``-s workspace-write`` run silently degrades to a READ-ONLY
# sandbox (verified: "writing is blocked by read-only sandbox"). Whitelist, not
# blanket: each key is re-passed as ``-c dotted.key=<toml literal>``.
CONFIG_READD_KEYS: tuple[str, ...] = (
    "windows",                    # windows.sandbox = "elevated"
    "sandbox_workspace_write",    # network_access etc.
    "model_provider",
    "model_providers",
    "web_search",
    "shell_environment_policy",
)

# stderr lines that mean "the sandbox / approval policy blocked an action".
# There is NO denial event in the --json stream — the model just sees a
# failed tool call — so this is the only structured signal.
_DENIAL_RE = re.compile(
    r"rejected by user approval settings|blocked by read-only sandbox|"
    r"writing outside of the project|sandbox.*denied|approval.*denied",
    re.IGNORECASE,
)
_READONLY_DEGRADE_RE = re.compile(r"blocked by read-only sandbox", re.IGNORECASE)
_OS_DENIED_RE = re.compile(
    r"Access to the path '[^']*' is denied|Access is denied|PermissionDenied|"
    r"Permission denied|Operation not permitted|EACCES|EPERM",
)
_MODEL_UNSUPPORTED_RE = re.compile(
    r"model is not supported|Model metadata for .* not found|invalid_request_error",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|quota", re.IGNORECASE)


# In-flight one-shot processes, so pair_stop / pair_status can reach them
# (there is no PairRuntime for codex). name → {"proc": Popen, "task_id": str,
# "started_at": datetime, "last_activity": datetime}.
_INFLIGHT: dict[str, dict[str, Any]] = {}
_INFLIGHT_LOCK = threading.Lock()


def inflight_info(name: str) -> dict[str, Any] | None:
    with _INFLIGHT_LOCK:
        info = _INFLIGHT.get(name)
        return dict(info) if info else None


def _tree_kill(proc: subprocess.Popen) -> str:
    """Kill the codex process AND its command grandchildren (PowerShell
    wrappers); ``proc.kill()`` alone would orphan a running command."""
    if proc.poll() is not None:
        return "already-exited"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False)
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return "tree-killed"
    except Exception:
        try:
            proc.kill()
            return "fallback-kill"
        except Exception:
            return "kill-failed"


def stop_inflight(name: str) -> str | None:
    """Tree-kill the pair's in-flight codex turn (pair_stop path). Returns an
    outcome string, or None when nothing was in flight in this process."""
    with _INFLIGHT_LOCK:
        info = _INFLIGHT.get(name)
    if not info:
        return None
    proc = info.get("proc")
    if proc is None:
        return None
    return _tree_kill(proc)


# ---------------------------------------------------------------------------
# Binary + config resolution
# ---------------------------------------------------------------------------

_EXE_CACHE: dict[str, str] = {}


def _codex_version(exe: str) -> tuple[int, ...]:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=15, stdin=subprocess.DEVNULL).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
        return tuple(int(x) for x in m.groups()) if m else ()
    except Exception:
        return ()


def _user_config_path() -> Path:
    return codex_home() / "config.toml"


try:  # py3.11+ stdlib; ``tomli`` (declared for <3.11 in pyproject) is API-compatible
    import tomllib as _toml
except ImportError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ImportError:
        _toml = None  # type: ignore[assignment]


def _load_user_config() -> dict[str, Any]:
    """Parsed ``~/.codex/config.toml`` or {} when missing/unreadable. Without a
    TOML parser (Python 3.10 with no ``tomli``) this silently returns {} —
    which drops the ``windows.sandbox`` re-add; ``sandbox_support_note`` says so."""
    p = _user_config_path()
    if _toml is None:
        return {}
    try:
        with open(p, "rb") as f:
            return _toml.load(f)
    except Exception:
        return {}


def codex_executable() -> str:
    """Locate the codex binary. Order: ``CLAUDE_PAIR_CODEX_PATH`` env →
    ``CODEX_CLI_PATH`` recorded in the user's config.toml (the Desktop-managed
    build, which is the only one whose updater works) → newest
    ``%LOCALAPPDATA%/OpenAI/Codex/bin/<hash>/codex.exe`` by ``--version`` →
    ``codex`` on PATH."""
    if "exe" in _EXE_CACHE:
        return _EXE_CACHE["exe"]
    env = os.environ.get("CLAUDE_PAIR_CODEX_PATH")
    if env and Path(env).exists():
        _EXE_CACHE["exe"] = env
        return env
    cfg = _load_user_config()
    # The Desktop writes CODEX_CLI_PATH into an MCP server's env block; scan
    # every [mcp_servers.*.env] for it rather than assuming a server name.
    for srv in (cfg.get("mcp_servers") or {}).values():
        envblk = (srv or {}).get("env") if isinstance(srv, dict) else None
        cand = (envblk or {}).get("CODEX_CLI_PATH") if isinstance(envblk, dict) else None
        if cand and Path(cand).exists():
            _EXE_CACHE["exe"] = str(cand)
            return str(cand)
    local = os.environ.get("LOCALAPPDATA")
    best: tuple[tuple[int, ...], str] | None = None
    if local:
        for cand in glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")):
            v = _codex_version(cand)
            if best is None or v > best[0]:
                best = (v, cand)
    if best:
        _EXE_CACHE["exe"] = best[1]
        return best[1]
    found = shutil.which("codex")
    _EXE_CACHE["exe"] = found or "codex"
    return _EXE_CACHE["exe"]


def _toml_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # TOML basic strings share JSON escaping for our values
    if isinstance(v, list):
        return "[" + ", ".join(_toml_literal(x) for x in v) + "]"
    return json.dumps(str(v))


def _flatten(prefix: str, obj: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    else:
        out.append((prefix, _toml_literal(obj)))


def config_readd_args() -> list[str]:
    """``-c key=value`` for every whitelisted top-level key present in the
    user's config (flattened to dotted paths)."""
    cfg = _load_user_config()
    pairs: list[tuple[str, str]] = []
    for key in CONFIG_READD_KEYS:
        if key in cfg:
            _flatten(key, cfg[key], pairs)
    args: list[str] = []
    for k, v in pairs:
        args += ["-c", f"{k}={v}"]
    return args


def sandbox_support_note() -> str | None:
    """Windows without ``windows.sandbox`` configured → workspace-write can't
    be enforced (degrades to read-only). Surfaced at create."""
    if os.name != "nt":
        return None
    if _toml is None:
        return ("this Python has no TOML parser (3.10 without `tomli`), so ~/.codex/config.toml "
                "cannot be read and `[windows] sandbox` is NOT re-added under --ignore-user-config — "
                "'workspace'/'auto' levels will run READ-ONLY. Install `tomli` or use Python 3.11+.")
    cfg = _load_user_config()
    if isinstance(cfg.get("windows"), dict) and cfg["windows"].get("sandbox"):
        return None
    return (
        "Codex on Windows needs `[windows] sandbox = \"elevated\"` in ~/.codex/config.toml "
        "for the workspace-write sandbox; without it 'workspace'/'auto' levels silently "
        "run READ-ONLY. Open the Codex app once (it writes the key) or add it by hand."
    )


# ---------------------------------------------------------------------------
# Thread store access (read-only)
# ---------------------------------------------------------------------------

def _state_db_path() -> Path | None:
    cands = sorted(codex_home().glob("state_*.sqlite"))
    return cands[-1] if cands else None


def _ro_connect(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)


def rollout_path_for(thread_id: str) -> Path | None:
    """Authoritative: ``threads.rollout_path`` in the state sqlite; fallback:
    glob the sessions tree for ``rollout-*-<id>.jsonl``."""
    db = _state_db_path()
    if db is not None:
        try:
            con = _ro_connect(db)
            try:
                row = con.execute("select rollout_path from threads where id=?",
                                  (thread_id,)).fetchone()
            finally:
                con.close()
            if row and row[0]:
                p = Path(row[0])
                if p.exists():
                    return p
        except Exception:
            pass
    for cand in glob.glob(str(codex_home() / "sessions" / "*" / "*" / "*" / f"rollout-*{thread_id}*.jsonl")):
        return Path(cand)
    return None


def guardian_verdicts_since(since_epoch: float, parent_thread_id: str | None = None) -> list[dict[str, Any]]:
    """Verdicts of ``codex-auto-review`` guardian threads created since
    ``since_epoch`` (the ``--approve-for-me`` reviewer). Each: {thread_id,
    outcome, risk_level, rationale}. When ``parent_thread_id`` is given only
    guardian threads whose rollout references it (their ``token_usage_record``
    carries the parent thread as ``session_id``) are returned, so concurrent
    pairs / Desktop activity can't cross-attribute verdicts (Astra catch).
    Best-effort; [] on any failure."""
    db = _state_db_path()
    if db is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        con = _ro_connect(db)
        try:
            rows = con.execute(
                "select id, rollout_path, created_at from threads "
                "where thread_source='guardian_review' and created_at >= ? "
                "order by created_at asc", (int(since_epoch) - 1,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    for tid, rp, _ts in rows:
        verdict: dict[str, Any] = {"thread_id": tid}
        matched_parent = parent_thread_id is None
        try:
            p = Path(rp) if rp else rollout_path_for(tid)
            if p and p.exists():
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if parent_thread_id and parent_thread_id in line:
                        matched_parent = True
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    pl = d.get("payload") or {}
                    if d.get("type") == "event_msg" and pl.get("type") == "agent_message":
                        try:
                            verdict.update(json.loads(pl.get("message") or "{}"))
                        except Exception:
                            verdict["raw"] = str(pl.get("message"))[:200]
        except Exception:
            pass
        if matched_parent:
            out.append(verdict)
    return out


def read_last_token_count(rollout: Path | None) -> dict[str, Any] | None:
    """The LAST ``token_count`` event_msg payload in a rollout: carries
    ``info.total_token_usage``, ``info.last_token_usage``,
    ``info.model_context_window`` (the EFFECTIVE window — 258,400 default,
    828,400 with the 1M config) and ``rate_limits`` (plan quota)."""
    if rollout is None or not rollout.exists():
        return None
    last: dict[str, Any] | None = None
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"token_count"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pl = d.get("payload") or {}
                if d.get("type") == "event_msg" and pl.get("type") == "token_count":
                    last = pl
    except Exception:
        return None
    return last


def _estimate_compacted_tokens(rollout: Path | None) -> int:
    """Rough size of the LAST ``compacted`` record's replacement history
    (chars / 4). 0 when there is none."""
    if rollout is None or not rollout.exists():
        return 0
    last: dict[str, Any] | None = None
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"compacted"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "compacted":
                    last = d.get("payload") or {}
    except Exception:
        return 0
    if not last:
        return 0
    text = json.dumps(last.get("replacement_history") or last.get("message") or "", default=str)
    return max(0, len(text) // 4)


def plan_usage_text(rate_limits: dict[str, Any] | None) -> str | None:
    if not isinstance(rate_limits, dict):
        return None
    parts: list[str] = []
    for key, label in (("primary", ""), ("secondary", "secondary ")):
        win = rate_limits.get(key)
        if not isinstance(win, dict):
            continue
        used = win.get("used_percent")
        mins = win.get("window_minutes")
        resets = win.get("resets_at")
        if used is None:
            continue
        span = ""
        if isinstance(mins, (int, float)) and mins:
            days = mins / 1440
            span = (f"{int(days)}-day" if days >= 1 and float(days).is_integer()
                    else f"{int(mins/60)}h" if mins >= 60 else f"{int(mins)}min")
        when = ""
        if isinstance(resets, (int, float)) and resets:
            try:
                when = ", resets " + datetime.fromtimestamp(resets).astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = ""
        parts.append(f"{label}{used:.0f}% of the {span} limit used{when}")
    plan = rate_limits.get("plan_type")
    reached = rate_limits.get("rate_limit_reached_type")
    s = "; ".join(parts)
    if plan:
        s = f"{s} (plan {plan})" if s else f"plan {plan}"
    if reached:
        s = f"{s} — LIMIT REACHED ({reached})"
    return s or None


# ---------------------------------------------------------------------------
# Rollout transcript helpers (used by transcript.py + pair_rewind)
# ---------------------------------------------------------------------------

def is_codex_rollout(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
        return '"session_meta"' in first
    except Exception:
        return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
                parts.append(str(c.get("text") or ""))
    return "\n".join(p for p in parts if p)


def tail_turns_codex(path: Path, last_n: int = 10) -> list[dict[str, Any]]:
    """Last N user/assistant turns from a Codex rollout JSONL, in the same
    shape ``transcript.tail_turns`` returns for Claude sessions."""
    turns: list[dict[str, Any]] = []
    pending_tools: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "response_item":
            continue
        pl = d.get("payload") or {}
        t = pl.get("type")
        if t == "message":
            role = pl.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _content_text(pl.get("content"))
            # Environment/context injections arrive as user messages wrapped in
            # <environment_context>/<permissions instructions> — skip those.
            if role == "user" and text.lstrip().startswith("<"):
                continue
            if not text:
                continue
            entry: dict[str, Any] = {"role": role, "content": text, "timestamp": d.get("timestamp")}
            if role == "assistant" and pending_tools:
                entry["tool_uses"] = pending_tools
                pending_tools = []
            turns.append(entry)
        elif t in ("custom_tool_call", "function_call", "local_shell_call"):
            pending_tools.append({
                "name": pl.get("name") or t,
                "input": pl.get("input") or pl.get("arguments"),
                "id": pl.get("call_id") or pl.get("id"),
            })
    return turns[-last_n:] if last_n > 0 else turns


def list_turn_points_codex(path: Path) -> list[dict[str, Any]]:
    """Rewind points for a Codex rollout: one per ``turn_context`` line (each
    turn starts with one, followed by the user message). Truncating the file
    just before a ``turn_context`` line drops that turn and everything after —
    verified 2026-09-07: ``exec resume`` reads the rollout JSONL (a truncated
    thread forgot the dropped turn; sqlite still held it, harmlessly)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    points: list[dict[str, Any]] = []
    tc_idx: list[int] = []
    for i, line in enumerate(lines):
        if '"turn_context"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "turn_context":
            tc_idx.append(i)
    for n, idx in enumerate(tc_idx):
        end = tc_idx[n + 1] if n + 1 < len(tc_idx) else len(lines)
        preview = ""
        asst = 0
        tools = 0
        files: list[str] = []
        ts = None
        for line in lines[idx:end]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            pl = d.get("payload") or {}
            if d.get("type") == "turn_context" and ts is None:
                ts = d.get("timestamp")
            if d.get("type") == "response_item":
                t = pl.get("type")
                if t == "message" and pl.get("role") == "user" and not preview:
                    txt = _content_text(pl.get("content")).strip()
                    if txt and not txt.startswith("<"):
                        preview = txt.replace("\n", " ")[:100]
                elif t == "message" and pl.get("role") == "assistant":
                    asst += 1
                elif t in ("custom_tool_call", "function_call", "local_shell_call"):
                    tools += 1
                    inp = str(pl.get("input") or pl.get("arguments") or "")
                    if "apply_patch" in inp:
                        for mm in re.finditer(r"\*\*\* (?:Add|Update|Delete) File: ([^\n\\]+)", inp):
                            fp = mm.group(1).strip()
                            if fp not in files:
                                files.append(fp)
        points.append({
            "point": n + 1, "raw_line_index": idx, "timestamp": ts, "preview": preview,
            "after_assistant_turns": asst, "after_tool_calls": tools, "after_files": files,
        })
    return points


def _history_db_path() -> Path | None:
    cands = sorted(codex_home().glob("thread_history_*.sqlite"))
    return cands[-1] if cands else None


def resync_history_after_truncate(thread_id: str, cut_ordinal: int, path: Path) -> str:
    """Bring Codex's sqlite history PROJECTION back in step with a truncated
    rollout. Codex keeps ``thread_history_projection_state(next_rollout_ordinal,
    next_rollout_byte_offset)`` + projected ``thread_turns`` / ``thread_items``
    rows; ``exec resume`` reads the rollout and simply appends, but ``exec
    fork`` (and the Desktop history view) go through the projection, which
    hard-errors once the rollout's ordinals no longer match ("expected ordinal
    23, got 22"). Verified 2026-09-07: deleting the projected rows at/after
    the cut and re-pointing the projection to (cut, new file size) makes fork
    work again and the projector re-projects the resumed turns itself.
    Best-effort: returns a one-line outcome, never raises."""
    db = _history_db_path()
    if db is None:
        return "no codex history sqlite found — projection left as is (fork may fail until Codex rebuilds it)"
    try:
        offset = path.stat().st_size
        con = sqlite3.connect(str(db), timeout=10)
        try:
            with con:
                for table in ("thread_items", "thread_turns", "thread_realtime_items"):
                    exists = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
                    if not exists and table == "thread_realtime_items":
                        continue  # optional on older Codex builds
                    # Any incompatible required table/column must roll back
                    # all deletes, without advancing the projection cursor.
                    con.execute(f"delete from {table} where thread_id=? and rollout_ordinal>=?",
                                (thread_id, cut_ordinal))
                n = con.execute(
                    "update thread_history_projection_state set next_rollout_ordinal=?, "
                    "next_rollout_byte_offset=? where thread_id=?",
                    (cut_ordinal, offset, thread_id),
                ).rowcount
        finally:
            con.close()
        if n:
            return f"codex history projection re-synced to ordinal {cut_ordinal} (fork keeps working)"
        return "no projection row for this thread (nothing to re-sync)"
    except Exception as e:  # noqa: BLE001
        return f"codex history projection re-sync failed ({e}); pair_fork may fail on this thread until Codex rebuilds it"


def truncate_rollout_before_line(path: Path, raw_line_index: int,
                                 thread_id: str | None = None) -> tuple[int, str | None]:
    """Keep lines [0, raw_line_index); drop the rest (atomic via tmp +
    replace), then re-sync Codex's sqlite projection when ``thread_id`` is
    given. Returns ``(lines_dropped, resync_note)``."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = lines[:raw_line_index]
    dropped = len(lines) - len(kept)
    cut_ordinal = raw_line_index
    try:
        cut_ordinal = int(json.loads(lines[raw_line_index]).get("ordinal", raw_line_index))
    except Exception:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    os.replace(tmp, path)
    note = resync_history_after_truncate(thread_id, cut_ordinal, path) if thread_id else None
    return dropped, note


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


class CodexAdapter(PairAdapter):
    backend_name = "codex"

    # ---- public surface ------------------------------------------------

    def create(self, spec: PairSpec, initial_message: str | None = None) -> CreateResult:
        """Eager create: one tiny probe turn materializes the thread (row +
        rollout) and returns its thread id — Codex names the thread itself at
        ``thread.started``, so the caller MUST take ``session_id`` from the
        result (never a pre-generated id)."""
        prompt = initial_message or PROBE_PROMPT
        sys_text = self._resolve_system_prompt(spec)
        if sys_text:
            # Codex exec has no --append-system-prompt; the thread's first user
            # message is the closest persistent equivalent (it stays in the
            # thread history for every later resume).
            prompt = (
                "<operating instructions for this thread — follow them in every turn>\n"
                f"{sys_text}\n</operating instructions>\n\n{prompt}"
            )
        args = self._exec_args(spec, model=None, effort=None, permission_mode=None)
        # Prompt over stdin ("-"): a long briefing as an argv element would
        # exceed Windows' ~32K command-line limit (Astra catch). Verified:
        # ``exec … -`` reads the prompt from stdin and proceeds on EOF.
        args.append("-")
        events, stderr, code, _dur = self._run(args, spec, timeout_seconds=300,
                                               task_label="create", log=False,
                                               stdin_text=prompt)
        tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        failed = next((e for e in events if e.get("type") == "turn.failed"), None)
        if not tid:
            raise CLIError("codex exec produced no thread.started event",
                           stderr=stderr or "\n".join(json.dumps(e)[:200] for e in events[-3:]),
                           exit_code=code)
        if failed or code != 0:
            msg = str((failed or {}).get("error", {}).get("message") if failed else "") or stderr
            if _MODEL_UNSUPPORTED_RE.search(msg or ""):
                raise CLIError(
                    f"the Codex model '{self._model_for(spec)}' is not available on this "
                    f"plan/account ({msg.strip()[:200]}). Pick a listed model (see "
                    f"pair_settings_get) — e.g. 'sol', 'terra' or 'luna'.",
                    stderr=stderr, exit_code=code,
                )
            raise CLIError(f"codex create turn failed: {msg.strip()[:300]}",
                           stderr=stderr, exit_code=code)
        reply = ""
        for e in events:
            if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
                reply = str(e["item"].get("text") or "")
        new_spec = spec.model_copy(update={"session_id": tid})
        tp = self.transcript_path(new_spec)
        return CreateResult(name=spec.name, session_id=tid,
                            transcript_path=str(tp) if tp else None,
                            initial_response=reply or None)

    def send(self, spec: PairSpec, message: str, *, model: str | None = None,
             effort: str | None = None, permission_mode: str | None = None,
             timeout_seconds: int | None = 300,
             on_event: Callable[[dict], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             task_id: str | None = None) -> SendResult:
        if not self.session_exists(spec):
            raise SessionMissing(spec.name, spec.session_id)
        args = self._exec_args(spec, model=model, effort=effort, permission_mode=permission_mode)
        args += ["resume", spec.session_id, "-"]   # message over stdin (see create)
        started = time.time()
        events, stderr, code, dur_ms = self._run(
            args, spec, timeout_seconds=timeout_seconds, task_label="send", log=True,
            on_event=on_event, should_stop=should_stop, task_id=task_id,
            stdin_text=message,
        )
        return self._build_send_result(
            spec, events, stderr, code, dur_ms, started,
            model_used=self._model_for(spec, model),
            permission_level=normalize_permission(permission_mode or spec.permission_mode),
        )

    def fork(self, spec: PairSpec, sentinel: str | None = None, timeout_seconds: int = 300) -> str:
        """Native ``exec fork <thread-id>`` — no prompt needed (verified: a
        promptless fork with stdin closed creates the new thread, 0 tokens,
        no turn). Returns the new thread id; the source is untouched."""
        args = self._exec_args(spec, model=None, effort=None, permission_mode=None)
        args += ["fork", spec.session_id]
        events, stderr, code, _ = self._run(args, spec, timeout_seconds=timeout_seconds,
                                            task_label="fork", log=False)
        tid = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        if not tid or tid == spec.session_id:
            raise CLIError(f"codex exec fork did not produce a new thread id (got {tid!r})",
                           stderr=stderr, exit_code=code)
        return tid

    def compact(self, spec: PairSpec, steering_prompt: str | None = None,
                timeout_seconds: int = 600,
                should_stop: Callable[[], bool] | None = None) -> CompactResult:
        """Manual compaction through ``codex app-server`` (stdio JSON-RPC).

        ``codex exec`` has no compaction command (a literal ``/compact`` prompt
        is just text the model answers "Compacted." to — verified), but the
        app-server protocol has ``thread/compact/start``: initialize →
        ``thread/resume`` (with a per-thread ``config`` that blanks
        ``mcp_servers`` so this MCP isn't started recursively; the Desktop's
        plugin servers still start, ~10 s) → ``thread/compact/start`` → wait
        for the ``contextCompaction`` item + ``turn/completed``. The rollout
        gains a ``compacted`` line; a later ``exec resume`` continues from the
        summary (verified 0.153.4: the thread still knew its code word).
        Codex compaction takes no steering text — ``steering_prompt`` is
        ignored with a note.
        """
        if not self.session_exists(spec):
            raise SessionMissing(spec.name, spec.session_id)
        rollout = self.transcript_path(spec)
        tc0 = read_last_token_count(rollout) or {}
        info0 = tc0.get("info") or {}
        pre = int((info0.get("last_token_usage") or {}).get("input_tokens")
                  or (info0.get("total_token_usage") or {}).get("total_tokens") or 0)
        t0 = time.monotonic()
        outcome = self._appserver_compact(spec, timeout_seconds=timeout_seconds, should_stop=should_stop)
        dur_ms = int((time.monotonic() - t0) * 1000)
        tc1 = read_last_token_count(rollout) or {}
        info1 = tc1.get("info") or {}
        post = int((info1.get("total_token_usage") or {}).get("total_tokens") or 0)
        notes: list[str] = []
        if not post:
            # Codex's post-compaction token_count reports zeros (verified on a
            # 164k thread); estimate from the compaction record's replacement
            # history instead — the next reply's footer shows the real fill.
            est = _estimate_compacted_tokens(rollout)
            if est:
                post = est
                notes.append("post-compaction size is an estimate from the summary (~chars/4); "
                             "the next reply's footer shows the real context fill")
        if steering_prompt:
            notes.append("steering_prompt ignored: Codex compaction takes no steering text")
        if outcome:
            notes.append(outcome)
        note = "; ".join(notes) if notes else None
        try:
            log_dir = logs_dir() / spec.name
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "main.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] === COMPACTED {pre:,} -> {post:,} "
                        f"tokens (manual, codex app-server) ===\n")
        except Exception:
            pass
        return CompactResult(name=spec.name, session_id=spec.session_id, pre_tokens=pre,
                             post_tokens=post, duration_ms=dur_ms, trigger="manual (app-server)",
                             summary_preview=note)

    def _appserver_compact(self, spec: PairSpec, *, timeout_seconds: int,
                           should_stop: Callable[[], bool] | None = None) -> str | None:
        """Drive one compaction over the app-server's stdio JSON-RPC. Returns a
        short note (or None) and raises CLIError / CommandTimeout on failure;
        ``should_stop`` (polled ~1/s) tree-kills the daemon and raises."""
        exe = codex_executable()
        args = [exe, "app-server"] + config_readd_args()
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "cwd": spec.cwd or None,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **popen_kwargs)
        except OSError as e:
            raise CLIError(f"could not start codex app-server ({exe}): {e}")
        with _INFLIGHT_LOCK:
            _INFLIGHT[spec.name] = {"proc": proc, "task_id": None, "started_at": datetime.utcnow(),
                                    "last_activity": datetime.utcnow(), "label": "compact"}
        msgs: list[dict] = []
        lock = threading.Lock()
        stderr_chunks: list[str] = []

        def _rd() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                try:
                    m = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                with lock:
                    msgs.append(m)

        def _erd() -> None:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                stderr_chunks.append(raw.decode("utf-8", "replace"))

        threading.Thread(target=_rd, daemon=True).start()
        threading.Thread(target=_erd, daemon=True).start()
        deadline = time.monotonic() + max(30, int(timeout_seconds))

        def _send(i: int, method: str, params: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                          "params": params}) + "\n").encode("utf-8"))
            proc.stdin.flush()

        last_stop_check = [0.0]

        def _check_stop() -> None:
            if should_stop is None:
                return
            now = time.monotonic()
            if now - last_stop_check[0] < 1.0:
                return
            last_stop_check[0] = now
            try:
                if should_stop():
                    _tree_kill(proc)
                    raise CLIError("compaction stopped by pair_stop (codex app-server tree-killed; "
                                   "the thread is unchanged unless the compaction record was already written)")
            except CLIError:
                raise
            except Exception:
                pass

        def _wait_result(i: int) -> dict:
            while time.monotonic() < deadline and proc.poll() is None:
                _check_stop()
                with lock:
                    for m in msgs:
                        if m.get("id") == i and ("result" in m or "error" in m):
                            return m
                time.sleep(0.2)
            return {}

        def _fail(msg: str) -> None:
            _tree_kill(proc)
            raise CLIError(msg, stderr="".join(stderr_chunks)[-1500:])

        try:
            from claude_squared import __version__ as _ver
            _send(1, "initialize", {"clientInfo": {"name": "claude-squared", "version": _ver}})
            r = _wait_result(1)
            if not r or "error" in r:
                if not r and proc.poll() is not None:
                    _fail(f"codex app-server exited (code {proc.poll()}) before initialize completed")
                _fail(f"codex app-server initialize failed: {json.dumps(r.get('error'))[:300] if r else 'timeout'}")
            # JSON-RPC handshake completion notification (documented; the
            # server tolerated its absence on 0.153.4 but don't rely on that).
            try:
                assert proc.stdin is not None
                proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except Exception:
                pass
            cfg: dict[str, Any] = {"mcp_servers": {}}
            if spec.effort:
                cfg["model_reasoning_effort"] = spec.effort
            for k, v in (spec.backend_options or {}).get("config", {}).items():
                cfg[k] = v
            _send(2, "thread/resume", {
                "threadId": spec.session_id, "cwd": spec.cwd or os.getcwd(),
                "model": self._model_for(spec), "sandbox": "read-only",
                "config": cfg, "excludeTurns": True,
            })
            r = _wait_result(2)
            if not r or "error" in r:
                _fail(f"codex app-server thread/resume failed: {json.dumps(r.get('error'))[:300] if r else 'timeout'}")
            _send(3, "thread/compact/start", {"threadId": spec.session_id})
            r = _wait_result(3)
            if not r or "error" in r:
                _fail(f"codex app-server thread/compact/start failed: {json.dumps(r.get('error'))[:300] if r else 'timeout'}")
            # Completion = a contextCompaction item completed for this thread
            # followed by turn/completed (thread/compacted was NOT observed on
            # 0.153.4 — the item + turn notifications are the reliable signal).
            saw_item = False
            turn_status: str | None = None
            turn_error: str | None = None
            while time.monotonic() < deadline and proc.poll() is None:
                _check_stop()
                with lock:
                    for m in msgs:
                        meth = m.get("method")
                        p = m.get("params") or {}
                        if p.get("threadId") not in (None, spec.session_id):
                            continue
                        if meth == "item/completed" and (p.get("item") or {}).get("type") == "contextCompaction":
                            saw_item = True
                        elif meth == "thread/compacted":
                            saw_item = True
                        elif meth == "turn/completed":
                            turn = p.get("turn") or {}
                            turn_status = str(turn.get("status") or "completed")
                            if turn.get("error"):
                                turn_error = json.dumps(turn.get("error"))[:300]
                        elif meth == "error":
                            turn_error = json.dumps(p)[:300]
                            turn_status = "failed"
                if turn_status is not None:
                    break
                time.sleep(0.3)
            if turn_status is None:
                _tree_kill(proc)
                raise CommandTimeout(spec.name, int(timeout_seconds))
            if turn_error or (turn_status not in ("completed",) and not saw_item):
                _fail(f"codex compaction did not complete (status {turn_status}): {turn_error or 'no contextCompaction item observed'}")
            return None if saw_item else f"turn completed with status {turn_status} but no contextCompaction item was observed"
        finally:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                _tree_kill(proc)
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(spec.name, {}).get("proc") is proc:
                    _INFLIGHT.pop(spec.name, None)

    def context(self, spec: PairSpec, timeout_seconds: int = 60) -> ContextReport:
        """Zero-inference: the rollout's last ``token_count`` event."""
        tc = read_last_token_count(self.transcript_path(spec))
        if not tc:
            raise CLIError(f"no token_count recorded yet for pair '{spec.name}' (send a message first)")
        info = tc.get("info") or {}
        last = info.get("last_token_usage") or {}
        total = info.get("total_token_usage") or {}
        window = int(info.get("model_context_window") or 0)
        used = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
        pct = (used / window * 100) if window else 0.0
        cw = context_windows(spec.model)
        md = [
            f"**Model:** {spec.model}",
            f"**Tokens:** {_fmt_tokens(used)} / {_fmt_tokens(window)} ({pct:.0f}%)",
            f"- last call: input {last.get('input_tokens', 0):,} (cached {last.get('cached_input_tokens', 0):,}), "
            f"output {last.get('output_tokens', 0):,} (reasoning {last.get('reasoning_output_tokens', 0):,})",
            f"- thread total: input {total.get('input_tokens', 0):,}, output {total.get('output_tokens', 0):,}, "
            f"all {total.get('total_tokens', 0):,}",
            f"- context_window setting: {spec.context_window}"
            + (f" (this model: {cw[0]:,} usable by default, {cw[1]:,} with '1m')" if cw else ""),
        ]
        pu = plan_usage_text(tc.get("rate_limits"))
        if pu:
            md.append(f"- plan usage: {pu}")
        md.append("(zero inference — read from the thread's rollout; Codex has no /context command)")
        return ContextReport(name=spec.name, session_id=spec.session_id, model=spec.model,
                             tokens_used=used, tokens_max=window, percent=pct,
                             raw_markdown="\n".join(md))

    def invoke_skill(self, spec: PairSpec, skill_name: str, args: str | None = None,
                     timeout_seconds: int = 300) -> SendResult:
        raise CLIError(
            f"pair_invoke is not available for Codex pairs ('{spec.name}'): codex exec has "
            f"no slash-command channel. Ask for the behaviour in plain language via "
            f"pair_send instead (Codex skills under ~/.codex/skills are picked up by name)."
        )

    def transcript_path(self, spec: PairSpec) -> Path | None:
        if not spec.session_id:
            return None
        return rollout_path_for(spec.session_id)

    def session_exists(self, spec: PairSpec) -> bool:
        p = self.transcript_path(spec)
        return p is not None and p.exists()

    # ---- arg building --------------------------------------------------

    @staticmethod
    def _model_for(spec: PairSpec, override: str | None = None) -> str:
        slug, _note = resolve_codex_model(override or spec.model)
        return slug

    def _resolve_system_prompt(self, spec: PairSpec) -> str | None:
        from claude_squared.registry import profiles_dir
        parts = []
        if spec.profile_name:
            pp = profiles_dir() / f"{spec.profile_name}.md"
            if pp.exists():
                parts.append(pp.read_text(encoding="utf-8").strip())
        if spec.system_prompt_append:
            parts.append(spec.system_prompt_append.strip())
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def validate_backend_options(level: str, backend_options: dict[str, Any] | None) -> None:
        """Reject combinations the CLI itself refuses. Verified 0.153.4:
        ``--sandbox`` "cannot be used with ``--approve-for-me``" — the
        approve-for-me flag IMPLIES the workspace-write sandbox (help text +
        the recorded sandbox_policy of every auto-level probe thread), so an
        explicit ``backend_options.sandbox`` at the ``auto`` level has no
        legal spelling. Raises ``ValueError`` with the remedy."""
        bo = backend_options or {}
        if not bo:
            return
        lvl = normalize_permission(level)
        _sandbox, approval = CODEX_PERMISSION_MAP[lvl]
        if bo.get("approval"):
            a = str(bo["approval"]).lower()
            if a not in ("none", "never", "approve-for-me", "bypass"):
                raise ValueError(
                    f"backend_options.approval must be none|approve-for-me|bypass (got {a!r})")
            approval = None if a in ("none", "never") else a
        sb = bo.get("sandbox")
        if sb and str(sb) not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError(
                f"backend_options.sandbox must be read-only|workspace-write|danger-full-access (got {sb!r})")
        if sb and approval == "approve-for-me":
            raise ValueError(
                "backend_options.sandbox cannot be combined with the 'auto' level: codex exec "
                "rejects -s together with --approve-for-me (which already implies the "
                "workspace-write sandbox). Use permission_mode='workspace' (+ sandbox) or set "
                "backend_options.approval='none'."
            )

    @staticmethod
    def permission_args(level: str, backend_options: dict[str, Any] | None = None) -> list[str]:
        """Neutral level → exec flags, honoring ``backend_options.sandbox`` /
        ``backend_options.approval`` native overrides.

        ``auto`` emits ONLY ``--approve-for-me``: the flag implies the
        workspace-write sandbox and the CLI refuses an explicit ``-s`` next to
        it (verified 0.153.4). ``unrestricted`` emits only the bypass flag.
        """
        CodexAdapter.validate_backend_options(level, backend_options)
        lvl = normalize_permission(level)
        sandbox, approval = CODEX_PERMISSION_MAP[lvl]
        bo = backend_options or {}
        if bo.get("sandbox"):
            sandbox = str(bo["sandbox"])
        if bo.get("approval"):
            a = str(bo["approval"]).lower()
            approval = None if a in ("none", "never") else a
        if approval == "bypass":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        args: list[str] = []
        if approval == "approve-for-me":
            args.append("--approve-for-me")
        elif sandbox:
            args += ["-s", sandbox]
        return args

    def _exec_args(self, spec: PairSpec, *, model: str | None, effort: str | None,
                   permission_mode: str | None) -> list[str]:
        exe = codex_executable()
        cwd = spec.cwd or os.getcwd()
        args = [exe, "exec", "--json", "--skip-git-repo-check", "-C", cwd,
                "--ignore-user-config", "--thread-source", THREAD_SOURCE]
        args += config_readd_args()
        slug = self._model_for(spec, model)
        args += ["-m", slug]
        eff = effort if effort is not None else spec.effort
        if eff:
            args += ["-c", f"model_reasoning_effort={_toml_literal(str(eff))}"]
        if spec.context_window == "1m":
            # The documented recipe is window=1,000,000 / auto-compact=900,000
            # (what the Codex app writes). The server clamps the window to the
            # model's ceiling (828,400 usable on the 5.6 family), which would
            # leave a 900k compact threshold unreachable — so the threshold is
            # derived from the model's EFFECTIVE max window when the cache is
            # readable (90% of it, reachable whether Codex compares against raw
            # or effective counts — unverified which). Historian catch.
            args += ["-c", f"model_context_window={CODEX_1M_CONFIG['model_context_window']}"]
            cw = context_windows(slug)
            limit = int(cw[1] * 0.9) if cw and cw[1] else CODEX_1M_CONFIG["model_auto_compact_token_limit"]
            args += ["-c", f"model_auto_compact_token_limit={limit}"]
        bo = spec.backend_options or {}
        for k, v in (bo.get("config") or {}).items():
            args += ["-c", f"{k}={_toml_literal(v)}"]
        args += self.permission_args(permission_mode or spec.permission_mode, bo)
        for d in spec.extra_dirs or []:
            args += ["--add-dir", d]
        for extra in bo.get("args") or []:
            args.append(str(extra))
        return args

    # ---- subprocess + logging --------------------------------------------

    def _run(self, args: list[str], spec: PairSpec, *, timeout_seconds: int | None,
             task_label: str, log: bool,
             on_event: Callable[[dict], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             task_id: str | None = None,
             stdin_text: str | None = None,
             ) -> tuple[list[dict], str, int | None, int]:
        """Spawn codex, stream stdout JSONL, write main.log lines as items
        arrive, honor should_stop (tree-kill) and the hard timeout (tree-kill +
        CommandTimeout). stdin is CLOSED (DEVNULL) unless ``stdin_text`` is
        given, in which case it is written whole and closed immediately (the
        prompt path — exec proceeds on EOF). Returns (events, stderr, exit
        code, duration_ms)."""
        log_dir = logs_dir() / spec.name
        log_dir.mkdir(parents=True, exist_ok=True)
        main_log = log_dir / "main.log"
        counter = ToolCounter(index_path=log_dir / "main.idx.json") if log else None
        line_count = self._count_lines(main_log)
        start_line = line_count + 1
        # Item ids seen so we can tag completions with the same T-N.
        tags: dict[str, str] = {}
        run_id = task_id or uuid.uuid4().hex

        log_lock = threading.Lock()

        def _log(line: str) -> None:
            nonlocal line_count
            if not log:
                return
            line = line.replace("\r", "").replace("\n", " / ")  # exactly one physical line
            try:
                with log_lock:
                    with open(main_log, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    line_count += 1
            except Exception:
                pass

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "cwd": spec.cwd or None,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        t0 = time.monotonic()
        started_at = datetime.utcnow()
        try:
            proc = subprocess.Popen(args, **popen_kwargs)
        except OSError as e:
            raise CLIError(f"could not start codex ({args[0]}): {e}")
        if stdin_text is not None:
            # Write the whole prompt and close — from a thread, so a prompt
            # larger than the pipe buffer can't deadlock against our readers.
            def _feed() -> None:
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(stdin_text.encode("utf-8"))
                    proc.stdin.flush()
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdin.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
            threading.Thread(target=_feed, daemon=True).start()
        with _INFLIGHT_LOCK:
            _INFLIGHT[spec.name] = {"proc": proc, "task_id": task_id, "started_at": started_at,
                                    "last_activity": started_at, "label": task_label}
        if log:
            ts = datetime.now().strftime("%H:%M:%S")
            _log(f"[{ts}] === CODEX TURN START (pair={spec.name}, model={self._model_for(spec)}, "
                 f"thread={spec.session_id[:8] if spec.session_id else 'new'}) ===")

        events: list[dict] = []
        stderr_chunks: list[str] = []
        q_lock = threading.Lock()

        def _touch() -> None:
            with _INFLIGHT_LOCK:
                info = _INFLIGHT.get(spec.name)
                if info is not None and info.get("proc") is proc:
                    info["last_activity"] = datetime.utcnow()

        def _reader() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    with q_lock:
                        events.append(ev)
                    _touch()
                    if log:
                        for out_line in self._format_event(ev, counter, tags, detail_dir=log_dir, run_id=run_id):
                            _log(out_line)
                    if on_event is not None:
                        try:
                            on_event(ev)
                        except Exception:
                            pass
                except Exception:
                    pass

        def _err_reader() -> None:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                try:
                    s = raw.decode("utf-8", errors="replace")
                    if "Reading additional input from stdin" in s:
                        continue
                    stderr_chunks.append(s)
                    if log and _DENIAL_RE.search(s):
                        ts = datetime.now().strftime("%H:%M:%S")
                        _log(f"[{ts}] [sandbox/approval DENIED] {s.strip()[:300]}")
                except Exception:
                    pass

        rt = threading.Thread(target=_reader, daemon=True)
        et = threading.Thread(target=_err_reader, daemon=True)
        rt.start()
        et.start()
        deadline = (t0 + timeout_seconds) if timeout_seconds else None
        killed_reason: str | None = None
        last_stop_check = 0.0
        while proc.poll() is None:
            now = time.monotonic()
            if should_stop is not None and now - last_stop_check >= 1.0:
                last_stop_check = now
                try:
                    if should_stop():
                        killed_reason = "stopped"
                        _tree_kill(proc)
                        break
                except Exception:
                    pass
            if deadline is not None and now >= deadline:
                killed_reason = "timeout"
                _tree_kill(proc)
                break
            time.sleep(0.2)
        rt.join(timeout=5)
        et.join(timeout=5)
        code = proc.poll()
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(spec.name, {}).get("proc") is proc:
                _INFLIGHT.pop(spec.name, None)
        dur_ms = int((time.monotonic() - t0) * 1000)
        stderr = "".join(stderr_chunks)
        if log:
            ts = datetime.now().strftime("%H:%M:%S")
            if killed_reason:
                _log(f"[{ts}] === TURN {killed_reason.upper()} ({dur_ms}ms) ===")
            elif not any(e.get("type") in ("turn.completed", "turn.failed") for e in events):
                _log(f"[{ts}] === TURN ERROR ({dur_ms}ms) exit={code} ===")
        if killed_reason == "timeout":
            raise CommandTimeout(spec.name, int(timeout_seconds or 0))
        if killed_reason == "stopped":
            raise CLIError("turn stopped by pair_stop (codex process tree-killed; the thread "
                           "resumes cleanly on the next send)", stderr=stderr)
        # Stash the log range on the events list for _build_send_result.
        events.append({"type": "_log_scope", "log_path": str(main_log),
                       "start_line": start_line, "end_line": line_count})
        return events, stderr, code, dur_ms

    @staticmethod
    def _count_lines(p: Path) -> int:
        if not p.exists():
            return 0
        try:
            with open(p, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    @staticmethod
    def _format_event(ev: dict, counter: ToolCounter | None, tags: dict[str, str], *,
                      detail_dir: Path | None = None, run_id: str | None = None) -> list[str]:
        """Codex ``--json`` event → main.log lines (T-N tagged like Claude's)."""
        ts = datetime.now().strftime("%H:%M:%S")
        t = ev.get("type")
        out: list[str] = []
        if t == "thread.started":
            out.append(f"[{ts}] === THREAD {str(ev.get('thread_id'))[:8]} ===")
        elif t == "item.started" or t == "item.completed":
            it = ev.get("item") or {}
            ity = it.get("type")
            iid = str(it.get("id") or "")
            # One PHYSICAL line per log line — a multi-line reply would
            # otherwise desync the recorded line ranges (Astra catch).
            if ity == "agent_message":
                if t == "item.completed":
                    txt = (it.get("text") or "").strip().replace("\r", "").replace("\n", " / ")
                    if txt:
                        out.append(f"[{ts}] [text] {txt[:400]}")
            elif ity == "reasoning":
                if t == "item.completed":
                    txt = str(it.get("text") or it.get("summary") or "").replace("\n", " / ")
                    if txt:
                        out.append(f"[{ts}] [thinking] {txt[:300]}")
            elif ity == "error":
                out.append(f"[{ts}] [error] {str(it.get('message')).replace(chr(10), ' / ')[:300]}")
            else:
                # command_execution / file_change / mcp_tool_call / web_search …
                if t == "item.started":
                    tag = counter.next_id_for(iid, tool_name=str(ity)) if counter else "T-?"
                    tags[iid] = tag
                    out.append(f"[{ts}] [{tag}] [tool_use] {ity}({CodexAdapter._item_preview(it)})")
                else:
                    # Codex item IDs can repeat in the next exec invocation.
                    # Only this run's map may correlate a completion.
                    tag = tags.get(iid, "T-?")
                    if tag == "T-?" and counter:
                        tag = counter.next_id_for(iid, tool_name=str(ity))
                        tags[iid] = tag
                        out.append(f"[{ts}] [{tag}] [tool_use] {ity}({CodexAdapter._item_preview(it)})")
                    status = it.get("status")
                    err = " ERR" if (status not in (None, "completed") or (it.get("exit_code") not in (None, 0))) else ""
                    res = it.get("aggregated_output")
                    if res is None:
                        res = json.dumps({k: v for k, v in it.items() if k not in ("id", "type")}, default=str)
                    preview = str(res).replace("\n", " / ")[:160]
                    exit_s = f"exit {it.get('exit_code')} " if it.get("exit_code") is not None else ""
                    out.append(f"[{ts}] [{tag}] [tool_result{err}] {exit_s}{preview}")
                if detail_dir is not None and tag != "T-?":
                    try:
                        save_codex_item(detail_dir, tag, it, completed=t == "item.completed", run_id=run_id)
                    except OSError as exc:
                        out.append(f"[{ts}] [tool detail unavailable] {type(exc).__name__}")
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            out.append(f"[{ts}] === TURN COMPLETED (in {u.get('input_tokens', 0):,} / cached "
                       f"{u.get('cached_input_tokens', 0):,} / out {u.get('output_tokens', 0):,} "
                       f"/ reasoning {u.get('reasoning_output_tokens', 0):,} tokens) ===")
        elif t == "turn.failed":
            out.append(f"[{ts}] === TURN FAILED: {str((ev.get('error') or {}).get('message'))[:300]} ===")
        return out

    @staticmethod
    def _item_preview(it: dict) -> str:
        ity = it.get("type")
        if ity == "command_execution":
            cmd = str(it.get("command") or "")
            # Drop the PowerShell wrapper prefix (and its quoting) for readability.
            cmd = re.sub(r'^"?[A-Za-z]:\\[^"]*powershell\.exe"?\s+-Command\s+', "", cmd).strip()
            if len(cmd) >= 2 and cmd[0] == cmd[-1] and cmd[0] in ('"', "'"):
                cmd = cmd[1:-1]
            return cmd[:140]
        if ity == "file_change":
            ch = it.get("changes") or []
            return ", ".join(f"{c.get('kind')} {os.path.basename(str(c.get('path') or ''))}" for c in ch)[:140]
        return json.dumps({k: v for k, v in it.items() if k not in ("id", "type", "status")}, default=str)[:140]

    # ---- result assembly -------------------------------------------------

    def _build_send_result(self, spec: PairSpec, events: list[dict], stderr: str,
                           code: int | None, dur_ms: int, started_epoch: float, *,
                           model_used: str, permission_level: str) -> SendResult:
        scope = next((e for e in events if e.get("type") == "_log_scope"), {})
        reply = ""
        errors: list[str] = []
        for e in events:
            if e.get("type") == "item.completed":
                it = e.get("item") or {}
                if it.get("type") == "agent_message":
                    reply = str(it.get("text") or "")
                elif it.get("type") == "error":
                    errors.append(str(it.get("message") or ""))
        failed = next((e for e in events if e.get("type") == "turn.failed"), None)
        completed = next((e for e in events if e.get("type") == "turn.completed"), None)
        usage = (completed or {}).get("usage") or {}

        # Context: prefer the rollout's token_count (effective window + last
        # call size); fall back to turn usage vs the cache's window.
        rollout = self.transcript_path(spec)
        tc = read_last_token_count(rollout)
        info = (tc or {}).get("info") or {}
        last = info.get("last_token_usage") or {}
        window = int(info.get("model_context_window") or 0)
        if not window:
            cw = context_windows(model_used)
            window = (cw[1] if spec.context_window == "1m" else cw[0]) if cw else 258_400
        used = int(last.get("input_tokens") or usage.get("input_tokens") or 0)
        pct = (used / window * 100) if window else 0.0
        warning = None
        if pct >= STRONG_WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct:.0f}%). Context near "
                       f"limit — STRONGLY consider pair_compact('{spec.name}') (Codex will "
                       f"auto-compact otherwise); or recreate with context_window='1m'.")
        elif pct >= WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(window)} ({pct:.0f}%). Consider "
                       f"pair_compact('{spec.name}') to free context (Codex compaction takes "
                       f"no steering text).")

        notes: list[str] = []
        denials: list[PermissionDenial] = []
        for line in (stderr or "").splitlines():
            if _DENIAL_RE.search(line):
                tool = "apply_patch" if "patch" in line.lower() else "exec"
                denials.append(PermissionDenial(tool_name=tool,
                                                tool_input={"detail": line.strip()[:300]}))
        # A shell command that the SANDBOX blocked fails with an OS
        # permission error in its own output (no router-level "rejected"
        # line): "Access to the path '…' is denied" (PowerShell), "Permission
        # denied" / "Operation not permitted" (POSIX). Inside a workspace-
        # write sandbox that is the sandbox speaking — report it as a denial
        # so the PAIR HANDOFF fires instead of a silent "BLOCKED" reply.
        if permission_level != "unrestricted":
            for e in events:
                if e.get("type") != "item.completed":
                    continue
                it = e.get("item") or {}
                if it.get("type") != "command_execution":
                    continue
                if it.get("exit_code") in (None, 0) and it.get("status") == "completed":
                    continue
                out = str(it.get("aggregated_output") or "")
                if _OS_DENIED_RE.search(out):
                    cmd = self._item_preview(it)
                    denials.append(PermissionDenial(
                        tool_name="exec", tool_use_id=str(it.get("id") or "") or None,
                        tool_input={"command": cmd[:200], "detail": _OS_DENIED_RE.search(out).group(0)[:200]},
                    ))
        if permission_level in ("workspace", "auto") and _READONLY_DEGRADE_RE.search(stderr or ""):
            notes.append(
                "the Codex sandbox ran READ-ONLY although this pair's level allows workspace "
                "writes — on Windows this means `[windows] sandbox = \"elevated\"` is missing "
                "from ~/.codex/config.toml (open the Codex app once, or add it by hand)."
            )
        guardian: list[str] = []
        if permission_level == "auto":
            for v in guardian_verdicts_since(started_epoch, parent_thread_id=spec.session_id):
                outcome = str(v.get("outcome") or "?")
                rat = str(v.get("rationale") or v.get("raw") or "")[:200]
                risk = v.get("risk_level")
                guardian.append(f"{outcome}" + (f" (risk {risk})" if risk else "") + (f" — {rat}" if rat else ""))
                if outcome.lower() == "deny":
                    denials.append(PermissionDenial(tool_name="guardian-review",
                                                    tool_input={"rationale": rat}))

        safety_signal: str | None = None
        safety_kind: str | None = None
        err_text = " ".join(errors + ([str((failed or {}).get("error", {}).get("message") or "")] if failed else []))
        rl = (tc or {}).get("rate_limits") or {}
        if failed or (code not in (0, None) and not completed):
            if _MODEL_UNSUPPORTED_RE.search(err_text):
                safety_signal = (
                    f"the Codex model '{model_used}' is not available on this plan/account: "
                    f"{err_text.strip()[:200]}. Switch with pair_update(name, model='sol'|'terra'|'luna')."
                )
                safety_kind = "model_unavailable"
            elif _RATE_LIMIT_RE.search(err_text) or rl.get("rate_limit_reached_type"):
                safety_signal = f"usage limit reached on the ChatGPT plan: {err_text.strip()[:200]}"
                safety_kind = "usage_limit"
            else:
                safety_signal = f"turn failed: {err_text.strip()[:300] or (stderr or '').strip()[-300:]}"
                safety_kind = "error"
        elif rl.get("rate_limit_reached_type"):
            safety_signal = f"the plan's rate limit is reached ({rl.get('rate_limit_reached_type')})"
            safety_kind = "usage_limit"

        if not reply and safety_signal:
            reply = f"(no reply — {safety_signal})"

        return SendResult(
            name=spec.name,
            response=reply,
            session_id=spec.session_id,
            model_used=model_used,
            cost_usd=None,
            duration_ms=dur_ms,
            permission_denials=denials,
            context=ContextStatus(tokens_used=used, tokens_max=window, percent=pct, warning=warning),
            cache_read_tokens=int(usage.get("cached_input_tokens") or 0),
            log_path=scope.get("log_path"),
            log_line_start=scope.get("start_line"),
            log_line_end=scope.get("end_line"),
            safety_signal=safety_signal,
            safety_kind=safety_kind,
            backend="codex",
            plan_usage=plan_usage_text(rl),
            guardian_notes=guardian,
            notes=notes,
        )
