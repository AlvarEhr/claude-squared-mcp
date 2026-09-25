"""Pair activity files (v0.16.0): what a pair is doing right now, readable
from any process — including the stdlib-only ``wait.py``.

Why: pending background tasks (``PairRuntime._bg_tasks``) and self-woken
turns lived only in the owning MCP process's memory. wait.py and every other
MCP process could only guess from task files, so "is this pair done?" had no
honest answer while background work was out (v0.16 design, 2026-09-25).

Layout, under ``~/.claude/pairs`` (``$CLAUDE_HOME/pairs``):

    activity/<pair>/<pid>.json   one file per (pair, owner process). Only that
                                 process writes it while it lives; readers
                                 MERGE all of a pair's files.
    activity/<pair>/pair.json    per-pair durable facts (the CLI-reported
                                 context window), any process may rewrite it.
    activity_owners/<pid>.json   beacon: "this process writes activity v1".
                                 A live task owner WITHOUT one is a pre-0.16
                                 process whose activity is invisible, so the
                                 view says "unknown", never "idle".

One file per owner, not per pair: two MCP processes can each hold a runtime
for the same pair (each has its own RuntimeRegistry; ``_PairLock`` serializes
sends, not runtime lifetime). A single shared file would be last-writer-wins
and erase the other process's pending background (historian, 2026-09-25).

Writer API — both backends (Claude runtime reader thread, Codex exec loop)::

    w = activity.writer(pair, backend)          # process-wide, one per pair
    w.turn_opened(task_id, kind)                # "send" | "self_woken" | "compact" | ...
                                                # every kind but "self_woken" starts a
                                                # new window (the caller's solicitation)
    w.turn_closed(task_id, status=, preview=, triage_pending=)  -> window_turns entry
    w.background_started(task_id, kind, description)
    w.background_finished(task_id, status)      # no turn open -> bounded wake_pending
    w.expect_wake(task_id=None)                 # a turn is starting with none open
    w.lose_background(reason, task_ids=None)    -> the lost entries (runtime killed)
    w.note_context(used, window, window_source=, source=)
    w.note_reported_window(tokens, model_key=, model=, source=)
    w.note_compaction(trigger, pre=, post=)
    w.set_triage(task_id, triage_dict)          # self-woken interim classifier result
    w.pending_background() / w.snapshot() / w.flush(timeout)

Module level: ``announce()`` (MCP startup: write the beacon), ``sweep()``
(startup: mark dead owners' files, drop old ones), ``shutdown(reason)``
(atexit: remaining background -> lost, state "exited", synchronous flush),
``forget(pair)`` (pair_forget), ``view(pair)`` / ``pair_activity_view(...)``
(the merged reader view), ``reported_window(pair, model_key)``.

Threading contract. Every ``ActivityWriter`` mutator only changes memory under
the writer's own lock and pokes the flusher: no file I/O, no other lock taken
while it is held. That lock is a LEAF, so mutators are safe from the runtime's
reader thread and from under ``_scope_lock`` / ``_promote_lock`` (the reader
must never block on disk — HANDOFF lock order). One daemon flusher thread per
process writes dirty files: turn / background / wake / compaction / triage
changes immediately, context-only changes at most once per
``LAZY_WRITE_INTERVAL_S``. File writes are atomic (unique tmp + replace, with
``async_tasks._save``'s PermissionError retry for Windows readers). A file's
writes are serialized by the writer's ``_io_lock`` (flusher vs ``flush()``),
taken only on I/O threads, always before the leaf lock.

The reader half between the BEGIN/END markers below is stdlib-only and
Python 3.7-compatible because wait.py embeds it verbatim;
tests/test_activity.py fails if the two copies drift.
"""

from __future__ import annotations

import copy
import threading
import uuid

from claude_squared import __version__ as _PACKAGE_VERSION
from claude_squared.registry import claude_home, pairs_dir


# ---- BEGIN STDLIB ACTIVITY VIEW ----
# Everything from here to the END marker is embedded verbatim in wait.py
# (stdlib only, Python >= 3.7, no names from outside this block). Helpers are
# prefixed _av_ so they cannot clash with wait.py's own functions.
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACTIVITY_SCHEMA_VERSION = 1
# Mirror of async_tasks.SELF_WOKEN_MESSAGE_PREFIX.
_AV_SELF_WOKEN_PREFIX = "<self-woken turn>"
# Process creation times recorded by an owner and re-read by a reader come
# from the same OS counter; anything beyond this is a different process
# that reused the PID.
_AV_START_TOLERANCE_S = 2.0
# How far back a task counts when looking for live pre-0.16 owners (the same
# 24 h the design uses as the background-work ceiling).
_AV_OWNER_LOOKBACK_S = 24 * 3600
# Codex detach contract (HANDOFF "v0.16.0" Codex detach contract): an
# events file written this recently proves a detached turn is still active.
_AV_EVENTS_GRACE_S = 2.0
_AV_K32 = []


def _av_now():
    return datetime.now(timezone.utc)


def _av_iso(dt):
    return dt.isoformat(timespec="milliseconds")


def _av_parse(ts):
    """ISO string -> aware UTC datetime, or None. Task files store NAIVE UTC
    (datetime.utcnow().isoformat()); activity files store aware UTC."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _av_kernel32():
    if not _AV_K32:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _AV_K32.append((ctypes, wintypes, k32))
    return _AV_K32[0]


def _av_proc(pid):
    """(alive, started_at) for a PID. alive is True/False, or None when the OS
    won't say (access denied: the process exists). started_at is the process
    creation time as epoch seconds, or None when unavailable (no PID-reuse
    guard then). Windows asks GetExitCodeProcess too: OpenProcess alone
    succeeds on an exited process whose handle someone still holds."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False, None
    if sys.platform == "win32":
        try:
            ctypes, wintypes, k32 = _av_kernel32()
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return (None if ctypes.get_last_error() == 5 else False), None
            try:
                code = wintypes.DWORD()
                alive = True
                if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                    alive = False  # 259 = STILL_ACTIVE
                times = [wintypes.FILETIME() for _ in range(4)]
                started = None
                if k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
                    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                    started = ticks / 1e7 - 11644473600.0  # FILETIME epoch is 1601
                return alive, started
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except OSError:
        return None, None
    started = None
    try:  # Linux: /proc/<pid>/stat field 22 = start time in clock ticks since boot
        with open("/proc/%d/stat" % pid) as f:
            raw = f.read()
        ticks = int(raw[raw.rindex(")") + 2:].split()[19])
        with open("/proc/stat") as f:
            btime = [int(line.split()[1]) for line in f if line.startswith("btime")][0]
        started = btime + ticks / float(os.sysconf("SC_CLK_TCK"))
    except Exception:
        started = None
    return True, started


def _av_owner_alive(pid, recorded_started_at):
    """An activity owner is alive if its PID is (or may be) alive AND, when both
    creation times are known, it is the same process that wrote the file."""
    alive, started = _av_proc(pid)
    if alive is False:
        return False
    if (isinstance(recorded_started_at, (int, float)) and started is not None
            and abs(started - recorded_started_at) > _AV_START_TOLERANCE_S):
        return False  # PID reused by a different process
    return True


def _av_task_owner_alive(pid, task_started):
    """A task file records no owner creation time, but a process created
    AFTER the task started cannot be its owner (PID reuse)."""
    alive, started = _av_proc(pid)
    if alive is False:
        return False
    if (task_started is not None and started is not None
            and started > task_started.timestamp() + _AV_START_TOLERANCE_S):
        return False
    return True


def _av_read_json(path, attempts=3):
    """(data, error). error == "missing" when the file is gone. Retries: a
    Windows reader can collide with the writer's replace."""
    err = None
    for i in range(attempts):
        try:
            with open(str(path), "r", encoding="utf-8") as f:
                return json.load(f), None
        except FileNotFoundError:
            return None, "missing"
        except (OSError, ValueError) as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(0.02 * (i + 1))
    return None, err


def _av_owner_files(pairs_dir, pair):
    d = Path(pairs_dir) / "activity" / pair
    try:
        names = sorted(os.listdir(str(d)))
    except OSError:
        return []
    return [d / n for n in names if n.endswith(".json") and n[:-5].isdigit()]


def _av_task_summary(data):
    msg = data.get("message")
    return {
        "task_id": data.get("task_id"),
        "pair": data.get("pair_name"),
        "status": data.get("status"),
        "owner_pid": data.get("owner_pid"),
        "child_pid": data.get("child_pid"),
        "child_started_at": data.get("child_started_at"),
        "events_path": data.get("events_path"),
        "queued": bool(data.get("queued")),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "self_woken": isinstance(msg, str) and msg.startswith(_AV_SELF_WOKEN_PREFIX),
    }


def _av_scan_tasks(pairs_dir, cache=None):
    """Summaries of every task file. ``cache`` (a dict the caller keeps across
    polls) maps path -> (mtime_ns, size, summary) so a long-lived watcher only
    re-reads files that changed — the async dir holds every task ever."""
    d = Path(pairs_dir) / "async"
    out = []
    try:
        entries = list(os.scandir(str(d)))
    except OSError:
        return out
    seen = set()
    for e in entries:
        if not e.name.endswith(".json") or e.name.startswith("."):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        seen.add(e.path)
        hit = cache.get(e.path) if cache is not None else None
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            out.append(hit[2])
            continue
        data, _err = _av_read_json(e.path, attempts=2)
        if not isinstance(data, dict):
            continue
        summary = _av_task_summary(data)
        if cache is not None:
            cache[e.path] = (st.st_mtime_ns, st.st_size, summary)
        out.append(summary)
    if cache is not None:
        for k in [k for k in cache if k not in seen]:
            cache.pop(k, None)
    return out


def _av_events_active(events_path, now):
    """The detach contract's second liveness proof: the turn's events file
    was written within the last _AV_EVENTS_GRACE_S seconds."""
    if not isinstance(events_path, str) or not events_path:
        return False
    try:
        mtime = os.stat(events_path).st_mtime
    except OSError:
        return False
    return now.timestamp() - mtime <= _AV_EVENTS_GRACE_S


def _av_beacon_ok(pairs_dir, pid):
    data, _err = _av_read_json(Path(pairs_dir) / "activity_owners" / ("%d.json" % pid))
    if not isinstance(data, dict):
        return False
    return _av_owner_alive(pid, data.get("owner_started_at"))


def _av_dead_owner_state(data, pid):
    """What an owner that died without closing its file left behind, as the
    sweep persists it: pending background -> background_lost, an open turn ->
    a window_turns entry, no wake. Pure: returns a new dict."""
    d = json.loads(json.dumps(data))
    at = d.get("updated_at") or _av_iso(_av_now())
    if d.get("backend") == "codex":
        why = ("owner MCP process (PID %d) died — the codex turn may continue detached "
               "(see its task's state)" % pid)
    else:
        why = ("owner MCP process (PID %d) died — the pair's claude process may have finished "
               "this unobserved; check pair_transcript" % pid)
    lost = d.setdefault("background_lost", [])
    for b in d.get("background") or []:
        rec = dict(b)
        rec.update({"reason": why, "at": at, "synthesized": True})
        lost.append(rec)
    d["background"] = []
    turn = d.get("turn")
    if isinstance(turn, dict):
        rec = dict(turn)
        rec.update({"ended_at": None, "status": "owner_died", "note": why, "bg_pending_after": 0,
                    "interim": False, "preview": "", "triage": None})
        d.setdefault("window_turns", []).append(rec)
    d["turn"] = None
    d["wake_pending"] = None
    d["state"] = "dead"
    d["closed_at"] = at
    d["closed_reason"] = why
    return d


# Auto-compaction fired at 967-971k of a 1M window on Opus 5 / 5.5, in both
# "1m" and "default" pairs (2026-09 measurements): window - 30k. Unmeasured
# for 200k windows — the same margin is an estimate there.
_AV_COMPACT_MARGIN = 30000


def compaction_point(window):
    """Estimated context size at which the CLI auto-compacts, or None."""
    if not isinstance(window, int) or window <= _AV_COMPACT_MARGIN:
        return None
    return window - _AV_COMPACT_MARGIN


def context_alert_threshold(window, headroom=None):
    """Context size at which to alert: ``headroom`` tokens before the
    estimated compaction point, so a real turn still fits (default
    max(15% of the window, 40k) — the pair_settings default
    ``context_alert_headroom`` overrides it). None when the window is
    unknown."""
    point = compaction_point(window)
    if point is None:
        return None
    if not isinstance(headroom, int) or headroom < 0:
        headroom = max(int(window * 0.15), 40000)
    return max(0, point - headroom)


def pair_activity_view(pairs_dir, pair, now=None, grace_seconds=5.0,
                       task_cache=None, live_states=None):
    """Merged, read-only view of one pair across every owner process.

    ``verdict``: "busy" (a turn is open, background is pending, a wake-up is
    expected, or a task is running/queued/detached), "unknown" (nothing busy
    is visible, but a live owner can't be seen: pre-0.16 process or an
    unreadable file), "settling" (quiet for less than ``grace_seconds``), or
    "idle". ``reasons`` says why for busy/unknown; ``notes`` carries
    non-blocking facts (dead owners, orphaned tasks). The window lists
    (window_turns, background_finished, background_lost, compactions) hold
    only entries since the latest solicited send across all owners.
    ``live_states`` maps pid -> in-memory state (the caller's own writer,
    fresher than its file). Never writes anything.
    """
    now = now or _av_now()
    pairs_dir = Path(pairs_dir)
    live_states = live_states or {}
    view = {
        "version": ACTIVITY_SCHEMA_VERSION, "pair": pair, "now": _av_iso(now),
        "verdict": "idle", "reasons": [], "notes": [], "owners": [],
        "turns": [], "background": [], "wake_pending": [], "running_tasks": [],
        "context": None, "reported_window": None, "window_started_at": None,
        "window_turns": [], "background_finished": [], "background_lost": [],
        "compactions": [], "quiet_since": None, "quiet_for_s": None,
    }
    busy, unknown, notes, quiet = [], [], [], []
    owners = []
    seen = set()
    for path in _av_owner_files(pairs_dir, pair):
        pid = int(path.name[:-5])
        seen.add(pid)
        if pid in live_states:
            data, err = live_states[pid], None
        else:
            data, err = _av_read_json(path)
        if data is None and err == "missing":
            continue
        if not isinstance(data, dict) or data.get("version") != ACTIVITY_SCHEMA_VERSION:
            why = err or "schema version %r, this reader knows %d" % (
                data.get("version") if isinstance(data, dict) else None, ACTIVITY_SCHEMA_VERSION)
            view["owners"].append({"pid": pid, "status": "unreadable", "error": why})
            unknown.append("activity of PID %d can't be read (%s)" % (pid, why))
            continue
        status = data.get("state") or "live"
        if status == "live" and not _av_owner_alive(pid, data.get("owner_started_at")):
            status = "died"
            data = _av_dead_owner_state(data, pid)
            notes.append("owner PID %d died without closing its activity" % pid)
        owners.append((pid, status, data))
    for pid, data in live_states.items():
        if pid not in seen and isinstance(data, dict):
            owners.append((pid, data.get("state") or "live", data))
    for pid, status, d in owners:
        view["owners"].append({"pid": pid, "status": status, "backend": d.get("backend"),
                               "updated_at": d.get("updated_at"),
                               "package_version": d.get("package_version")})

    starts = [_av_parse(d.get("window_started_at")) for _p, _s, d in owners]
    starts = [s for s in starts if s is not None]
    wstart = max(starts) if starts else None
    view["window_started_at"] = _av_iso(wstart) if wstart is not None else None

    def _in_window(ts):
        if wstart is None:
            return True
        t = _av_parse(ts)
        return t is not None and t >= wstart

    ctx_best = None
    list_keys = (("window_turns", "started_at"), ("background_finished", "ended_at"),
                 ("background_lost", "at"), ("compactions", "at"))
    for pid, status, d in owners:
        lt = _av_parse(d.get("last_transition_at"))
        if lt is not None:
            quiet.append(lt)
        if status == "live":
            turn = d.get("turn")
            if isinstance(turn, dict):
                view["turns"].append(dict(turn, pid=pid))
                busy.append("turn open in PID %d (%s, task %s, since %s)" % (
                    pid, turn.get("kind"), str(turn.get("task_id"))[:8], turn.get("started_at")))
            bg = [b for b in (d.get("background") or []) if isinstance(b, dict)]
            for b in bg:
                view["background"].append(dict(b, pid=pid))
            if bg:
                busy.append("%d background task(s) pending in PID %d (%s)" % (
                    len(bg), pid, ", ".join(str(b.get("kind")) for b in bg)))
            wake = d.get("wake_pending")
            if isinstance(wake, dict):
                until = _av_parse(wake.get("until"))
                if until is not None and until > now:
                    view["wake_pending"].append(dict(wake, pid=pid))
                    busy.append("wake-up pending in PID %d (background finished %s; the pair "
                                "should resume by %s)" % (pid, wake.get("since"), wake.get("until")))
                elif until is not None:
                    quiet.append(until)
        for key, tkey in list_keys:
            for e in d.get(key) or []:
                if isinstance(e, dict) and _in_window(e.get(tkey)):
                    view[key].append(dict(e, pid=pid))
        ctx = d.get("context")
        if isinstance(ctx, dict):
            at = _av_parse(ctx.get("at"))
            if at is not None and (ctx_best is None or at > ctx_best[0]):
                ctx_best = (at, dict(ctx, pid=pid))
    for key, tkey in list_keys:
        view[key].sort(key=lambda e: str(e.get(tkey) or ""))
    view["context"] = ctx_best[1] if ctx_best else None
    meta, _err = _av_read_json(pairs_dir / "activity" / pair / "pair.json")
    if isinstance(meta, dict):
        view["reported_window"] = meta.get("reported_window")

    owner_pids = set(p for p, _s, _d in owners)
    lookback = now - timedelta(seconds=_AV_OWNER_LOOKBACK_S)
    checked = {}
    for t in _av_scan_tasks(pairs_dir, task_cache):
        if t.get("pair") != pair:
            continue
        tid = str(t.get("task_id") or "?")
        started = _av_parse(t.get("started_at"))
        owner = t.get("owner_pid")
        has_owner = isinstance(owner, int) and not isinstance(owner, bool) and owner > 0
        if t.get("status") == "running":
            row = {"task_id": tid, "owner_pid": owner, "queued": t.get("queued"),
                   "self_woken": t.get("self_woken"), "started_at": t.get("started_at")}
            child = t.get("child_pid")
            if has_owner and _av_task_owner_alive(owner, started):
                row["state"] = "queued" if t.get("queued") else "running"
                busy.append("task %s %s in PID %d%s" % (
                    tid[:8], row["state"], owner, " (self-woken)" if t.get("self_woken") else ""))
            # The child is spawned AFTER its task starts (a queued task can
            # spawn it minutes later), so the owner rule "created after the
            # task = PID reuse" would misread a live child as gone; the task
            # records the child's own creation time instead (integration fix,
            # 2026-09-25).
            elif isinstance(child, int) and child > 0 and _av_owner_alive(child, t.get("child_started_at")):
                row["state"] = "detached"
                busy.append("task %s detached: owner PID %s is gone, its turn process PID %d "
                            "still runs" % (tid[:8], owner, child))
            elif _av_events_active(t.get("events_path"), now):
                row["state"] = "detached"
                busy.append("task %s detached: owner PID %s is gone, its events file is still "
                            "advancing" % (tid[:8], owner))
            elif t.get("events_path"):
                row["state"] = "finished_detached"
                notes.append("task %s finished detached (owner PID %s died, its turn process "
                             "exited); pair_poll('%s') collects the result" % (tid[:8], owner, tid))
            elif has_owner:
                row["state"] = "orphaned"
                notes.append("task %s orphaned (owner PID %d is gone); pair_poll('%s') "
                             "finalizes it" % (tid[:8], owner, tid))
            else:
                row["state"] = "legacy"
                notes.append("task %s is 'running' with no owner PID (pre-0.7.9 file)" % tid[:8])
            view["running_tasks"].append(row)
        else:
            fin = _av_parse(t.get("finished_at"))
            if fin is not None:
                quiet.append(fin)
        # Mixed-version fleet: a live owner of a recent task that never wrote a
        # beacon can have self-woken turns / background we cannot see.
        if (has_owner and owner not in owner_pids and started is not None
                and started >= lookback):
            if owner not in checked:
                checked[owner] = (_av_task_owner_alive(owner, started)
                                  and not _av_beacon_ok(pairs_dir, owner))
                if checked[owner]:
                    unknown.append("activity unknown for PID %d (owns task %s; a pre-0.16 MCP "
                                   "process — its background work and self-woken turns are "
                                   "invisible here)" % (owner, tid[:8]))

    view["notes"] = notes
    if busy:
        view["verdict"] = "busy"
    elif unknown:
        view["verdict"] = "unknown"
    else:
        qs = max(quiet) if quiet else None
        if qs is not None:
            view["quiet_since"] = _av_iso(qs)
            view["quiet_for_s"] = round((now - qs).total_seconds(), 1)
        settling = qs is not None and (now - qs).total_seconds() < grace_seconds
        view["verdict"] = "settling" if settling else "idle"
    view["reasons"] = busy + unknown
    return view
# ---- END STDLIB ACTIVITY VIEW ----


WAKE_PENDING_SECONDS = 120
LAZY_WRITE_INTERVAL_S = 1.0
WINDOW_TURNS_CAP = 50
LIST_CAP = 50
BACKGROUND_CAP = 100
PREVIEW_CHARS = 240
DEAD_RETENTION_SECONDS = 24 * 3600

_CLEAN, _LAZY, _URGENT = 0, 1, 2


def activity_dir() -> Path:
    return pairs_dir() / "activity"


def _home_pairs() -> Path:
    # No mkdir: writer() can be first called on the runtime's reader thread,
    # which must not touch the disk. Directories are made by the flusher.
    return claude_home() / "pairs"


def _atomic_write(path: Path, text: str) -> None:
    """tmp + replace, retrying PermissionError like ``async_tasks._save``
    (a Windows reader can briefly block replacing the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(4):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_OWNER_STARTED_AT: list[float | None] = []


def _owner_started_at() -> float | None:
    if not _OWNER_STARTED_AT:
        _OWNER_STARTED_AT.append(_av_proc(os.getpid())[1])
    return _OWNER_STARTED_AT[0]


# ---- beacon -------------------------------------------------------------

_BEACON_LOCK = threading.Lock()
_BEACONS: set[str] = set()  # pairs dirs this process has announced itself in


def _beacon_path(home_pairs: Path) -> Path:
    return home_pairs / "activity_owners" / f"{os.getpid()}.json"


def _ensure_beacon(home_pairs: Path) -> None:
    key = str(home_pairs)
    with _BEACON_LOCK:
        if key in _BEACONS:
            return
        _atomic_write(_beacon_path(home_pairs), json.dumps({
            "version": ACTIVITY_SCHEMA_VERSION, "pid": os.getpid(),
            "owner_started_at": _owner_started_at(),
            "package_version": _PACKAGE_VERSION,
            "announced_at": _av_iso(_av_now()),
        }))
        _BEACONS.add(key)


def announce() -> None:
    """MCP startup: declare this process an activity-v1 writer, so readers
    don't report its tasks' pairs as "activity unknown" before it has written
    any pair file. Never raises."""
    try:
        _ensure_beacon(_home_pairs())
    except OSError:
        pass


# ---- writer --------------------------------------------------------------

class ActivityWriter:
    """In-memory activity of one pair in THIS process + its file. See the
    module docstring for the threading contract."""

    def __init__(self, pair: str, backend: str, home_pairs: Path):
        self.pair = pair
        self.backend = backend
        self._home = home_pairs
        self._path = home_pairs / "activity" / pair / f"{os.getpid()}.json"
        self._meta_path = home_pairs / "activity" / pair / "pair.json"
        self._lock = threading.Lock()     # LEAF: memory only
        self._io_lock = threading.Lock()  # this file's writes; I/O threads only
        self._dirty = _CLEAN
        self._meta: dict | None = None
        self._meta_dirty = False
        self._last_write = 0.0
        self._forgotten = False  # forget() ran: never write this file again
        now = _av_iso(_av_now())
        self._state: dict = {
            "version": ACTIVITY_SCHEMA_VERSION, "pair": pair, "backend": backend,
            "owner_pid": os.getpid(), "owner_started_at": _owner_started_at(),
            "package_version": _PACKAGE_VERSION,
            "state": "live", "created_at": now, "updated_at": now, "seq": 0,
            "closed_at": None, "closed_reason": None,
            "last_transition_at": None,
            "window_started_at": None, "window_task_id": None,
            "turn": None, "background": [], "wake_pending": None,
            "context": None,
            "window_turns": [], "background_finished": [],
            "background_lost": [], "compactions": [],
        }

    # -- internals (callers hold self._lock) --

    def _mark(self, level: int) -> None:
        if level > self._dirty:
            self._dirty = level

    @staticmethod
    def _cap(items: list, n: int) -> None:
        if len(items) > n:
            del items[:len(items) - n]

    def _set_wake(self, task_id: str | None, now) -> None:
        self._state["wake_pending"] = {
            "since": _av_iso(now), "task_id": task_id,
            "until": _av_iso(now + timedelta(seconds=WAKE_PENDING_SECONDS)),
        }

    # -- turns --

    def turn_opened(self, task_id: str | None, kind: str = "send", *,
                    new_window: bool | None = None) -> None:
        """A turn started. ``kind`` "self_woken" continues the current window;
        any other kind (a solicited send, compact, invoke) starts a new one,
        clearing this owner's window lists. Clears a pending wake-up."""
        if new_window is None:
            new_window = kind != "self_woken"
        now = _av_now()
        iso = _av_iso(now)
        with self._lock:
            st = self._state
            if st["turn"] is not None:
                # A close we never saw (e.g. an untracked turn). Keep it visible.
                self._close_turn_locked(st["turn"].get("task_id"), "superseded", "", False, now)
            if new_window:
                st["window_started_at"] = iso
                st["window_task_id"] = task_id
                for key in ("window_turns", "background_finished", "background_lost", "compactions"):
                    st[key] = []
            st["turn"] = {"task_id": task_id, "kind": kind, "started_at": iso}
            st["wake_pending"] = None
            st["last_transition_at"] = iso
            self._mark(_URGENT)
        _poke()

    def _close_turn_locked(self, task_id, status, preview, triage_pending, now) -> dict:
        st = self._state
        turn = st["turn"] or {}
        pending = len(st["background"])
        for b in st["background"]:
            b["outlived_turn"] = True
        entry = {
            "task_id": turn.get("task_id") or task_id, "kind": turn.get("kind"),
            "started_at": turn.get("started_at"), "ended_at": _av_iso(now),
            "status": status, "bg_pending_after": pending, "interim": pending > 0,
            "preview": (preview or "")[:PREVIEW_CHARS],
            "triage": {"state": "pending"} if triage_pending else None,
        }
        st["window_turns"].append(entry)
        self._cap(st["window_turns"], WINDOW_TURNS_CAP)
        st["turn"] = None
        st["last_transition_at"] = _av_iso(now)
        return entry

    def turn_closed(self, task_id: str | None = None, *, status: str = "done",
                    preview: str = "", triage_pending: bool = False) -> dict | None:
        """The open turn ended. Returns its window_turns entry, or None when no
        turn is open or ``task_id`` names a different one (idempotent). With no
        turn open it resolves a pending wake-up instead: a result arrived for a
        turn that never opened (e.g. the CLI's stale resume notice)."""
        now = _av_now()
        out = None
        with self._lock:
            turn = self._state["turn"]
            if turn is None:
                if self._state["wake_pending"] is None:
                    return None
                self._state["wake_pending"] = None
                self._state["last_transition_at"] = _av_iso(now)
            elif task_id and turn.get("task_id") and task_id != turn.get("task_id"):
                return None
            else:
                out = dict(self._close_turn_locked(task_id, status, preview, triage_pending, now))
            self._mark(_URGENT)
        _poke()
        return out

    # -- background --

    def background_started(self, task_id: str, kind: str = "task", description: str = "") -> None:
        """Pass only BACKGROUNDED tasks: foreground Bash calls and blocking Agent
        calls emit ``task_started`` too, with ``is_backgrounded: false``
        (verified CLI 2.1.280, 2026-09-25)."""
        now = _av_iso(_av_now())
        with self._lock:
            bg = self._state["background"]
            for e in bg:
                if e.get("task_id") == task_id:
                    e.update(kind=kind, description=(description or "")[:160])
                    break
            else:
                bg.append({"task_id": task_id, "kind": kind,
                           "description": (description or "")[:160],
                           "started_at": now, "outlived_turn": False})
            if len(bg) > BACKGROUND_CAP:
                # A leak of missed completions must not grow the file forever.
                dropped = bg[:len(bg) - BACKGROUND_CAP]
                del bg[:len(bg) - BACKGROUND_CAP]
                for e in dropped:
                    self._state["background_lost"].append(dict(
                        e, reason="dropped from tracking: over %d pending entries — may already "
                                  "have finished (no completion event seen)" % BACKGROUND_CAP,
                        at=now))
                self._cap(self._state["background_lost"], LIST_CAP)
            self._state["last_transition_at"] = now
            self._mark(_URGENT)
        _poke()

    def background_finished(self, task_id: str, status: str = "completed") -> None:
        """A background task ended. With no turn open the pair is about to wake
        (the CLI emits task_notification BEFORE the self-woken turn's first
        event — 1.4-1.5 s on Haiku, tens of seconds at xhigh), so readers see
        a bounded ``wake_pending`` instead of a false idle. A completion DURING
        a turn is delivered inside that turn (a ``queued_command`` attachment
        on the next tool result) and wakes nothing afterwards (verified CLI
        2.1.280, 2026-09-25), so it sets no wake."""
        now = _av_now()
        iso = _av_iso(now)
        with self._lock:
            st = self._state
            entry = None
            for i, e in enumerate(st["background"]):
                if e.get("task_id") == task_id:
                    entry = st["background"].pop(i)
                    break
            st["background_finished"].append({
                "task_id": task_id, "kind": entry.get("kind") if entry else "unknown",
                "description": entry.get("description", "") if entry else "",
                "started_at": entry.get("started_at") if entry else None,
                "ended_at": iso, "status": status,
                "outlived_turn": bool(entry and entry.get("outlived_turn")),
            })
            self._cap(st["background_finished"], LIST_CAP)
            if st["turn"] is None:
                self._set_wake(task_id, now)
            st["last_transition_at"] = iso
            self._mark(_URGENT)
        _poke()

    def expect_wake(self, task_id: str | None = None) -> None:
        """A turn is about to start with none open — e.g. a ``system:init``
        arriving with no scope open: CLI 2.1.280 emits one ahead of every
        queued turn (20-80 ms after the previous result or the completion;
        the first assistant event can be tens of seconds later at xhigh).
        Same bounded ``wake_pending`` as a background completion; no-op
        while a turn is open."""
        now = _av_now()
        with self._lock:
            if self._state["turn"] is not None:
                return
            self._set_wake(task_id, now)
            self._state["last_transition_at"] = _av_iso(now)
            self._mark(_URGENT)
        _poke()

    def lose_background(self, reason: str, *, task_ids: list[str] | None = None) -> list[dict]:
        """The runtime is going away with background still pending (evict,
        pair_update/clear/rewind/compact, pair_stop force, crash, atexit): move
        it to ``background_lost``. A pending wake-up is lost too. Returns the
        new ``background_lost`` records."""
        now = _av_iso(_av_now())
        with self._lock:
            st = self._state
            keep, gone = [], []
            for e in st["background"]:
                (gone if task_ids is None or e.get("task_id") in task_ids else keep).append(e)
            st["background"] = keep
            recs = [dict(e, reason=reason, at=now) for e in gone]
            wake = st["wake_pending"]
            if task_ids is None and isinstance(wake, dict):
                recs.append({"task_id": wake.get("task_id"), "kind": "wake-up",
                             "description": "the pair was about to resume after background "
                                            "work finished", "reason": reason, "at": now})
                st["wake_pending"] = None
            if recs:
                st["background_lost"].extend(recs)
                self._cap(st["background_lost"], LIST_CAP)
                st["last_transition_at"] = now
                self._mark(_URGENT)
            out = [dict(r) for r in recs]
        if out:
            _poke()
        return out

    def pending_background(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._state["background"]]

    # -- context --

    def note_context(self, used: int | None, window: int | None = None, *,
                     window_source: str | None = None, source: str = "stream") -> None:
        """Live context fill. Claude: input + cache_read + cache_creation of the
        latest TOP-LEVEL assistant event; Codex: the rollout's
        ``last_token_usage`` (never the cumulative total). ``window`` should be
        the CLI-reported one (see ``reported_window``); ``window_source``
        "reported" | "inferred". Written lazily (<= 1/s)."""
        if used is None:
            return
        with self._lock:
            self._state["context"] = {
                "used": int(used), "window": int(window) if window else None,
                "pct": round(used * 100.0 / window, 1) if window else None,
                "window_source": window_source, "source": source,
                "at": _av_iso(_av_now()),
            }
            self._mark(_LAZY)
        _poke()

    def note_reported_window(self, tokens: int | None, *, model_key: str | None = None,
                             model: str | None = None, source: str = "result") -> None:
        """Persist the window the CLI reported (Claude: result modelUsage
        contextWindow; Codex: the rollout's model_context_window) per pair, so
        a fresh runtime or another process uses it instead of inferring from
        the spec. ``model_key`` identifies what the pair asked for (e.g.
        "opus|default") so a model switch invalidates it."""
        if not tokens:
            return
        rec = {"tokens": int(tokens), "model_key": model_key, "model": model,
               "source": source, "at": _av_iso(_av_now())}
        with self._lock:
            old = (self._meta or {}).get("reported_window") or {}
            if (old.get("tokens"), old.get("model_key"), old.get("model")) == (
                    rec["tokens"], model_key, model):
                return
            self._meta = {"version": ACTIVITY_SCHEMA_VERSION, "pair": self.pair,
                          "reported_window": rec}
            self._meta_dirty = True
        _poke()

    def note_compaction(self, trigger: str | None, pre: int | None = None,
                        post: int | None = None) -> None:
        now = _av_iso(_av_now())
        with self._lock:
            st = self._state
            st["compactions"].append({"at": now, "trigger": trigger, "pre": pre, "post": post})
            self._cap(st["compactions"], LIST_CAP)
            ctx = st["context"]
            if post is not None and isinstance(ctx, dict):
                window = ctx.get("window")
                ctx.update(used=int(post), at=now, source="compaction",
                           pct=round(post * 100.0 / window, 1) if window else None)
            self._mark(_URGENT)
        _poke()

    def set_triage(self, task_id: str, triage: dict) -> bool:
        """Attach the interim classifier's verdict to that turn's window entry
        (the ONLY place triage is stored — terminal task files are never
        rewritten). False when the entry is gone (new window / capped)."""
        with self._lock:
            for e in reversed(self._state["window_turns"]):
                if e.get("task_id") == task_id:
                    tri = dict(triage)
                    tri.setdefault("state", "done")
                    e["triage"] = tri
                    self._mark(_URGENT)
                    break
            else:
                return False
        _poke()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    # -- I/O (flusher thread, flush(), shutdown) --

    def _flush(self, force: bool) -> bool:
        """Write if dirty and due. True when nothing is left unwritten."""
        with self._io_lock:
            with self._lock:
                if self._forgotten or (not self._dirty and not self._meta_dirty):
                    return True
                mono = time.monotonic()
                due = (force or self._dirty == _URGENT or self._meta_dirty
                       or mono - self._last_write >= LAZY_WRITE_INTERVAL_S)
                if not due:
                    return False
                text = None
                if self._dirty:
                    self._state["updated_at"] = _av_iso(_av_now())
                    self._state["seq"] += 1
                    text = json.dumps(self._state, indent=1)
                meta = json.dumps(self._meta, indent=1) if self._meta_dirty else None
                self._dirty = _CLEAN
                self._meta_dirty = False
            try:
                _ensure_beacon(self._home)
                if text is not None:
                    _atomic_write(self._path, text)
                if meta is not None:
                    _atomic_write(self._meta_path, meta)
                ok = True
            except (OSError, ValueError):
                ok = False
                with self._lock:  # retry at the lazy pace
                    if text is not None:
                        self._mark(_LAZY)
                    if meta is not None:
                        self._meta_dirty = True
            self._last_write = time.monotonic()
            return ok

    def flush(self, timeout: float = 5.0) -> bool:
        """Write now from the calling thread (never from the reader thread)."""
        end = time.monotonic() + timeout
        while True:
            if self._flush(force=True):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def _shutdown(self, reason: str) -> None:
        now = _av_now()
        with self._lock:
            st = self._state
            if st["turn"] is not None:
                self._close_turn_locked(None, "aborted", "", False, now)["note"] = reason
        self.lose_background(reason)
        with self._lock:
            self._state.update(state="exited", closed_at=_av_iso(now), closed_reason=reason)
            self._mark(_URGENT)
        self.flush(timeout=2.0)


# ---- process-wide writers + flusher ----------------------------------------

_WRITERS: dict[tuple[str, str], ActivityWriter] = {}
_WRITERS_LOCK = threading.Lock()
_FLUSH_COND = threading.Condition()
_FLUSH_STATE = {"poked": False, "thread": None}


def writer(pair: str, backend: str = "claude") -> ActivityWriter:
    """This process's writer for ``pair`` (created on first use, no I/O).
    Always pass the pair's backend; the latest caller's value wins."""
    home = _home_pairs()
    key = (str(home), pair)
    with _WRITERS_LOCK:
        w = _WRITERS.get(key)
        if w is None:
            w = _WRITERS[key] = ActivityWriter(pair, backend, home)
    if w.backend != backend:
        with w._lock:  # noqa: SLF001
            w.backend = w._state["backend"] = backend  # noqa: SLF001
            w._mark(_URGENT)  # noqa: SLF001
    return w


def _writers_snapshot() -> list[ActivityWriter]:
    with _WRITERS_LOCK:
        return list(_WRITERS.values())


def _poke() -> None:
    """Wake the flusher. Takes only the flusher's condition lock (never while
    a writer lock is held by the flusher itself — no nesting either way)."""
    with _FLUSH_COND:
        _FLUSH_STATE["poked"] = True
        if _FLUSH_STATE["thread"] is None:
            t = threading.Thread(target=_flusher_loop, daemon=True, name="pair-activity-flusher")
            _FLUSH_STATE["thread"] = t
            t.start()
        _FLUSH_COND.notify()


def _flusher_loop() -> None:
    while True:
        writers = _writers_snapshot()
        lazy_pending = any(w._dirty or w._meta_dirty for w in writers)  # noqa: SLF001
        with _FLUSH_COND:
            if not _FLUSH_STATE["poked"]:
                _FLUSH_COND.wait(timeout=0.25 if lazy_pending else 30.0)
            _FLUSH_STATE["poked"] = False
        for w in _writers_snapshot():
            try:
                w._flush(force=False)  # noqa: SLF001
            except Exception:
                pass


def flush_all(timeout: float = 5.0) -> bool:
    ok = True
    for w in _writers_snapshot():
        ok = w.flush(timeout) and ok
    return ok


def shutdown(reason: str = "the owning MCP process exited") -> None:
    """atexit, AFTER the runtimes were stopped (their stop path records its
    own, more specific losses first): anything still pending is recorded
    lost, files are marked "exited" and written synchronously, the beacon is
    removed. Never raises."""
    for w in _writers_snapshot():
        try:
            w._shutdown(reason)  # noqa: SLF001
        except Exception:
            pass
    with _BEACON_LOCK:
        for key in list(_BEACONS):
            try:
                _beacon_path(Path(key)).unlink(missing_ok=True)
            except OSError:
                pass
            _BEACONS.discard(key)


def forget(pair: str) -> None:
    """pair_forget: drop this process's writer and files for ``pair``. Other
    owners' files are theirs (they go when those processes exit/are swept)."""
    home = _home_pairs()
    with _WRITERS_LOCK:
        w = _WRITERS.pop((str(home), pair), None)
    paths = [home / "activity" / pair / f"{os.getpid()}.json", home / "activity" / pair / "pair.json"]
    if w is not None:
        with w._io_lock:  # noqa: SLF001 — no flush may land after the unlink
            with w._lock:  # noqa: SLF001
                w._forgotten = True  # noqa: SLF001
            for p in paths:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    try:
        (home / "activity" / pair).rmdir()  # only if empty
    except OSError:
        pass


def reported_window(pair: str, model_key: str | None = None) -> dict | None:
    """The persisted CLI-reported window for ``pair`` ({tokens, model_key,
    model, source, at}), or None — also None when ``model_key`` is given and
    differs (the pair switched models since)."""
    data, _err = _av_read_json(_home_pairs() / "activity" / pair / "pair.json")
    rec = data.get("reported_window") if isinstance(data, dict) else None
    if not isinstance(rec, dict) or not rec.get("tokens"):
        return None
    if model_key is not None and rec.get("model_key") != model_key:
        return None
    return rec


_TASK_CACHE: dict = {}


def view(pair: str, *, grace_seconds: float = 5.0) -> dict:
    """``pair_activity_view`` for this process: our own writer's in-memory
    state stands in for its (up to a second older) file."""
    home = _home_pairs()
    announce()  # our own tasks must never read as a pre-0.16 owner
    with _WRITERS_LOCK:
        w = _WRITERS.get((str(home), pair))
    live = {os.getpid(): w.snapshot()} if w is not None else None
    return pair_activity_view(home, pair, grace_seconds=grace_seconds,
                              task_cache=_TASK_CACHE, live_states=live)


def sweep(*, now: datetime | None = None,
          retention_seconds: float = DEAD_RETENTION_SECONDS) -> dict[str, int]:
    """MCP startup: persist dead owners' leftovers (pending background ->
    lost, open turn -> window entry; exactly what the view already shows),
    delete dead/exited files older than ``retention_seconds``, remove dead
    beacons. Other processes' LIVE files are never touched. Never raises."""
    now = now or _av_now()
    counts = {"marked_dead": 0, "deleted": 0, "beacons_removed": 0}
    home = _home_pairs()
    me = os.getpid()
    try:
        beacons = list((home / "activity_owners").glob("*.json"))
    except OSError:
        beacons = []
    for b in beacons:
        if not b.stem.isdigit() or int(b.stem) == me:
            continue
        data, err = _av_read_json(b)
        started = data.get("owner_started_at") if isinstance(data, dict) else None
        if err == "missing" or _av_owner_alive(int(b.stem), started):
            continue
        try:
            b.unlink(missing_ok=True)
            counts["beacons_removed"] += 1
        except OSError:
            pass
    try:
        pair_dirs = [p for p in (home / "activity").iterdir() if p.is_dir()]
    except OSError:
        pair_dirs = []
    for pdir in pair_dirs:
        for path in _av_owner_files(home, pdir.name):
            pid = int(path.name[:-5])
            if pid == me:
                continue
            data, _err = _av_read_json(path)
            if not isinstance(data, dict) or data.get("version") != ACTIVITY_SCHEMA_VERSION:
                continue  # unreadable or another schema: not ours to judge
            state = data.get("state") or "live"
            try:
                if state == "live":
                    if _av_owner_alive(pid, data.get("owner_started_at")):
                        continue
                    _atomic_write(path, json.dumps(_av_dead_owner_state(data, pid), indent=1))
                    counts["marked_dead"] += 1
                else:
                    ended = _av_parse(data.get("closed_at") or data.get("updated_at"))
                    if ended is not None and (now - ended).total_seconds() >= retention_seconds:
                        path.unlink(missing_ok=True)
                        counts["deleted"] += 1
            except OSError:
                continue
    return counts
