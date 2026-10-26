"""Read-only, stdlib snapshot for the Pairs pane. Never imports the package.

Use the installed wait.py's merged activity view and OS PID checks verbatim:
the pane must not call a queued task, pending background or expected wake idle.
No process is stopped, and no registry/task/activity/watch file is changed.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
from pathlib import Path
import re
import runpy
import sys

JSON_LIMIT = 128 * 1024
TASK_LIMIT = 64 * 1024
BYTE_BUDGET = 4 * 1024 * 1024
FILE_BUDGET = 128
SCAN_BUDGET = 2048
TASK_BUDGET = 128
ROW_BUDGET = 64
RECENT_SECONDS = 15 * 60


class Reads:
    """Hard I/O limits, including races where a file grows after stat()."""
    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.partial = False
        self.memo = {}

    def json(self, path, limit=JSON_LIMIT):
        path = Path(path)
        if path in self.memo:
            return self.memo[path]
        value = None
        try:
            size = path.stat().st_size
            if size > limit or size > BYTE_BUDGET - self.bytes or self.files >= FILE_BUDGET:
                self.partial = True
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
        self.memo[path] = value
        return value

    def entries(self, directory, limit=SCAN_BUDGET):
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= limit:
                        self.partial = True
                        break
                    yield entry
        except FileNotFoundError:
            pass
        except OSError:
            self.partial = True


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
    for task_id in list(references)[:TASK_BUDGET]:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            continue
        path = folder / (task_id + ".json")
        try:
            candidates[path.name] = (path.stat(), path)
        except OSError:
            pass
    recent = []
    for entry in reads.entries(folder):
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
            reads.partial = True  # old unreferenced queued work cannot be ruled out
            continue
        recent.append((stat, Path(entry.path)))
    room = max(0, TASK_BUDGET - len(candidates))
    if len(recent) > room:
        reads.partial = True
    for stat, path in heapq.nlargest(room, recent, key=lambda item: item[0].st_mtime_ns):
        candidates[path.name] = (stat, path)
    summaries, saved = [], {}
    cache_bytes = 0
    for filename, (stat, path) in candidates.items():
        old = previous.get(filename)
        if (isinstance(old, dict) and old.get("mtime") == str(stat.st_mtime_ns) and old.get("size") == stat.st_size
                and isinstance(old.get("summary"), dict)):
            summary = old["summary"]
        else:
            data = reads.json(path, TASK_LIMIT)
            if not isinstance(data, dict):
                continue
            summary = reader["_av_task_summary"](data)
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


def safe_pair(name):
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and not any(c in name for c in "/\\:\0"))


def tail(path):
    """Read at most 8 KiB even when the pair's log is many megabytes."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 8192)
            stream.seek(offset)
            data = stream.read(8192)
        lines = data.decode("utf-8", "replace").splitlines()
        if offset:
            lines = lines[1:]
        line = next((line.strip() for line in reversed(lines) if line.strip()), "")
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        return "".join(c if c.isprintable() else " " for c in line)[:320]
    except OSError:
        return ""


def context_fill(reader, view, spec, defaults):
    """Last activity-file fill and the watcher's threshold, without a model call."""
    context = view.get("context") or {}
    reported = view.get("reported_window") or {}
    window = context.get("window") or reported.get("tokens")
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


def snapshot(home, session=None, show_all=True, expanded=(), cache=None):
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
    for entry in reads.entries(pairs / "watches"):
        path = Path(entry.path)
        if path.suffix != ".json":
            continue
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
    if len(names) > ROW_BUDGET:
        reads.partial = True
    names = names[:ROW_BUDGET]
    owner_files = {}
    references = set()
    for name in names:
        # Context/window metadata takes priority over the optional task scan;
        # exhausting its budget must not replace a reported window with a guess.
        reads.json(pairs / "activity" / name / "pair.json")
        owner_files[name] = [Path(e.path) for e in reads.entries(pairs / "activity" / name, 16)
                             if e.name.endswith(".json") and e.name[:-5].isdigit()]
        for path in owner_files[name]:
            activity = reads.json(path)
            turn = activity.get("turn") if isinstance(activity, dict) else None
            if isinstance(turn, dict) and isinstance(turn.get("task_id"), str):
                references.add(turn["task_id"])
    tasks, saved = task_summaries(pairs, set(names), references, reader, reads, now, cache, show_all) if names else ([], {})
    view_fn.__globals__["_av_scan_tasks"] = lambda *args, **kwargs: tasks
    view_fn.__globals__["_av_owner_files"] = lambda root, name: owner_files.get(name, [])
    rows = []
    for name in names:
        view = view_fn(pairs, name, now=now)
        waiting = watches.get(name, [])
        attention = any(t.get("state") in ("orphaned", "finished_detached") for t in view["running_tasks"])
        if view["verdict"] == "idle" and not waiting and not attention and name not in specs:
            continue
        spec = specs.get(name) if isinstance(specs.get(name), dict) else {}
        starts = [parse(item.get("started_at")) for key in ("turns", "running_tasks", "background")
                  for item in view[key]]
        starts += [parse(item.get("since")) for item in view["wake_pending"]]
        starts = [start for start in starts if start is not None]
        started = min(starts) if starts else parse(view.get("window_started_at"))
        context = view.get("context") or {}
        fill = context_fill(reader, view, spec, defaults)
        rows.append({
            "name": name, "backend": spec.get("backend") or context.get("backend") or "unknown",
            "model": spec.get("model") or "unknown", "status": "needs attention" if attention else view["verdict"],
            "elapsed_s": max(0, (now - started).total_seconds()) if started else None,
            "last_line": tail(pairs / "logs" / name / "main.log") if name in expanded else "", "context": fill,
            "reason": "; ".join(view["reasons"][:2]) if name in expanded else "", "watches": waiting,
        })
    if reads.partial:
        for row in rows:
            if row["status"] == "idle":
                row["status"] = "unknown"
    return {"rows": rows, "error": error, "updated_at": now.isoformat(), "cache": saved,
            "notice": "Partial snapshot: old, oversized or over-budget task/activity data omitted." if reads.partial else None,
            "io": {"bytes": reads.bytes, "files": reads.files}}


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
        result = snapshot(args.home, session=request.get("session"), show_all=request.get("all", True),
                          expanded=request.get("expanded", []), cache=request.get("cache"))
    except Exception as exc:
        result = {"rows": [], "error": f"Pair state could not be read: {exc}"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
