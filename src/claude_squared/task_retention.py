"""Best-effort retention for old terminal task results, never pair work/logs."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone

from filelock import FileLock, Timeout
from claude_squared.registry import pairs_dir

logger = logging.getLogger(__name__)
RETENTION_SECONDS = 14 * 86400
CHECK_INTERVAL_SECONDS = 86400
TASK_BYTES = 16 * 1024 * 1024
ACTIVITY_BYTES = 2 * 1024 * 1024
_workers = {}
_workers_lock = threading.Lock()


def _read(path: Path, limit: int) -> dict:
    if path.stat().st_size > limit:
        raise ValueError("retention metadata exceeds read limit")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("retention metadata grew beyond read limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("retention metadata is not an object")
    return data


def _old_terminal(data: dict, cutoff: float) -> bool:
    if (data.get("status") not in ("done", "failed", "stopped") or data.get("queued")
            or data.get("detached") or data.get("state") in ("running", "queued", "detached")):
        return False
    try:
        finished = datetime.fromisoformat(data["finished_at"].replace("Z", "+00:00"))
        finished = finished.replace(tzinfo=timezone.utc) if finished.tzinfo is None else finished
        return finished.timestamp() < cutoff
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _references(data, ids: set[str]) -> set[str]:
    found = set()
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item in ids:
            found.add(item)
    return found


def _activity_references(root: Path, ids: set[str], pair: str | None = None) -> set[str]:
    from claude_squared import activity
    found = set()
    paths = (root / "activity" / pair).glob("*.json") if pair else (root / "activity").glob("*/*.json")
    for path in paths:
        for attempt in range(3):
            try:
                data = _read(path, ACTIVITY_BYTES)
                if data.get("version") != activity.ACTIVITY_SCHEMA_VERSION:
                    raise ValueError("unknown activity schema; references cannot be verified")
                found.update(_references(data, ids))
                break
            except OSError:
                if attempt == 2:
                    raise  # a disappearing/replacing file may still protect a task
                time.sleep(.02)
    # Also protect local references which the asynchronous flusher has not
    # published yet. Snapshot locks are leaves, and held for memory copies only.
    for writer in activity._writers_snapshot():
        if writer._home.resolve() == root and (pair is None or writer.pair == pair):
            found.update(_references(writer.snapshot(), ids))
    return found


def prune_terminal_tasks(root: Path | None = None, *, now: float | None = None) -> dict:
    """Cross-process daily gate; recheck eligibility/references under the pair lock.

    No running task can be pruned by age. Terminal files need both an old mtime
    and old finished_at; ambiguous data, active children and references survive.
    """
    from claude_squared import activity, async_tasks
    root = (root if root is not None else pairs_dir()).resolve()
    folder = root / "async"
    now = time.time() if now is None else now
    cutoff = now - RETENTION_SECONDS
    result = {"ran": False, "deleted": 0, "kept": 0, "errors": 0}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with FileLock(str(folder / ".retention.lock"), timeout=0):
            stamp = folder / ".retention"
            try:
                last = float(_read(stamp, 1024).get("attempted_at", 0))
            except (OSError, ValueError, TypeError):
                last = 0
            if now - last < CHECK_INTERVAL_SECONDS:
                return result
            temp = stamp.with_name(".retention-" + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_text(json.dumps({"attempted_at": now}), encoding="utf-8")
                temp.replace(stamp)  # record attempt first: failures cannot cause retry storms
            finally:
                temp.unlink(missing_ok=True)
            result["ran"] = True
            candidates = []
            for path in folder.glob("*.json"):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    data = _read(path, TASK_BYTES)
                    pair = data.get("pair_name")
                    if (data.get("task_id") != path.stem or not isinstance(pair, str) or not pair
                            or pair in (".", "..") or any(c in pair for c in "/\\:\0")
                            or not _old_terminal(data, cutoff)):
                        result["kept"] += 1
                        continue
                    candidates.append((path, pair))
                except (OSError, ValueError):
                    result["errors"] += 1
            ids = {path.stem for path, _ in candidates}
            # An unreadable activity record makes reference discovery unsafe:
            # abort this attempt rather than guess which old tasks it protects.
            references = _activity_references(root, ids) if ids else set()
            (root / "locks").mkdir(exist_ok=True)
            for path, pair in candidates:
                if path.stem in references:
                    result["kept"] += 1
                    continue
                try:
                    with FileLock(str(root / "locks" / (pair + ".lock")), timeout=0):
                        data = _read(path, TASK_BYTES)
                        with async_tasks._states_guard:
                            local = path.stem in async_tasks._local_states or path.stem in async_tasks._unsaved_results
                        if (local or path.stat().st_mtime >= cutoff or not _old_terminal(data, cutoff)
                                or data.get("task_id") != path.stem or data.get("pair_name") != pair
                                or path.stem in references
                                or path.stem in _activity_references(root, ids, pair)):
                            result["kept"] += 1
                            continue
                        child = data.get("child_pid")
                        # A terminal receipt may still have a surviving child.
                        # Unknown liveness is protected too; never stop it here.
                        if data.get("events_path") and child is None:
                            result["kept"] += 1  # detached metadata without a verifiable child identity
                            continue
                        if child is not None:
                            if (not isinstance(child, int) or isinstance(child, bool) or child <= 0
                                    or activity._av_proc(child)[0] is not False):
                                result["kept"] += 1
                                continue
                        path.unlink()
                        result["deleted"] += 1
                except Timeout:
                    result["kept"] += 1  # never wait behind work on the pair
                except (OSError, ValueError, TypeError):
                    result["errors"] += 1
    except Timeout:
        return result
    except Exception as exc:
        result["errors"] += 1
        logger.warning("Async task retention skipped unsafe/unavailable state (%s)", type(exc).__name__)
    if result["ran"]:
        logger.info("Async task retention (14 days): deleted=%d kept=%d errors=%d",
                    result["deleted"], result["kept"], result["errors"])
    return result


def start_housekeeping() -> threading.Event:
    """Start once per server/home; the shared stamp limits all servers to daily."""
    root = pairs_dir().resolve()
    with _workers_lock:
        existing = _workers.get(root)
        if existing and existing[0].is_alive():
            return existing[1]
        stop = threading.Event()
        def run():
            while not stop.is_set():
                try:
                    prune_terminal_tasks(root)
                except Exception as exc:
                    logger.warning("Async task retention attempt failed (%s)", type(exc).__name__)
                if stop.wait(CHECK_INTERVAL_SECONDS):
                    break
        thread = threading.Thread(target=run, daemon=True, name="async-task-retention")
        _workers[root] = (thread, stop)
        thread.start()
        return stop
