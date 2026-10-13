"""Source of the standalone ``wait.py`` script that gets installed to
``~/.claude/pairs/wait.py`` on MCP server startup.

Why standalone: the agent's ``Bash(command="python ...")`` invokes whatever
``python`` is on its shell PATH. That Python may NOT have ``claude_squared``
importable — Desktop install bundles the package via PYTHONPATH inside the MCP
server's own subprocess, which the agent's bash spawn doesn't inherit. So a
``python -m claude_squared wait`` command fails for Desktop-only users with
``No module named claude_squared``.

This script imports nothing from our package — only stdlib — so it works on any
Python >= 3.7 the agent's bash happens to find. It polls the JSON state files
that ``async_tasks`` and ``activity`` write atomically.

v0.16.0: the script is assembled from three parts — a head, the stdlib
activity view copied VERBATIM from ``activity.py`` (between its BEGIN/END
markers; one source, so the watcher and the MCP can never disagree about
"idle"), and the watcher body. Head and body are raw strings: no escaping.

Kept in sync with ``__main__.py``'s ``_cmd_wait`` (which is the in-package
equivalent for users who DO have the module importable). Behavioral parity is a
soft promise; any divergence should favor this standalone version since it's
the one users actually invoke.
"""

from pathlib import Path

_BEGIN = "# ---- BEGIN STDLIB ACTIVITY VIEW ----"
_END = "# ---- END STDLIB ACTIVITY VIEW ----"

_WAIT_HEAD = r'''#!/usr/bin/env python3
"""Standalone watcher for claude-squared pairs and tasks (stdlib only).

    python wait.py <pair>              follow the pair until it is IDLE: no turn
                                       open, no task running or queued, no
                                       background work pending, no wake-up
                                       expected — then a summary of the window
    python wait.py <pair> --each-turn  return when the next turn ends (interim
                                       self-woken turns included)
    python wait.py <task_id|prefix>    wait for that one task (as before 0.16)

Options: --timeout SECS (default 21600 = 6 h) --poll SECS (default 2)
         --context-alert PCT | --no-context-alert

Context alert: exits 8 when the pair's context CROSSES the alert level while
the watcher runs (never at start, so re-arming is safe). Default level: the
estimated auto-compaction point (window - 30k) minus a headroom of
max(15% of the window, 40k) tokens — pair_settings_set(context_alert_headroom=N)
changes it for every pair; a pair's own context_alert_at (pair_update) moves
it earlier for that pair — a running watcher re-reads it every poll and fires
at once if the new level is already behind the pair's context; --context-alert
PCT sets a percentage of the window instead.

Exit codes:
    0  done / idle
    1  failed (work error — message on stderr)
    2  not found or ambiguous
    3  timeout (the watcher gave up; the pair is still working — re-arm)
    4  owner MCP server died — supervision event, NOT a work error. Mid-turn
       ("orphaned"): the work may well have completed — check pair_poll / git.
       Still queued ("not delivered"): it never ran — re-send it.
    5  stopped (pair_stop; the runtime is still alive)
    6  crashed (the pair's claude process died mid-turn; pair_send resumes)
    7  finished detached (Codex: the owner died, the turn finished on its own —
       pair_poll collects the result)
    8  context alert (see above)
    9  background work lost (killed while the watcher ran)
   10  idle as far as can be seen, but part of the pair is invisible or
       unresolved (a pre-0.16 MCP process, an activity file that is
       unreadable or behind, a wake-up that never started, background with
       no completion event for 24 h) — check pair_status before relying on it
   64  usage error
Exit 1 with EMPTY output means the watcher process itself was killed.
"""
from __future__ import annotations

'''

_WAIT_BODY = r'''

import shlex

# Kept in sync with claude_squared.async_tasks.ORPHAN_ERROR_PREFIX and
# CRASHED_ERROR_PREFIX (stdlib-only by design — see _wait_script.py).
_ORPHAN_ERROR_PREFIX = "ORPHANED: "
_CRASHED_ERROR_PREFIX = "CRASHED: "
DEFAULT_TIMEOUT_S = 6 * 3600
PAIR_GRACE_S = 5.0          # quiet this long before "idle" is final
TRIAGE_WAIT_S = 10.0        # the summary waits this long for pending triage
_TURN_EXIT = {"done": 0, "stopped": 5, "crashed": 6, "owner_died": 4, "failed": 1, "timeout": 1}


def _turn_exit(status) -> int:
    """A turn's status -> exit code; bookkeeping statuses (superseded,
    aborted, an error subtype we don't know) count as done unless they
    name a failure."""
    status = str(status or "")
    if status in _TURN_EXIT:
        return _TURN_EXIT[status]
    return 1 if status.startswith("error") else 0


def _claude_home() -> Path:
    """Mirror claude_squared.registry.claude_home() — stdlib-only."""
    h = os.environ.get("CLAUDE_HOME")
    return Path(h) if h else Path.home() / ".claude"


def _pairs_dir() -> Path:
    return _claude_home() / "pairs"


def _async_dir() -> Path:
    return _pairs_dir() / "async"


def _state_path(task_id: str) -> Path:
    return _async_dir() / f"{task_id}.json"


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _latest_task_for_pair(pair_name: str) -> str | None:
    """Most-recently-STARTED task id of a pair (mirrors
    async_tasks.latest_task_id_for_pair). ISO started_at sorts lexically."""
    d = _async_dir()
    if not d.is_dir():
        return None
    best_id, best_started = None, ""
    for p in d.glob("*.json"):
        data = _read(p)
        if not isinstance(data, dict) or data.get("pair_name") != pair_name:
            continue
        started = data.get("started_at") or ""
        if best_id is None or started > best_started:
            best_id, best_started = data.get("task_id"), started
    return best_id


def _find_tasks_by_prefix(prefix: str) -> list[str]:
    """Task ids whose file name starts with ``prefix`` (mirrors
    async_tasks.find_task_by_prefix). Empty prefix matches nothing."""
    if not prefix:
        return []
    d = _async_dir()
    if not d.is_dir():
        return []
    return [p.stem for p in d.glob(f"{prefix}*.json") if p.stem.startswith(prefix)]


def _registry_read(name: str):
    """(ok, entry): ok is False when registry.json could not be read — on
    Windows a reader's open handle can fail the writer's os.replace and the
    reader then sees no file or a torn one — so callers keep what they knew."""
    reg = _read(_pairs_dir() / "registry.json")
    pairs = reg.get("pairs") if isinstance(reg, dict) else None
    if not isinstance(pairs, dict):
        return False, None
    entry = pairs.get(name)
    return True, (entry if isinstance(entry, dict) else None)


def _registry_pair(name: str):
    return _registry_read(name)[1]


def _is_pair(name: str) -> bool:
    return (_registry_pair(name) is not None
            or (_pairs_dir() / "activity" / name).is_dir()
            or _latest_task_for_pair(name) is not None)


def _backend(pair: str) -> str:
    spec = _registry_pair(pair)
    return str(spec.get("backend") or "claude") if isinstance(spec, dict) else "claude"


def _headroom_read():
    """(ok, headroom) from defaults.json, like ``_registry_read``: ok is False
    when the file exists but could not be read or parsed (a torn or locked
    read mid-write), so callers keep the last good value. No file is a good
    read with no headroom set — pair_settings_reset deletes it."""
    try:
        d = json.loads((_pairs_dir() / "defaults.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True, None
    except (OSError, ValueError):
        return False, None
    if not isinstance(d, dict):
        return False, None
    v = d.get("context_alert_headroom")
    return True, (v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None)


def _alert_at(entry):
    """A registry entry's own context alert (``context_alert_at``, v0.17), or None."""
    v = entry.get("context_alert_at") if isinstance(entry, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _hms(iso) -> str:
    dt = _av_parse(iso)
    return dt.astimezone().strftime("%H:%M:%S") if dt else "?"


def _dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def _cmd(*args: str) -> str:
    """The exact command to re-arm this watcher."""
    here = Path(sys.argv[0]).resolve().as_posix() if sys.argv and sys.argv[0] else "wait.py"
    return " ".join([shlex.quote(sys.executable), shlex.quote(here)] + [shlex.quote(a) for a in args])


class Alert:
    """Context alert on a CROSSING: the level must be passed while we watch."""

    def __init__(self, mode, pct):
        self.mode, self.pct, self.prev, self.prev_level = mode, pct, None, None
        self.alert_at = None  # the pair's own level (v0.17), from the last good registry read
        self.headroom = None  # the global headroom, from the last good defaults.json read
        self.alert_at_read = self.headroom_read = False  # a good read seen yet?

    def note_registry(self, ok, entry):
        """Take the pair's ``context_alert_at`` from this poll's registry read
        (a pair_update applies to a watcher already running). A failed read
        keeps the last good value."""
        if ok:
            self.alert_at, self.alert_at_read = _alert_at(entry), True

    def level(self, window, backend="claude", auto_compact_token_limit=None):
        if self.mode == "off" or not window:
            return None
        if self.mode == "pct":
            return int(window * self.pct / 100.0)
        # A failed read keeps the last good headroom: falling back to the
        # default for one poll moved the level and back, and the drop rule in
        # check() read the move back as a lowered level — a spurious exit 8
        # (second review of b07509e).
        ok, headroom = _headroom_read()
        if ok:
            self.headroom, self.headroom_read = headroom, True
        return context_alert_threshold(window, self.headroom, backend, auto_compact_token_limit,
                                       self.alert_at)

    def check(self, ctx, reported):
        """(used, window, level) when this observation crosses the level."""
        if not isinstance(ctx, dict) or not ctx.get("used"):
            return None
        used = int(ctx["used"])
        window = ctx.get("window") or (reported or {}).get("tokens")
        self.backend = ctx.get("backend") or (reported or {}).get("backend") or "claude"
        self.auto_compact_token_limit = ctx.get("auto_compact_token_limit") or (reported or {}).get("auto_compact_token_limit")
        level = self.level(window, self.backend, self.auto_compact_token_limit)
        prev, self.prev = self.prev, used
        # A level computed before both inputs had one good read stood on the
        # defaults, not the pair's settings: no base for the drop rule below.
        known = self.mode == "pct" or (self.alert_at_read and self.headroom_read)
        prev_level, self.prev_level = self.prev_level, (level if known else None)
        if level is None or prev is None:
            return None
        if prev < level <= used:
            return used, window, level
        # The level moved down past the fill while we watched (a lower
        # context_alert_at, a smaller window): fire once, as the crossing
        # would have — unless the fill was already past the old level.
        if prev_level is not None and level < prev_level and prev < prev_level and level <= used:
            return used, window, level
        return None


def _without_alert_flags(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--context-alert":
            skip = True
        elif a != "--no-context-alert":
            out.append(a)
    return out


def _alert_message(pair, used, window, level, rearm_base, backend="claude", auto_compact_token_limit=None):
    rearm_base = _without_alert_flags(rearm_base)
    point = compaction_point(window, backend, auto_compact_token_limit)
    nxt = min(99, int(max(used, (used + point) / 2 if point else used) * 100 / window) + 1)
    lines = [
        f"CONTEXT ALERT: pair '{pair}' is at {used:,} / {window:,} tokens ({used * 100 / window:.0f}%), "
        f"past the alert level {level:,}.",
    ]
    if point and backend == "codex":
        # Unmeasured for Codex: the native trigger point/quantity, and the
        # rollout's token_count can land only after a tool call finishes
        # (observed 2026-09-25) — say so (anvil, round 3).
        lines.append(f"  Estimated Codex auto-compaction point: {point:,} ({max(0, point - used):,} tokens "
                     f"away) — mid-turn, with a generic summary. Based on the last reported input tokens; "
                     f"native trigger accounting is unmeasured, and rollout updates may lag one tool call.")
    elif point:
        lines.append(f"  Auto-compaction fires near {point:,} ({max(0, point - used):,} tokens away) — "
                     f"mid-turn, with a generic summary.")
    # Codex compaction takes no steering input (protocol-verified 2026-09-11).
    compact = (f"pair_compact('{pair}')" if backend == "codex" else
               f"pair_compact('{pair}', steering_prompt=...)")
    lines.append(f"  To compact cleanly: pair_stop('{pair}') -> {compact} -> pair_send('{pair}', 'continue').")
    lines.append(f"  Keep watching:  {_cmd(*rearm_base, '--context-alert', str(nxt))}")
    lines.append(f"  or without the alert:  {_cmd(*rearm_base, '--no-context-alert')}")
    return "\n".join(lines)


def _task_exit(data) -> int | None:
    """Terminal task state -> exit code (None while running)."""
    status = data.get("status")
    if status == "done":
        return 0
    if status == "stopped":
        print(data.get("error") or "stopped by pair_stop", file=sys.stderr)
        return 5
    if status == "failed":
        err = data.get("error") or "(no error message)"
        print(err, file=sys.stderr)
        if err.startswith(_ORPHAN_ERROR_PREFIX):
            return 4
        if err.startswith(_CRASHED_ERROR_PREFIX):
            return 6
        return 1
    return None


def _dead_owner_exit(data, now) -> int | None:
    """A running task whose owner MCP process died. None = keep waiting
    (a detached Codex turn still runs), else the exit code. Same identity
    rules as the activity view: the owner can't have been created after its
    task started (PID reuse); the child is judged by its OWN recorded
    creation time — it is spawned after the task starts, minutes later when
    queued (anvil review, 2026-09-25)."""
    owner = data.get("owner_pid")
    if not (isinstance(owner, int) and owner > 0):
        return None
    started = _av_parse(data.get("started_at"))
    if _av_task_owner_alive(owner, started):
        return None
    child = data.get("child_pid")
    if (isinstance(child, int) and child > 0
            and _av_owner_alive(child, data.get("child_started_at"))):
        return None
    events = data.get("events_path")
    if _av_events_active(events, now):
        return None
    tid = data.get("task_id")
    if events:
        print(f"finished detached: the owner MCP server (pid {owner}) died, and the turn's own "
              f"process has exited. pair_poll('{tid}') collects the recovered result.",
              file=sys.stderr)
        return 7
    if data.get("queued"):
        # v0.18.0 (Incident 2026-10-04): still queued when its server died.
        print(f"not delivered: owner MCP server (pid {owner}) died before this send ran — it "
              f"was never delivered. Re-send it.", file=sys.stderr)
        return 4
    if _backend(str(data.get("pair_name") or "")) == "codex":
        how = ("A codex turn from before v0.16 dies with its MCP server; the thread is intact — "
               "pair_send resumes it.")
    else:
        how = ("The pair's claude process usually runs the turn to completion anyway — check "
               "pair_poll / pair_transcript / git, then pair_send to resume.")
    print(f"orphaned: owner MCP server (pid {owner}) is no longer alive; the task was running "
          f"but its supervisor died mid-turn. {how}", file=sys.stderr)
    return 4


def wait_task(task_id: str, arg: str, timeout_s: float, poll_s: float, alert: Alert,
              extra: list) -> int:
    state_file = _state_path(task_id)
    deadline = time.monotonic() + timeout_s
    pair = None
    while True:
        data = _read(state_file)
        if not isinstance(data, dict):
            # Mid-write race or transient I/O error; just retry.
            if time.monotonic() >= deadline:
                print(f"timeout after {timeout_s:.0f}s; couldn't read the task file", file=sys.stderr)
                return 3
            time.sleep(poll_s)
            continue
        code = _task_exit(data)
        if code is not None:
            return code
        now = _av_now()
        if data.get("status") == "running":
            code = _dead_owner_exit(data, now)
            if code is not None:
                return code
        pair = pair or data.get("pair_name")
        if pair:
            view = pair_activity_view(_pairs_dir(), pair, now=now, grace_seconds=0)
            alert.note_registry(*_registry_read(pair))
            hit = alert.check(view.get("context"), view.get("reported_window"))
            if hit:
                print(_alert_message(pair, *hit, [arg] + extra, alert.backend, alert.auto_compact_token_limit))
                return 8
        if time.monotonic() >= deadline:
            print(f"watcher gave up after {_dur(timeout_s)}; task {task_id[:8]} is still "
                  f"{data.get('status')} — the pair is still working. Re-arm:\n"
                  f"  {_cmd(arg, *extra)}", file=sys.stderr)
            return 3
        time.sleep(poll_s)


def _turn_key(t):
    return (t.get("pid"), t.get("task_id"), t.get("started_at"))


def _turn_line(t) -> str:
    tid = t.get("task_id") or ""
    kind = "self-woken" if t.get("kind") == "self_woken" else (t.get("kind") or "turn")
    extra = f", {t.get('bg_pending_after')} background still out" if t.get("interim") else ""
    prev = str(t.get("preview") or "").replace("\n", " ")[:100]
    return (f"  {_hms(t.get('ended_at'))} {kind} {tid[:8] or '(untracked)'} [{t.get('status')}"
            f"{extra}] {prev!r}")


def _summary(pair, view, observed, t0_wall, started_iso, mode) -> str:
    # The view's copy of a turn is the fresher one (a triage verdict lands
    # on it after the turn closed); observed keeps turns a newer window has
    # since cleared from the view.
    turns = dict((_turn_key(t), t) for t in observed)
    for t in view.get("window_turns") or []:
        turns[_turn_key(t)] = t
    turns = sorted(turns.values(), key=lambda t: str(t.get("ended_at") or t.get("started_at") or ""))
    sends = [t for t in turns if t.get("kind") != "self_woken"]
    woken = [t for t in turns if t.get("kind") == "self_woken"]
    interims = [t for t in turns if t.get("interim")]
    since = view.get("window_started_at") or started_iso
    since_dt = _av_parse(since)
    span = (_av_now() - since_dt).total_seconds() if since_dt else time.time() - t0_wall
    out = [f"pair '{pair}' {mode} — window {_dur(span)} (since {_hms(since)}), "
           f"watched {_dur(time.time() - t0_wall)}"]
    out.append(f"turns: {len(turns)} ({len(sends)} send/compact/invoke, {len(woken)} self-woken; "
               f"{len(interims)} interim)")
    for t in turns[-8:]:
        out.append(_turn_line(t))
    flagged = [t for t in interims if isinstance(t.get("triage"), dict)
               and t["triage"].get("needs_attention")]
    pending = [t for t in interims if isinstance(t.get("triage"), dict)
               and t["triage"].get("state") == "pending"]
    if flagged:
        out.append(f"interims flagged for attention ({len(flagged)}):")
        for t in flagged:
            tri = t["triage"]
            line = str(tri.get("summary") or tri.get("reason") or "").replace("\n", " ")[:140]
            out.append(f"  {_hms(t.get('ended_at'))} task {str(t.get('task_id'))[:8]} — {line} "
                       f"→ pair_poll('{t.get('task_id')}')")
    if pending:
        out.append(f"triage pending for {len(pending)} interim(s)")
    fin = [b for b in view.get("background_finished") or [] if b.get("outlived_turn")]
    lost = view.get("background_lost") or []
    still = view.get("background") or []
    if fin or lost or still:
        stale = [b for b in still if b.get("stale")]
        out.append(f"background: {len(fin) + len(lost) + len(still)} started, {len(fin)} finished, "
                   f"{len(lost)} lost, {len(still)} still running"
                   + (f" ({len(stale)} stale: no completion event for 24 h — may have finished "
                      f"unseen)" if stale else ""))
        for b in lost[-5:]:
            out.append(f"  lost {b.get('kind')} {str(b.get('task_id'))[:10]}: {b.get('reason')}")
    comps = view.get("compactions") or []
    if comps:
        out.append("compactions: " + "; ".join(
            f"{c.get('trigger')} {c.get('pre') or '?'}→{c.get('post') or '?'} at {_hms(c.get('at'))}"
            for c in comps))
    ctx = view.get("context") or {}
    if ctx.get("used"):
        window = ctx.get("window") or (view.get("reported_window") or {}).get("tokens")
        pct = f" ({ctx['used'] * 100 / window:.0f}%)" if window else ""
        out.append(f"context now: {ctx['used']:,}" + (f" / {window:,}" if window else "") + pct)
    for n in view.get("notes") or []:
        out.append(f"note: {n}")
    if turns and turns[-1].get("task_id"):
        out.append(f"final reply: pair_poll('{turns[-1]['task_id']}', with_turn_log=True)")
    else:
        out.append(f"final reply: pair_poll('{pair}', with_turn_log=True)")
    return "\n".join(out)


def _quiet(view) -> bool:
    """Nothing left to follow: "idle", or "unknown" once it has been quiet
    as long as idle must be (a just-created owner's file can lag its task)."""
    if view.get("running_tasks"):
        return False
    verdict = view.get("verdict")
    if verdict == "idle":
        return True
    quiet_for = view.get("quiet_for_s")
    return verdict == "unknown" and (quiet_for is None or quiet_for >= PAIR_GRACE_S)


def _idle_exit(pair, turns) -> int:
    """Exit code once the pair is idle: the outcome of whichever is newer —
    the latest task (it may have failed before any turn opened) or the last
    turn the activity files recorded."""
    last = sorted(turns or [{}], key=lambda t: str(t.get("ended_at") or ""))[-1]
    turn_end = _av_parse(last.get("ended_at"))
    tid = _latest_task_for_pair(pair)
    data = _read(_state_path(tid)) if tid else None
    if isinstance(data, dict) and data.get("status") != "running":
        fin = _av_parse(data.get("finished_at"))
        if turn_end is None or (fin is not None and fin >= turn_end - timedelta(seconds=1)):
            code = _task_exit(data)
            if code is not None:
                return code
    return _turn_exit(last.get("status"))


def _lost_key(b):
    return (b.get("pid"), b.get("task_id"), b.get("at"))


def _observe(view, seen, observed, started) -> list:
    """Record the turns in ``view``; returns those that ended while we watch
    and weren't seen before. A turn seen again is refreshed in place — its
    triage verdict can land after it closed."""
    new_turns = []
    for t in view.get("window_turns") or []:
        key = _turn_key(t)
        if key in seen:
            seen[key].clear()
            seen[key].update(t)
            continue
        seen[key] = t
        ended = _av_parse(t.get("ended_at"))
        if ended is not None and ended >= started:
            observed.append(t)
            new_turns.append(t)
    return new_turns


def _forgotten(view, registry) -> bool:
    """pair_forget left only tombstones: no registry entry, no live owner.
    ``registry`` is this poll's (ok, entry); an unreadable registry proves nothing."""
    ok, entry = registry
    owners = view.get("owners") or []
    return (ok and entry is None and any(o.get("status") == "forgotten" for o in owners)
            and not any(o.get("status") == "live" for o in owners))


def wait_pair(pair: str, each_turn: bool, timeout_s: float, poll_s: float, alert: Alert,
              extra: list) -> int:
    t0_wall = time.time()
    started = _av_now()
    started_iso = _av_iso(started)
    deadline = time.monotonic() + timeout_s
    cache: dict = {}
    seen: dict = {}
    observed: list = []
    # Losses on file when we start are old news; any other that appears is
    # fresh — whatever its timestamp (a dead owner's synthesized loss
    # carries its last write time, a forced compact's lands before the
    # window it opens).
    lost_seen = None
    triage_until = None  # set while quiet with triage pending (TRIAGE_WAIT_S)
    base = [pair] + extra
    while True:
        now = _av_now()
        view = pair_activity_view(_pairs_dir(), pair, now=now, grace_seconds=PAIR_GRACE_S,
                                  task_cache=cache)
        # Turns that ended while we watch (the window list is cleared by a new
        # send, so remember them).
        new_turns = _observe(view, seen, observed, started)
        registry = _registry_read(pair)  # once per poll: the alert and the forgotten check
        alert.note_registry(*registry)
        hit = alert.check(view.get("context"), view.get("reported_window"))
        if hit:
            print(_alert_message(pair, *hit, base, alert.backend, alert.auto_compact_token_limit))
            return 8
        for row in view.get("running_tasks") or []:
            if row.get("state") in ("orphaned", "finished_detached"):
                data = _read(_state_path(row["task_id"]))
                code = _dead_owner_exit(data, now) if isinstance(data, dict) else None
                if code is not None:
                    print(_summary(pair, view, observed, t0_wall, started_iso, "stopped being watched"))
                    return code
        losses = view.get("losses") or []
        if lost_seen is None:
            lost_seen = set(_lost_key(b) for b in losses
                            if (_av_parse(b.get("at")) or started) < started)
        fresh_lost = [b for b in losses if _lost_key(b) not in lost_seen]
        if fresh_lost:
            for b in fresh_lost:
                lost_seen.add(_lost_key(b))
            print("BACKGROUND LOST: " + "; ".join(
                f"{b.get('kind')} {str(b.get('task_id'))[:10]} — {b.get('reason')}" for b in fresh_lost),
                file=sys.stderr)
            print("The pair will not wake for this work. Check what landed (pair_poll / git) and "
                  "re-send if it is still needed.", file=sys.stderr)
            print(_summary(pair, view, observed, t0_wall, started_iso, "lost background work"))
            return 9
        if _forgotten(view, registry):
            print(f"pair '{pair}' was forgotten (pair_forget) while being watched", file=sys.stderr)
            return 2
        if each_turn and new_turns:
            t = new_turns[-1]
            print(f"turn ended ({'self-woken' if t.get('kind') == 'self_woken' else t.get('kind')}, "
                  f"{t.get('status')}):")
            print(_turn_line(t))
            if t.get("interim"):
                print(f"  interim: background still out — {_cmd(pair, *extra)} follows it")
            if t.get("task_id"):
                print(f"  full reply: pair_poll('{t['task_id']}', with_turn_log=True)")
            return _turn_exit(t.get("status"))
        if _quiet(view):
            # A short wait for pending triage is a state of THIS loop, not a
            # loop of its own: every refreshed snapshot goes through the same
            # checks above (a turn ending then reaches --each-turn, a loss
            # exits 9, new work keeps us following) — a separate inner loop
            # swallowed those (anvil reviews, 2026-09-25).
            if any(isinstance(t.get("triage"), dict) and t["triage"].get("state") == "pending"
                   for t in (view.get("window_turns") or [])):
                if triage_until is None:
                    triage_until = time.monotonic() + TRIAGE_WAIT_S
                if time.monotonic() < triage_until:
                    time.sleep(min(poll_s, 1.0))
                    continue
            if view.get("verdict") == "unknown":
                for r in view.get("reasons") or []:
                    print(f"note: {r}", file=sys.stderr)
                print(_summary(pair, view, observed, t0_wall, started_iso,
                               "idle as far as can be seen (part of it is invisible)"))
                return 10
            print(_summary(pair, view, observed, t0_wall, started_iso, "idle"))
            return _idle_exit(pair, list(seen.values()))
        triage_until = None
        if time.monotonic() >= deadline:
            why = "; ".join((view.get("reasons") or [])[:3]) or str(view.get("verdict"))
            print(f"watcher gave up after {_dur(timeout_s)}; pair '{pair}' is still working "
                  f"({why}). Re-arm:\n  {_cmd(pair, *extra)}", file=sys.stderr)
            return 3
        time.sleep(poll_s)


def _utf8_output() -> None:
    """Redirected output on Windows defaults to the ANSI code page (cp1252),
    and a model's reply preview or a note can hold characters it can't
    encode: the print raised UnicodeEncodeError and the watcher died with
    exit 1 instead of its real code (anvil review, 2026-09-25). UTF-8 with
    replacement never raises (not even on a lone surrogate from JSON)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str]) -> int:
    _utf8_output()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 64
    arg = argv[0]
    timeout_s, poll_s = float(DEFAULT_TIMEOUT_S), 2.0
    each_turn = False
    alert = Alert("default", None)
    extra: list = []
    i = 1
    while i < len(argv):
        a = argv[i]
        try:
            if a == "--timeout" and i + 1 < len(argv):
                timeout_s = float(argv[i + 1])
                extra += [a, argv[i + 1]]
                i += 2
            elif a == "--poll" and i + 1 < len(argv):
                poll_s = max(0.5, float(argv[i + 1]))
                extra += [a, argv[i + 1]]
                i += 2
            elif a == "--context-alert" and i + 1 < len(argv):
                pct = float(argv[i + 1])
                if not 0 < pct < 100:
                    raise ValueError
                alert = Alert("pct", pct)
                extra += [a, argv[i + 1]]
                i += 2
            elif a == "--no-context-alert":
                alert = Alert("off", None)
                extra.append(a)
                i += 1
            elif a == "--each-turn":
                each_turn = True
                extra.append(a)
                i += 1
            else:
                print(f"unknown arg: {a}", file=sys.stderr)
                return 64
        except ValueError:
            print(f"invalid value for {a}: {argv[i + 1]}", file=sys.stderr)
            return 64

    # Resolution: exact task id -> pair name (follow the pair) -> unique task
    # prefix. Two attempts tolerate the race right after a task is created.
    for attempt in (0, 1):
        if _state_path(arg).exists():
            return wait_task(arg, arg, timeout_s, poll_s, alert, extra)
        if _is_pair(arg):
            return wait_pair(arg, each_turn, timeout_s, poll_s, alert, extra)
        matches = _find_tasks_by_prefix(arg)
        if len(matches) == 1:
            print(f"resolved prefix '{arg}' -> task {matches[0]}", file=sys.stderr)
            return wait_task(matches[0], arg, timeout_s, poll_s, alert, extra)
        if len(matches) > 1:
            shown = ", ".join(t[:12] for t in matches[:5])
            more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
            print(f"ambiguous: prefix '{arg}' matches {len(matches)} tasks: {shown}{more}. "
                  f"Use a longer prefix or the full task id.", file=sys.stderr)
            return 2
        if attempt == 0:
            time.sleep(min(poll_s, 1.0))
    print(f"not found: '{arg}' is not a task id, prefix, or pair name", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def _activity_view_block() -> str:
    """The stdlib view, verbatim from activity.py (the single source)."""
    src = Path(__file__).with_name("activity.py").read_text(encoding="utf-8")
    start = src.index(_BEGIN)
    return src[start:src.index(_END, start) + len(_END)]


WAIT_SCRIPT_SOURCE = _WAIT_HEAD + _activity_view_block() + _WAIT_BODY


def install_wait_script(target: "Path | None" = None) -> "Path":
    """Write the standalone wait.py to ``~/.claude/pairs/wait.py`` (or the
    overridden path) if missing or stale. Returns the absolute target path.

    Idempotent: if the on-disk content matches the embedded source, no-op.
    """
    from claude_squared.registry import pairs_dir
    if target is None:
        target = pairs_dir() / "wait.py"
    try:
        existing = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = None
    if existing != WAIT_SCRIPT_SOURCE:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write to avoid agent reading half-written content
        tmp = target.with_suffix(".tmp")
        tmp.write_text(WAIT_SCRIPT_SOURCE, encoding="utf-8")
        tmp.replace(target)
    return target.resolve()
