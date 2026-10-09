"""Interim-turn triage (v0.16.0).

An INTERIM turn is a self-woken turn that ends while background work is
still pending: the pair reports progress (or a problem) and will wake again.
A parent following the pair with ``wait.py <pair>`` should learn whether an
interim needs its attention without reading every one. Solicited replies are
never triaged — the parent reads those directly (design rev. C).

Sources, per the ``interim_triage`` setting:
- "auto" (default): the CLI's own ``post_turn_summary`` event for the turn
  (``status_category`` / ``status_detail`` / ``needs_action``, emitted on
  every turn by CLI 2.1.280 — a classifier the CLI already ran, free). Only
  when it is missing or unusable, the Haiku fallback below.
- "haiku": always the isolated Haiku classifier.
- "off": none.

The Haiku call is isolated on purpose (2026-09-25 probe): no session, no MCP,
no tools, no setting sources, and ``--system-prompt`` REPLACING the default
prompt — 629 input tokens / $0.0019 instead of 6,790 / $0.0148 with the
default prompt. The prompt goes on stdin (variadic flags like ``--tools``
swallow a trailing positional prompt). It runs on ONE daemon worker thread,
never the runtime's reader thread, from an empty temp cwd with
CREATE_NO_WINDOW, and is killed by PID after ``HAIKU_TIMEOUT_S``. Its reply
arrives fenced (```json ...```), so parsing is tolerant. At a usage limit the
CLI returns the limit notice as a SUCCESS result — detected, the heuristic
used instead, and Haiku skipped for ``USAGE_LIMIT_BACKOFF_S``.

Results go ONLY into the activity file's window_turns entry
(``ActivityWriter.set_triage``) — terminal task files are never rewritten.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

MODES = ("auto", "haiku", "off")
HAIKU_TIMEOUT_S = 60
USAGE_LIMIT_BACKOFF_S = 15 * 60
_REPLY_CHARS = 6000

_SYSTEM_PROMPT = (
    "You triage progress reports from a coding agent that works in the background for a "
    "parent agent. Reply with ONE JSON object and nothing else: "
    '{"importance": 0-3, "needs_attention": true|false, "reason": "<=120 chars", '
    '"summary": "<=160 chars"}. importance: 0 routine noise, 1 normal progress, '
    "2 notable result or decision taken, 3 blocked / asks the parent something / "
    "error. needs_attention is true only if the parent should act or answer before the "
    "agent can usefully continue."
)

_QUESTION_RE = re.compile(
    r"\b(should i|shall i|do you want|would you like|which (?:one|option)|please (?:confirm|"
    r"approve|decide|advise)|need your|waiting (?:for|on) (?:you|your)|blocked|can(?:no|')t "
    r"proceed|decision)\b", re.IGNORECASE)


def current_mode() -> str:
    """The ``interim_triage`` setting ("auto" when unset/unreadable). Reads
    the defaults file — call from tool/send threads, never the reader."""
    try:
        from claude_squared.settings import load_defaults
        mode = load_defaults().interim_triage
    except Exception:
        mode = None
    return mode if mode in MODES else "auto"


def from_summary(summary: dict | None) -> dict | None:
    """Triage from the CLI's post_turn_summary, or None when it is unusable
    (absent, or neither a category nor a detail)."""
    if not isinstance(summary, dict):
        return None
    category = str(summary.get("status_category") or "").strip().lower()
    detail = str(summary.get("status_detail") or "").strip()
    action = str(summary.get("needs_action") or "").strip()
    if not category and not detail:
        return None
    needs = bool(action) or category == "blocked"
    importance = 3 if needs else (2 if category == "review_ready" else 1)
    return {"importance": importance, "needs_attention": needs,
            "reason": (action or category or "")[:120], "summary": detail[:160],
            "category": category or None, "source": "cli_summary"}


def heuristic(reply: str) -> dict:
    """Last resort: a question/blocker in the reply's last paragraph."""
    tail = (reply or "").strip().split("\n\n")[-1][-600:]
    asks = "?" in tail or bool(_QUESTION_RE.search(tail))
    first = next((ln.strip() for ln in (reply or "").splitlines() if ln.strip()), "")
    return {"importance": 3 if asks else 1, "needs_attention": asks,
            "reason": "asks something / reports a blocker" if asks else "progress report",
            "summary": first[:160], "source": "heuristic"}


def parse_classifier_reply(text: str) -> dict | None:
    """Tolerant parse of the classifier's reply: fenced or bare JSON, the
    first {...} object found; None when nothing usable."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or "needs_attention" not in data:
        return None
    try:
        importance = max(0, min(3, int(data.get("importance", 1))))
    except (TypeError, ValueError):
        importance = 1
    return {"importance": importance, "needs_attention": bool(data.get("needs_attention")),
            "reason": str(data.get("reason") or "")[:120],
            "summary": str(data.get("summary") or "")[:160], "source": "haiku"}


# ---- worker ------------------------------------------------------------------

_QUEUE: "queue.Queue[tuple[Any, str | None, dict | None, str]]" = queue.Queue()
_WORKER: list[threading.Thread] = []
_WORKER_LOCK = threading.Lock()
_BACKOFF_UNTIL = [0.0]


def submit(writer: Any, task_id: str | None, summary: dict | None, reply: str, *,
           mode: str = "auto") -> None:
    """Triage one self-woken interim. Memory-only on the caller's thread
    (the runtime's reader): the summary path is a dict lookup; anything
    needing Haiku is queued for the single worker."""
    if not task_id or mode == "off":
        return
    if mode == "auto":
        tri = from_summary(summary)
        if tri is not None:
            writer.set_triage(task_id, tri)
            return
    _QUEUE.put((writer, task_id, summary, reply or ""))
    with _WORKER_LOCK:
        if not _WORKER or not _WORKER[0].is_alive():
            t = threading.Thread(target=_worker_loop, daemon=True, name="pair-interim-triage")
            _WORKER[:] = [t]
            t.start()


def _worker_loop() -> None:
    while True:
        writer, task_id, summary, reply = _QUEUE.get()
        try:
            tri = None
            if time.monotonic() >= _BACKOFF_UNTIL[0]:
                tri = classify_with_haiku(reply)
            if tri is None:
                tri = heuristic(reply)
            writer.set_triage(task_id, tri)
        except Exception:
            try:
                writer.set_triage(task_id, heuristic(reply))
            except Exception:
                pass


def _claude_cli() -> str:
    return os.environ.get("CLAUDE_PAIR_CLI_PATH") or shutil.which("claude") or "claude"


def classify_with_haiku(reply: str) -> dict | None:
    """One isolated Haiku call (worker thread only). None on any failure —
    timeout, bad JSON, a usage-limit notice (which also starts the backoff)."""
    from claude_squared.adapters.claude import _USAGE_LIMIT_RE
    # --no-chrome: CLAUDE_CODE_ENABLE_CFC in the inherited environment would
    # otherwise connect Claude in Chrome even here (v0.17.0, as for pairs).
    argv = [_claude_cli(), "-p", "--model", "haiku", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--no-chrome", "--tools", "",
            "--setting-sources", "", "--system-prompt", _SYSTEM_PROMPT,
            "--output-format", "json"]
    prompt = "Progress report to triage:\n\n" + (reply or "")[-_REPLY_CHARS:]
    kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryDirectory(prefix="cs-triage-", ignore_cleanup_errors=True) as cwd:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, **kwargs)
        except OSError:
            return None
        try:
            out, _err = proc.communicate(prompt.encode("utf-8"), timeout=HAIKU_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return None
    try:
        data = json.loads(out.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    text = str(data.get("result") or "")
    if _USAGE_LIMIT_RE.search(text):
        _BACKOFF_UNTIL[0] = time.monotonic() + USAGE_LIMIT_BACKOFF_S
        return None
    if data.get("is_error"):
        return None
    tri = parse_classifier_reply(text)
    if tri is not None:
        tri["cost_usd"] = float(data.get("total_cost_usd") or 0.0)
    return tri


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the classifier WE spawned, by its PID (and its children)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
