"""Read-only, stdlib snapshot for the Pairs pane. Never imports the package.

Use the installed wait.py's merged activity view and OS PID checks verbatim:
the pane must not call a queued task, pending background or expected wake idle.
No process is stopped, and no registry/task/activity/watch file is changed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import runpy
import sys


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def snapshot(home):
    pairs = Path(home).expanduser().resolve() / "pairs"
    script = pairs / "wait.py"
    if not script.is_file():
        return {"rows": [], "error": "Start the pair MCP server to install wait.py in this Claude home."}
    reader = runpy.run_path(str(script))
    view_fn = reader["pair_activity_view"]
    parse = reader["_av_parse"]
    now = reader["_av_now"]()
    # One coherent task scan per refresh, not one scan of all history per pair.
    tasks = reader["_av_scan_tasks"](pairs)
    view_fn.__globals__["_av_scan_tasks"] = lambda *args, **kwargs: tasks
    proc = reader["_av_proc"]
    pids = {}

    def process(pid):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False, None
        if pid not in pids:
            pids[pid] = proc(pid)
        return pids[pid]

    view_fn.__globals__["_av_proc"] = process
    registry = read_json(pairs / "registry.json")
    specs = registry.get("pairs") if isinstance(registry, dict) else None
    error = None
    if not isinstance(specs, dict):
        specs = {}
        error = "Pair registry unavailable; showing observable work only."
    watches = {}
    for path in (pairs / "watches").glob("*.json"):
        record = read_json(path)
        if not isinstance(record, dict) or record.get("version") != 1 or not safe_pair(record.get("pair")):
            continue
        started = parse(record.get("started_at"))
        session = record.get("session_id")
        if started is None or not isinstance(session, str) or not session:
            continue
        alive, born = process(record.get("hook_pid"))
        if alive is False or (born is not None and born > started.timestamp() + 2):
            continue  # gone, or another process reused the watcher PID
        watches.setdefault(record["pair"], []).append({
            "session_id": session, "cwd": record.get("cwd"), "hook_pid": record["hook_pid"],
            "started_at": record["started_at"], "elapsed_s": max(0, (now - started).total_seconds()),
            "verified": alive is True,
        })
    names = set(specs) | set(watches)
    names.update(task.get("pair") for task in tasks if safe_pair(task.get("pair")) and task.get("status") == "running")
    if (pairs / "activity").is_dir():
        names.update(p.name for p in (pairs / "activity").iterdir() if p.is_dir())
    rows = []
    for name in sorted(n for n in names if safe_pair(n)):
        view = view_fn(pairs, name, now=now)
        waiting = watches.get(name, [])
        attention = any(t.get("state") in ("orphaned", "finished_detached") for t in view["running_tasks"])
        if view["verdict"] == "idle" and not waiting and not attention:
            continue
        spec = specs.get(name) if isinstance(specs.get(name), dict) else {}
        starts = [parse(item.get("started_at")) for key in ("turns", "running_tasks", "background")
                  for item in view[key]]
        starts += [parse(item.get("since")) for item in view["wake_pending"]]
        starts = [start for start in starts if start is not None]
        started = min(starts) if starts else parse(view.get("window_started_at"))
        context = view.get("context") or {}
        window = context.get("window") or (view.get("reported_window") or {}).get("tokens")
        used = context.get("used")
        fill = ({"used": used, "window": window, "percent": used * 100 / window}
                if isinstance(used, (int, float)) and isinstance(window, (int, float)) and window > 0 else None)
        rows.append({
            "name": name, "backend": spec.get("backend") or context.get("backend") or "unknown",
            "model": spec.get("model") or "unknown", "status": "needs attention" if attention else view["verdict"],
            "elapsed_s": max(0, (now - started).total_seconds()) if started else None,
            "last_line": tail(pairs / "logs" / name / "main.log"), "context": fill,
            "reason": "; ".join(view["reasons"][:2]), "watches": waiting,
        })
    return {"rows": rows, "error": error, "updated_at": now.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = snapshot(args.home)
    except Exception as exc:
        result = {"rows": [], "error": f"Pair state could not be read: {exc}"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
