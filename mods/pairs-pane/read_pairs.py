"""Read-only, stdlib snapshot for the Pairs pane. Never imports the package.

Use the installed wait.py's merged activity view and OS PID checks verbatim:
the pane must not call a queued task, pending background or expected wake idle.
No process is stopped, and no registry/task/activity/watch file is changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import heapq
import json
import os
from pathlib import Path
import re
import runpy
import sys
import sqlite3
import time

JSON_LIMIT = 128 * 1024
TASK_LIMIT = 64 * 1024
BYTE_BUDGET = 4 * 1024 * 1024
FILE_BUDGET = 128
SCAN_BUDGET = 2048
TASK_BUDGET = 128
ROW_BUDGET = 64
WATCH_BUDGET = 24
TASK_READ_BUDGET = 48
ASYNC_SCAN_SECONDS = .15
ASYNC_POSIX_SCAN_CAP = 50000
RECENT_SECONDS = 2 * 60 * 60
WATCH_MAX_AGE = 7 * 24 * 3600


class Reads:
    """Hard I/O limits, including races where a file grows after stat()."""
    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.partial = False
        self.memo = {}
        self.issues = set()
        self.budget_limited = False
        self.task_discovery_limited = False

    def json(self, path, limit=JSON_LIMIT):
        path = Path(path)
        if path in self.memo:
            return self.memo[path]
        value = None
        try:
            size = path.stat().st_size
            if size > limit or size > BYTE_BUDGET - self.bytes or self.files >= FILE_BUDGET:
                self.partial = True
                self.budget_limited = True
                self.issues.add(str(path))
                self.memo[path] = None
                return None
            self.files += 1
            with path.open("rb") as stream:
                data = stream.read(min(limit, BYTE_BUDGET - self.bytes))
            self.bytes += len(data)
            value = json.loads(data)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.partial = True
            self.issues.add(str(path))
        self.memo[path] = value
        return value

    def entries(self, directory, limit=SCAN_BUDGET):
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= limit:
                        self.partial = True
                        self.budget_limited = True
                        self.issues.add(str(directory))
                        break
                    yield entry
        except FileNotFoundError:
            pass
        except OSError:
            self.partial = True
            self.issues.add(str(directory))

    def task_entries(self, directory):
        # Windows DirEntry caches stat metadata from enumeration. A 2,048-entry
        # ceiling hid current tasks behind old history; bound elapsed work instead.
        deadline = time.monotonic() + ASYNC_SCAN_SECONDS
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if time.monotonic() >= deadline or (os.name != "nt" and index >= ASYNC_POSIX_SCAN_CAP):
                        self.partial = self.budget_limited = self.task_discovery_limited = True
                        self.issues.add(str(directory))
                        break
                    yield entry
        except FileNotFoundError:
            pass
        except OSError:
            self.partial = self.task_discovery_limited = True
            self.issues.add(str(directory))


def task_summaries(pairs, names, references, reader, reads, now, cache, show_all):
    """Recent metadata plus known-running task IDs; never retain result/prompt blobs."""
    folder = pairs / "async"
    previous = cache.get("tasks", {}) if isinstance(cache, dict) else {}
    if not isinstance(previous, dict):
        previous = {}
    candidates = {}
    # Active activity records and the prior refresh keep long-running tasks
    # eligible even when their file has not changed within the recent window.
    references = set(references)
    for filename, old in previous.items():
        summary = old.get("summary", {}) if isinstance(old, dict) else {}
        if summary.get("status") == "running" and (show_all or summary.get("pair") in names):
            references.add(filename[:-5] if filename.endswith(".json") else filename)
    if len(references) > TASK_BUDGET:
        reads.partial = True
        reads.budget_limited = True
        reads.task_discovery_limited = True
    for task_id in list(references)[:TASK_BUDGET]:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            continue
        path = folder / (task_id + ".json")
        try:
            candidates[path.name] = (path.stat(), path)
        except OSError:
            pass
    recent = []
    for entry in reads.task_entries(folder):
        if not entry.name.endswith(".json") or entry.name.startswith("."):
            continue
        try:
            stat = entry.stat()
        except OSError:
            reads.partial = True
            continue
        if entry.name in candidates:
            continue
        if stat.st_mtime < now.timestamp() - RECENT_SECONDS:
            continue
        recent.append((stat, Path(entry.path)))
    if str(folder) in reads.issues:
        reads.task_discovery_limited = True
    room = max(0, TASK_BUDGET - len(candidates))
    if len(recent) > room:
        reads.partial = True
        reads.budget_limited = True
        reads.task_discovery_limited = True
    for stat, path in heapq.nlargest(room, recent, key=lambda item: item[0].st_mtime_ns):
        candidates[path.name] = (stat, path)
    summaries, saved = [], {}
    cache_bytes = 0
    initial_reads = reads.files
    for filename, (stat, path) in candidates.items():
        old = previous.get(filename)
        if (isinstance(old, dict) and old.get("mtime") == str(stat.st_mtime_ns) and old.get("size") == stat.st_size
                and isinstance(old.get("summary"), dict)):
            summary = old["summary"]
        else:
            # Leave capacity for the selected pairs' activity and window data.
            if reads.files - initial_reads >= TASK_READ_BUDGET or reads.files >= FILE_BUDGET - 32:
                reads.budget_limited = True
                reads.task_discovery_limited = True
                continue
            data = reads.json(path, TASK_LIMIT)
            if not isinstance(data, dict):
                reads.task_discovery_limited = True
                continue
            summary = summarize_task(data, filename, reader)
        # Nanosecond timestamps exceed JavaScript's exact integer range; the
        # cache crosses the mod's JSON boundary between refreshes.
        saved_item = {"mtime": str(stat.st_mtime_ns), "size": stat.st_size, "summary": summary}
        item_bytes = len(json.dumps(saved_item).encode("utf-8"))
        if item_bytes > 8192:
            reads.partial = True
            continue
        if cache_bytes + item_bytes <= 256 * 1024:
            saved[filename] = saved_item
            cache_bytes += item_bytes
        if show_all or summary.get("pair") in names:
            summaries.append(summary)
    return summaries, {"tasks": saved}


def summarize_task(data, filename, reader):
    summary = reader["_av_task_summary"](data)
    summary.update(filename=filename, outcome=task_outcome(data))
    result = data.get("result")
    if isinstance(result, dict) and isinstance(result.get("model_used"), str):
        summary["model_used"] = result["model_used"][:160]
    context = result.get("context") if isinstance(result, dict) else None
    if isinstance(context, dict):
        summary["result_context"] = {key: context.get(key) for key in ("tokens_used", "tokens_max")}
    return summary


def compact_row(name, spec, watches=(), skipped=False, busy=False, undecided=False):
    return {"name": name, "backend": spec.get("backend") or "unknown", "model": spec.get("model") or "unknown",
            "status": "busy" if busy else "unknown" if undecided else "idle", "elapsed_s": None,
            "finished_at": None, "finished_ago_s": None, "outcome": None, "detail": None,
            "context": {"used": None, "window": None, "percent": None, "alert_at": None, "above_alert": False},
            "watches": list(watches), "partial": skipped, "details_skipped": skipped, "compact": True}


def activity_hints(pairs, names, reads):
    """Bounded stat-only pass; old idle owners never consume JSON read slots."""
    hints, paths = {}, {}
    remaining = SCAN_BUDGET
    for index, name in enumerate(names):
        files = []
        if remaining <= 0 or index >= SCAN_BUDGET:
            reads.budget_limited = True
            continue
        for entry in reads.entries(pairs / "activity" / name, min(16, remaining)):
            remaining -= 1
            if entry.name == "pair.json" or (entry.name.endswith(".json") and entry.name[:-5].isdigit()):
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, Path(entry.path)))
        files.sort(key=lambda item: item[0], reverse=True)
        paths[name] = [path for _, path in files if path.name != "pair.json"]
        hints[name] = max((at for at, _ in files), default=0)
    return hints, paths


def safe_pair(name):
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and not any(c in name for c in "/\\:\0"))


def tail(path, size=8192):
    """Physical tail only: HH:MM:SS labels cannot order lines across days.

    A log entry can span lines (a multi-line tool result); continuation lines
    are folded into their entry with " / ", as single-line entries already are.
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - size)
            stream.seek(offset)
            data = stream.read(size)
        lines = data.decode("utf-8", "replace").splitlines()
        if offset:
            lines = lines[1:]
        entries = []
        for line in lines:
            line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).rstrip()
            line = "".join(c if c.isprintable() else " " for c in line)
            if not line.strip() or re.match(r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?=== ", line):
                continue
            if line.startswith("["):
                entries.append(line)
            elif entries:  # a continuation; one before the first entry was cut by the tail
                entries[-1] += " / " + line.strip()
        return entries
    except OSError:
        return []


SUB_LAUNCH = re.compile(r"^\[(\d\d:\d\d):\d\d\] \[T-\d+\] \[-> sub-agent #(\d+): ([^\]]+)\] launching: ?(.*)$")
SUB_RETURN = re.compile(r"^\[\d\d:\d\d:\d\d\] \[T-\d+\] \[<- sub-agent #(\d+) returned\](.*)$")
SUB_STARTED = re.compile(r"^\[\d\d:\d\d:\d\d\] \[system:task_started\] local_agent (?:\(\w+\) )?([0-9a-f]{6,}): ?(.*)$")
SUB_ENDED = re.compile(r"^\[\d\d:\d\d:\d\d\] \[system:task_notification\] task ([0-9a-f]{6,}) (?:completed|failed|stopped|killed)")
SUB_SCAN = 256 * 1024


def _project_dir(root, spec):
    cwd = spec.get("cwd")
    return root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) if isinstance(cwd, str) else None


def subagents(root, name):
    """A Claude pair's sub-agents, running first then newest: launches and
    returns from the main log's recent tail, finished ones from their logs."""
    logs = root / "pairs" / "logs" / name
    found = {}
    try:
        names = [entry.name for entry in os.scandir(logs)]
    except OSError:
        names = []
    for filename in names:
        match = re.fullmatch(r"subagent-(\d+)-(.+)\.log", filename)
        if match:
            ident = int(match.group(1))
            found[ident] = {"id": ident, "type": match.group(2), "description": "", "status": "done", "at": None}
    try:
        with (logs / "main.log").open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - SUB_SCAN))
            text = stream.read(SUB_SCAN).decode("utf-8", "replace")
    except OSError:
        text = ""
    waiting = []  # launches whose task_started line has not been seen yet
    for line in text.splitlines():
        launch = SUB_LAUNCH.match(line)
        if launch:
            # Numbers restart when a pair is cleared: the newest launch wins.
            ident = int(launch.group(2))
            entry = {"id": ident, "type": launch.group(3), "description": launch.group(4).strip(),
                     "status": "running", "at": launch.group(1)}
            if (logs / f"subagent-{ident}-{entry['type']}.log").is_file() and ident in found:
                entry["status"] = found[ident]["status"]
            found[ident] = entry
            waiting.append(entry)
            continue
        started = SUB_STARTED.match(line)
        if started and waiting:
            # Parallel launches can log their starts out of order; match by description.
            entry = next((e for e in waiting if e["description"] == started.group(2).strip()), waiting[0])
            entry["agent"] = started.group(1)
            waiting.remove(entry)
            continue
        returned = SUB_RETURN.match(line)
        # A background launch "returns" at once; it ends with its task notification.
        if returned and int(returned.group(1)) in found and "Async agent launched" not in returned.group(2):
            found[int(returned.group(1))]["status"] = "done"
            continue
        ended = SUB_ENDED.match(line)
        if ended:
            for entry in found.values():
                if entry.get("agent") == ended.group(1):
                    entry["status"] = "done"
    rows = sorted(found.values(), key=lambda e: (e["status"] != "running", -e["id"]))
    return [{k: v for k, v in e.items() if k != "agent"} for e in rows[:12]], found


def _local(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"


def sub_transcript(root, name, spec, ident, found):
    """The sub-agent's own transcript, named in its extracted log's header or
    found by the agent id its launch reported. Only paths inside the pair's
    own project folder's subagents directories are accepted."""
    entry = found.get(ident) or {}
    project = _project_dir(root, spec)
    agent = entry.get("agent")
    extracted = root / "pairs" / "logs" / name / f"subagent-{ident}-{entry.get('type', '')}.log"
    if extracted.is_file():
        try:
            with extracted.open("r", encoding="utf-8", errors="replace") as stream:
                head = stream.read(4096)
            match = re.search(r"=== source jsonl: (.+?\.jsonl) ===", head)
            path = Path(match.group(1)) if match else None
        except OSError:
            path = None
        # Numbers restart after a clear: an extracted log of an earlier run
        # with the same number names another agent's transcript.
        if path is not None and isinstance(agent, str) and not path.name.startswith(f"agent-{agent}"):
            extracted = None
        elif (path is not None and project is not None and path.parent.name == "subagents"
                and path.parent.parent.parent.resolve() == project.resolve()):
            return path, extracted
    else:
        extracted = None
    if project is not None and isinstance(agent, str):
        # The pair's session may have rotated since the launch: any session's folder.
        matches = sorted(project.glob(f"*/subagents/agent-{agent}*.jsonl"))
        if matches:
            return matches[0], extracted
    return None, extracted


def _jsonl_lines(path, size=64 * 1024):
    """A running sub-agent's transcript tail as main.log-style lines, tagged by
    tool-use id so an entry can be opened in full."""
    try:
        with path.open("rb") as stream:
            end = stream.seek(0, 2)
            stream.seek(max(0, end - size))
            data = stream.read(size)
    except OSError:
        return []
    lines = []
    for raw in data.splitlines()[1 if end > size else 0:]:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict):
            continue
        at = _local(event.get("timestamp"))
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                role = "text" if message.get("role") == "assistant" else "sub-user"
                lines.append(f"[{at}] [{role}] " + " / ".join(block["text"].splitlines())[:400])
            elif kind == "tool_use":
                args = json.dumps(block.get("input"), ensure_ascii=False)[:300]
                lines.append(f"[{at}] [{block.get('id')}] [tool_use] {block.get('name')}({args})")
            elif kind == "tool_result":
                result = block.get("content")
                text = result if isinstance(result, str) else " ".join(
                    part.get("text", "") for part in result or [] if isinstance(part, dict))
                flag = " ERR" if block.get("is_error") else ""
                lines.append(f"[{at}] [{block.get('tool_use_id')}] [tool_result{flag}] "
                             + " / ".join(str(text).splitlines())[:300])
    return lines[-120:]


def subagent_activity(root, name, spec, ident):
    rows, found = subagents(root, name)
    if ident not in found:
        return None
    path, extracted = sub_transcript(root, name, spec, ident, found)
    entry = found[ident]
    # The sub-agent's own transcript has real times and covers background
    # sub-agents, which never get an extracted log; the log is the fallback.
    if path is not None and path.is_file():
        lines = _jsonl_lines(path)
    elif extracted is not None:
        lines = tail(extracted, 32 * 1024)
    else:
        lines = []
    return {"id": ident, "type": entry.get("type"), "description": entry.get("description", ""),
            "status": entry["status"], "logs": lines}


def task_outcome(data):
    status = data.get("status")
    if status == "failed":
        error = data.get("error") or ""
        if isinstance(error, str):
            if error.startswith("ORPHANED: "):
                return "orphaned"
            if error.startswith("CRASHED: "):
                return "crashed"
    return status


def task_detail(pairs, tasks, reads, parse):
    completed = [t for t in tasks if t.get("status") != "running" and parse(t.get("finished_at"))]
    running = [t for t in tasks if t.get("status") == "running"]
    latest = max(completed, key=lambda t: parse(t["finished_at"])) if completed else None
    current = min(running, key=lambda t: str(t.get("started_at") or "")) if running else None
    def payload(task):
        filename = task.get("filename") if task else None
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            return {}
        data = reads.json(pairs / "async" / filename, TASK_LIMIT)
        return data if isinstance(data, dict) else {}
    data = payload(latest)
    outcome = (latest.get("outcome") or latest.get("status")) if latest else None
    result = data.get("result")
    reply = result.get("response") if isinstance(result, dict) else None
    if not isinstance(reply, str) or outcome in ("orphaned", "failed", "stopped", "crashed"):
        reply = None  # supervision/error text is never a model reply
    message = payload(current).get("message")
    at = parse(latest.get("finished_at")) if latest else None
    return {"reply": reply, "reply_at": at.isoformat() if at else None, "outcome": outcome,
            "working_on": message.splitlines()[0] if isinstance(message, str) and message else None,
            "logs": [], "note": None if reply is not None or outcome in ("orphaned", "failed", "stopped", "crashed")
            else "Latest reply unavailable within the read limits."}


def context_fill(reader, view, spec, defaults):
    """Last activity-file fill and the watcher's threshold, without a model call."""
    context = view.get("context") or {}
    reported = view.get("reported_window") or {}
    window = context.get("window") or reported.get("tokens")
    if (context.get("window_source") == "inferred" and type(reported.get("tokens")) is int
            and reported["tokens"] > 0
            and (reported.get("backend") or "claude") == (context.get("backend") or "claude")):
        window = reported["tokens"]
    used = context.get("used")
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        window = None
    if (not isinstance(used, int) or isinstance(used, bool) or used < 0
            or (window is not None and used > window)):
        used = None
    headroom = defaults.get("context_alert_headroom")
    if not isinstance(headroom, int) or isinstance(headroom, bool) or headroom < 0:
        headroom = None
    # Match pair_status and wait.py's Alert.level, including the persisted
    # Codex compaction limit and per-pair override. The pane marks the current
    # level; unlike wait.py's exit 8 it need not observe a crossing first.
    threshold = reader["context_alert_threshold"](
        window, headroom, context.get("backend") or reported.get("backend") or "claude",
        context.get("auto_compact_token_limit") or reported.get("auto_compact_token_limit"),
        reader["_alert_at"](spec))
    return {"used": used, "window": window,
            "percent": used * 100 / window if used is not None and window else None,
            "alert_at": threshold, "above_alert": bool(threshold and used is not None and used >= threshold)}


def display_model(spec, view, tasks, parse):
    """Resolve floating aliases only from already-read, same-family evidence."""
    requested = spec.get("model") or "unknown"
    if not isinstance(requested, str) or re.search(r"\d", requested):
        return requested
    alias = re.escape(re.sub(r"^claude-", "", requested))
    pattern = (r"gpt-\d+(?:\.\d+)?-" + alias + r"(?:-|$)" if spec.get("backend") == "codex"
               else r"claude-" + alias + r"-\d")
    reported = view.get("reported_window") or {}
    key = reported.get("model_key")
    candidates = []
    if not key or (isinstance(key, str) and key.split("|", 1)[0] == requested):
        candidates.append((parse(reported.get("at")), reported.get("model")))
    candidates.extend((parse(task.get("finished_at")), task.get("model_used")) for task in tasks)
    candidates = [(at, model) for at, model in candidates
                  if at is not None and isinstance(model, str) and re.match(pattern, model)]
    return max(candidates, key=lambda item: item[0])[1] if candidates else requested


def snapshot(home, session=None, show_all=True, expanded=(), cache=None, focus=None):
    pairs = Path(home).expanduser().resolve() / "pairs"
    script = pairs / "wait.py"
    if not script.is_file():
        return {"rows": [], "error": "Start the pair MCP server to install wait.py in this Claude home."}
    reader = runpy.run_path(str(script))
    view_fn = reader["pair_activity_view"]
    parse = reader["_av_parse"]
    now = reader["_av_now"]()
    reads = Reads()
    def view_read(path, **kwargs):
        value = reads.json(path)
        return (value, None) if value is not None else (None, "missing or bounded read")

    view_fn.__globals__["_av_read_json"] = view_read
    proc = reader["_av_proc"]
    pids = {}

    def process(pid):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False, None
        if pid not in pids:
            pids[pid] = proc(pid)
        return pids[pid]

    view_fn.__globals__["_av_proc"] = process
    registry = reads.json(pairs / "registry.json", 2 * 1024 * 1024)
    specs = registry.get("pairs") if isinstance(registry, dict) else None
    error = None
    if not isinstance(specs, dict):
        specs = {}
        error = "Pair registry unavailable; showing observable work only."
    defaults = reads.json(pairs / "defaults.json")
    if not isinstance(defaults, dict):
        defaults = {}
    watches = {}
    watch_paths = []
    for entry in reads.entries(pairs / "watches"):
        path = Path(entry.path)
        if path.suffix != ".json":
            continue
        try:
            modified = entry.stat().st_mtime
            if modified >= now.timestamp() - WATCH_MAX_AGE:
                watch_paths.append((modified, path))
        except OSError:
            pass
    if len(watch_paths) > WATCH_BUDGET:
        reads.budget_limited = True
    for _, path in sorted(watch_paths, reverse=True)[:WATCH_BUDGET]:
        record = reads.json(path, 8192)
        if not isinstance(record, dict) or record.get("version") != 1 or not safe_pair(record.get("pair")):
            continue
        if not show_all and record.get("session_id") != session:
            continue
        done = reads.json(path.with_suffix(".done"), 8192)
        if record.get("state") == "done" or (isinstance(done, dict) and done.get("state") == "done"
                and done.get("hook_pid") == record.get("hook_pid") and done.get("started_at") == record.get("started_at")):
            continue
        started = parse(record.get("started_at"))
        manager_session = record.get("session_id")
        if started is None or not isinstance(manager_session, str) or not manager_session:
            continue
        alive, born = process(record.get("hook_pid"))
        if alive is False or (born is not None and born > started.timestamp() + 2):
            continue  # gone, or another process reused the watcher PID
        watches.setdefault(record["pair"], []).append({
            "session_id": manager_session, "cwd": record.get("cwd"), "hook_pid": record["hook_pid"],
            "started_at": record["started_at"], "elapsed_s": max(0, (now - started).total_seconds()),
            "verified": alive is True,
        })
    names = set(specs) | set(watches) if show_all else set(watches)
    if show_all:
        names.update(e.name for e in reads.entries(pairs / "activity") if e.is_dir())
    names = sorted(n for n in names if safe_pair(n))
    # Task summaries and stat hints rank work before we spend the shared file
    # budget on activity. Registry-only rows survive every detail limit.
    hints, candidates = activity_hints(pairs, names, reads)
    cutoff = now.timestamp() - RECENT_SECONDS
    activity_busy, activity_references = set(), set()
    primary = sorted(names, key=lambda name: (name not in watches, -hints.get(name, 0), name))
    for name in primary:
        for path in candidates.get(name, []):
            pid = int(path.stem)
            alive, born = process(pid)
            if alive is False or (born is not None and born > hints.get(name, 0) + 2):
                continue
            if reads.files >= FILE_BUDGET - TASK_READ_BUDGET:
                reads.budget_limited = True
                break
            data = reads.json(path)
            if not isinstance(data, dict) or data.get("state", "live") != "live":
                continue
            turn = data.get("turn")
            if turn or data.get("background") or data.get("wake_pending"):
                activity_busy.add(name)
            if isinstance(turn, dict) and isinstance(turn.get("task_id"), str):
                activity_references.add(turn["task_id"])
    tasks, saved = task_summaries(pairs, set(names), activity_references, reader, reads, now, cache, show_all) if names else ([], {})
    tasks_by_pair = {}
    for task in tasks:
        if safe_pair(task.get("pair")):
            tasks_by_pair.setdefault(task["pair"], []).append(task)
    busy = activity_busy | {name for name, items in tasks_by_pair.items() if any(t.get("status") == "running" for t in items)}
    recent_tasks = {name for name, items in tasks_by_pair.items()
                    if any(parse(t.get("finished_at")) and parse(t["finished_at"]).timestamp() >= cutoff for t in items)}
    if show_all:
        names = list(set(names) | busy | recent_tasks)
    selected = busy | set(watches) | recent_tasks | {name for name in names if hints.get(name, 0) >= cutoff}
    names.sort(key=lambda name: (0 if name in busy else 1 if name in watches else 2 if name in selected else 3,
                                  -hints.get(name, 0), name))
    owner_files = {}
    view_fn.__globals__["_av_scan_tasks"] = lambda *args, **kwargs: tasks
    view_fn.__globals__["_av_owner_files"] = lambda root, name: owner_files.get(name, [])
    rows = []
    detail_bytes = 0
    detailed = 0
    for name in names:
        spec = specs.get(name) if isinstance(specs.get(name), dict) else {}
        waiting = watches.get(name, [])
        if name not in selected:
            if name not in specs:
                continue  # forgotten pairs' old activity directories are not idle pairs
            # A global task-history cap does not invalidate a complete, old
            # per-pair activity scan. With no such evidence, retain unknown.
            undecided = (name not in hints or (reads.task_discovery_limited and not candidates.get(name))
                       or str(pairs / "activity" / name) in reads.issues)
            rows.append(compact_row(name, spec, waiting, skipped=undecided, undecided=undecided))
            continue
        nonregistry_work = name in busy or name in recent_tasks
        paths = candidates.get(name, [])
        meta = pairs / "activity" / name / "pair.json"
        required = sum(1 for path in paths + [meta] if path not in reads.memo and path.is_file())
        if (detailed >= ROW_BUDGET or reads.files + required > FILE_BUDGET or reads.bytes >= BYTE_BUDGET
                or str(pairs / "activity" / name) in reads.issues):
            reads.budget_limited = True
            if name in specs or nonregistry_work:
                rows.append(compact_row(name, spec, waiting, skipped=True, busy=name in busy,
                                        undecided=name not in recent_tasks or reads.task_discovery_limited))
            continue
        detailed += 1
        owner_files[name] = paths
        reads.json(meta)
        references = set()
        for path in paths:
            activity = reads.json(path)
            turn = activity.get("turn") if isinstance(activity, dict) else None
            if isinstance(turn, dict) and isinstance(turn.get("task_id"), str):
                references.add(turn["task_id"])
            if name in expanded and isinstance(activity, dict):
                finished_turns = [item for item in activity.get("window_turns", []) if isinstance(item, dict)
                                  and item.get("ended_at") and isinstance(item.get("task_id"), str)]
                if finished_turns:
                    references.add(max(finished_turns, key=lambda item: str(item["ended_at"]))["task_id"])
        known_ids = {task.get("task_id") for task in tasks}
        for task_id in sorted(references - known_ids):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
                continue
            if len(tasks) >= TASK_BUDGET:
                reads.budget_limited = True
                break
            data = reads.json(pairs / "async" / (task_id + ".json"), TASK_LIMIT)
            if isinstance(data, dict):
                summary = summarize_task(data, task_id + ".json", reader)
                tasks.append(summary)
                try:
                    stat = (pairs / "async" / (task_id + ".json")).stat()
                    item = {"mtime": str(stat.st_mtime_ns), "size": stat.st_size, "summary": summary}
                    if len(json.dumps(saved).encode("utf-8")) + len(json.dumps(item).encode("utf-8")) < 256 * 1024:
                        saved.setdefault("tasks", {})[task_id + ".json"] = item
                except OSError:
                    pass
        if any(reads.memo.get(path) is None and str(path) in reads.issues for path in paths + [meta]):
            if name in specs or nonregistry_work:
                rows.append(compact_row(name, spec, waiting, skipped=True, busy=name in busy, undecided=True))
            continue
        view = view_fn(pairs, name, now=now)
        attention = any(t.get("state") in ("orphaned", "finished_detached") for t in view["running_tasks"])
        starts = [parse(item.get("started_at")) for key in ("turns", "running_tasks", "background")
                  for item in view[key]]
        starts += [parse(item.get("since")) for item in view["wake_pending"]]
        starts = [start for start in starts if start is not None]
        started = min(starts) if starts else parse(view.get("window_started_at"))
        context = view.get("context") or {}
        pair_tasks = [task for task in tasks if task.get("pair") == name]
        completions = [(parse(item.get("ended_at") or item.get("finished_at")), item.get("outcome") or item.get("status"))
                       for item in view["window_turns"] + view["background_finished"] + pair_tasks
                       if item.get("status") != "running"]
        completions = [(at, status) for at, status in completions if at is not None]
        finished, outcome = max(completions, key=lambda item: (item[0], item[1] in ("orphaned", "crashed"))) if completions else (None, None)
        if name not in specs and view["verdict"] != "busy" and not (finished and finished.timestamp() >= cutoff):
            continue
        orphaned = any(t.get("state") == "orphaned" for t in view["running_tasks"])
        status = "orphaned" if orphaned else "needs attention" if attention else view["verdict"]
        if status in ("idle", "settling") and outcome in ("orphaned", "failed", "stopped", "crashed"):
            status = outcome
        # Idle Claude runtimes can have no current context record. Reuse a
        # bounded stored SendResult, but never restore pre-compaction usage or
        # Codex cumulative usage. The merged view already keeps old owner reports.
        if (spec.get("backend", "claude") == "claude" and view["verdict"] == "idle"
                and context.get("used") is None and "implausible_used" not in context):
            after = [parse(item.get("at")) for item in view["compactions"]]
            after = [at for at in after if at is not None]
            results = []
            for path in owner_files[name]:
                activity = reads.json(path)
                ctx = activity.get("context") if isinstance(activity, dict) else None
                if isinstance(ctx, dict):
                    results.append((parse(ctx.get("at")), ctx))
            for task in pair_tasks:
                ctx = task.get("result_context")
                if isinstance(ctx, dict):
                    results.append((parse(task.get("finished_at")),
                                    {"used": ctx.get("tokens_used"), "window": ctx.get("tokens_max")}))
            results = [(at, ctx) for at, ctx in results if at is not None and (not after or at >= max(after))
                       and type(ctx.get("used")) is int and type(ctx.get("window")) is int
                       and 0 <= ctx["used"] <= ctx["window"] and ctx["window"] > 0]
            if results:
                at, ctx = max(results, key=lambda item: item[0])
                view["context"] = dict(ctx, backend="claude", at=at.isoformat())
        fill = context_fill(reader, view, spec, defaults)
        # A formerly expanded row may have aged into the collapsed Idle group
        # since the UI's previous snapshot. Gate detail using this snapshot too.
        detail = name in expanded and (not show_all or waiting or attention or view["verdict"] == "busy"
                                      or status in ("orphaned", "crashed")
                                      or (finished and (now - finished).total_seconds() <= RECENT_SECONDS))
        details = task_detail(pairs, pair_tasks, reads, parse) if detail else None
        if details is not None:
            if orphaned:
                details.update(reply=None, outcome="orphaned", note=None)
            details["logs"] = tail(pairs / "logs" / name / "main.log")
            if (spec.get("backend") or "claude") == "claude":
                details["subagents"] = subagents(pairs.parent, name)[0]
                ident = (focus or {}).get(name) if isinstance(focus, dict) else None
                sub = subagent_activity(pairs.parent, name, spec, ident) if type(ident) is int else None
                if sub is not None:
                    details["logs"] = sub.pop("logs")
                    details["focus"] = sub
            size = len(json.dumps(details).encode("utf-8"))
            if detail_bytes + size > 1024 * 1024:
                details = {"reply": None, "reply_at": None, "working_on": None, "logs": [],
                           "note": "Collapse another expanded row to load these details."}
            else:
                detail_bytes += size
        activity_path = str(pairs / "activity" / name)
        affected = any(issue == activity_path or issue.startswith(activity_path + os.sep) for issue in reads.issues)
        affected = affected or any(str(pairs / "async" / (str(item.get("task_id")) + ".json")) in reads.issues
                                   for key in ("turns", "running_tasks") for item in view[key])
        affected = affected or bool(details and details["note"])
        rows.append({
            "name": name, "backend": spec.get("backend") or context.get("backend") or "unknown",
            "model": display_model(spec, view, pair_tasks, parse), "status": status,
            "elapsed_s": max(0, (now - started).total_seconds()) if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "finished_ago_s": max(0, (now - finished).total_seconds()) if finished else None,
            "outcome": outcome,
            "detail": details, "context": fill, "watches": waiting,
            "partial": bool(affected and (view["verdict"] == "busy" or waiting
                             or (finished and (now - finished).total_seconds() <= RECENT_SECONDS))),
        })
    return {"rows": rows, "error": error, "updated_at": now.isoformat(), "cache": saved,
            "budget_limited": reads.budget_limited,
            "notice": "Details skipped (budget); all known registry pairs are still listed." if reads.budget_limited else
                      "Some working/recent pair details are unavailable within the read limits." if any(r["partial"] for r in rows) else None,
            "io": {"bytes": reads.bytes, "files": reads.files}}


def transcript_page(home, name, offset=0):
    """An explicit user read: at most 64 KiB of a transcript per page."""
    reads = Reads()
    root = Path(home).expanduser().resolve()
    registry = reads.json(root / "pairs" / "registry.json", 2 * 1024 * 1024) or {}
    spec = registry.get("pairs", {}).get(name, {}) if safe_pair(name) else {}
    session = spec.get("session_id")
    path = None
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return {"lines": [], "note": "No transcript is recorded for this pair.", "next": None}
    if spec.get("backend") == "codex":
        codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        databases = sorted(Path(e.path) for e in reads.entries(codex) if re.fullmatch(r"state_\d+\.sqlite", e.name))
        if databases:
            try:
                con = sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
                try:
                    record = con.execute("SELECT rollout_path FROM threads WHERE id=?", (session,)).fetchone()
                    path = Path(record[0]) if record and record[0] else None
                finally:
                    con.close()
            except (OSError, sqlite3.Error):
                pass
    elif isinstance(spec.get("cwd"), str):
        path = root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", spec["cwd"]) / (session + ".jsonl")
    if path is None or path.suffix != ".jsonl" or not path.is_file():
        return {"lines": [], "note": "Transcript unavailable (it may be archived or compressed).", "next": None}
    offset = max(0, offset) if type(offset) is int else 0
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(min(offset, size))
            data = stream.read(TASK_LIMIT)
    except OSError:
        return {"lines": [], "note": "Transcript could not be read.", "next": None}
    end = len(data)
    note = None
    if offset + end < size:
        boundary = data.rfind(b"\n")
        if boundary >= 0:
            end = boundary + 1
        else:
            note = "An oversized transcript record was skipped within this page's read limit."
    lines = []
    for raw in data[:end].splitlines():
        try:
            event = json.loads(raw)
            message = event.get("message") or event.get("payload") or event
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant"):
                continue
            text = content if isinstance(content, str) else "\n".join(
                block.get("text", "") for block in (content or []) if isinstance(block, dict)
                and block.get("type") in ("text", "input_text", "output_text"))
            if text:
                stamp = event.get("timestamp") or ""
                lines.append({"role": role, "at": stamp, "text": text})
        except (ValueError, TypeError, AttributeError):
            note = "Some transcript records could not be displayed within this page's read limit."
    return {"lines": lines, "note": note, "next": offset + end if offset + end < size else None,
            "offset": offset, "bytes": len(data)}


DETAIL_CHARS = 6000
DETAIL_SCAN = 16 * 1024 * 1024


def _clip(text):
    text = text if isinstance(text, str) else json.dumps(text, indent=2, ensure_ascii=False)
    return text if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS] + f"\n… ({len(text) - DETAIL_CHARS:,} more characters)"


def tool_detail(home, name, tag, sub=None):
    """One tool call's full input and output, read only when its activity line is opened."""
    root = Path(home).expanduser().resolve()
    if not safe_pair(name) or not isinstance(tag, str) or not re.fullmatch(r"T-\d+|toolu_\w+", tag):
        return {"note": "No tool call to show."}
    reads = Reads()
    logs = root / "pairs" / "logs" / name
    if type(sub) is int:
        # A sub-agent's call: T-n from its extracted index, or (while it runs)
        # the tool-use id itself; searched in that sub-agent's transcript only.
        registry = reads.json(root / "pairs" / "registry.json", 2 * 1024 * 1024) or {}
        spec = registry.get("pairs", {}).get(name, {})
        found = subagents(root, name)[1]
        path = sub_transcript(root, name, spec, sub, found)[0] if isinstance(spec, dict) else None
        tool_id = tag
        if tag.startswith("T-"):
            index = reads.json(logs / f"subagent-{sub}-{(found.get(sub) or {}).get('type', '')}.idx.json",
                               4 * 1024 * 1024)
            entry = index.get(tag) if isinstance(index, dict) else None
            tool_id = entry.get("tool_use_id") if isinstance(entry, dict) else None
        if path is None or not isinstance(tool_id, str):
            return {"note": "Full detail isn't recorded for this call; the activity line is all there is."}
        return _transcript_call([path], tool_id)
    if not tag.startswith("T-"):
        return {"note": "No tool call to show."}
    item = reads.json(logs / "codex-tool-items" / f"{tag}.json", TASK_LIMIT)
    if isinstance(item, dict):
        event = item.get("completed") or item.get("started") or {}
        kind = item.get("tool_name") or event.get("type")
        if kind == "command_execution":
            code = event.get("exit_code")
            return {"input": _clip(event.get("command") or ""), "language": "powershell",
                    "output": _clip(event.get("aggregated_output") or ""),
                    "status": None if code is None else f"exit {code}"}
        if kind == "file_change":
            return {"input": "\n".join(f"{c.get('kind', '?')}  {c.get('path', '?')}" for c in event.get("changes") or []
                                       if isinstance(c, dict)), "output": "", "status": event.get("status")}
        rest = {k: v for k, v in event.items() if k not in ("id", "type", "status")}
        return {"input": _clip(rest), "language": "json", "output": "", "status": event.get("status")}
    index = reads.json(logs / "main.idx.json", 4 * 1024 * 1024)
    entry = index.get(tag) if isinstance(index, dict) else None
    tool_id = entry.get("tool_use_id") if isinstance(entry, dict) else None
    registry = reads.json(root / "pairs" / "registry.json", 2 * 1024 * 1024) or {}
    spec = registry.get("pairs", {}).get(name, {})
    session, cwd = spec.get("session_id"), spec.get("cwd")
    if not isinstance(tool_id, str) or not isinstance(session, str) or not isinstance(cwd, str) \
            or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        return {"note": "Full detail isn't recorded for this call; the activity line is all there is."}
    path = root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / (session + ".jsonl")
    # The pair's log interleaves its sub-agents' tool calls; theirs live in
    # <session>/subagents/*.jsonl. Search the main transcript, then the
    # sub-agent transcripts newest first, within one byte budget.
    candidates = [path]
    try:
        candidates += sorted((p for p in (path.parent / session / "subagents").glob("*.jsonl")),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return _transcript_call(candidates, tool_id)


def _transcript_call(candidates, tool_id):
    """A Claude tool call's input and result from the first transcript tail holding its id."""
    needle = tool_id.encode("utf-8")
    budget = DETAIL_SCAN * 2
    data = b""
    for candidate in candidates:
        if budget <= 0:
            break
        try:
            with candidate.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - min(budget, DETAIL_SCAN)))
                chunk = stream.read(min(budget, DETAIL_SCAN))
        except OSError:
            continue
        budget -= len(chunk)
        if needle in chunk:
            data = chunk
            break
    found = {}
    for raw in data.splitlines():
        if needle not in raw:
            continue
        try:
            content = (json.loads(raw).get("message") or {}).get("content")
        except ValueError:
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") == tool_id:
                found["input"] = block.get("input")
            elif block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                result = block.get("content")
                found["output"] = result if isinstance(result, str) else "\n".join(
                    part.get("text", "") for part in result or [] if isinstance(part, dict))
                found["status"] = "error" if block.get("is_error") else None
    if not found:
        return {"note": "This call is older than the part of the transcript the pane reads."}
    tool_input = found.get("input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return {"input": _clip(command if isinstance(command, str) else tool_input or ""),
            "language": "bash" if isinstance(command, str) else "json",
            "output": _clip(found.get("output") or ""), "status": found.get("status")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1) if not sys.stdin.isatty() else b""
        if len(raw) > 1024 * 1024:
            raise ValueError("snapshot request exceeds 1 MiB")
        request = json.loads(raw) if raw else {}
        if not isinstance(request, dict):
            raise ValueError("snapshot request must be an object")
        if isinstance(request.get("transcript"), dict):
            target = request["transcript"]
            result = {"transcript": transcript_page(args.home, target.get("pair"), target.get("offset", 0))}
        elif isinstance(request.get("tool"), dict):
            target = request["tool"]
            result = {"tool": tool_detail(args.home, target.get("pair"), target.get("tag"), target.get("sub"))}
        else:
            result = snapshot(args.home, session=request.get("session"), show_all=request.get("all", True),
                              expanded=request.get("expanded", []), cache=request.get("cache"),
                              focus=request.get("focus"))
    except Exception as exc:
        result = {"rows": [], "error": f"Pair state could not be read: {exc}"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
