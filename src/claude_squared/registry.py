"""Registry persistence with filelock-based concurrency safety."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from filelock import FileLock

from claude_squared.errors import PairAlreadyExists, PairError, PairNotFound
from claude_squared.models import PairSpec, Registry

logger = logging.getLogger(__name__)

# Registry paths whose last load fell back to "empty" because the file was not
# valid JSON. Writes through ``locked_registry`` are refused while a path is
# in here — see ``_load_unlocked``.
_CORRUPT: set[str] = set()


def claude_home() -> Path:
    """User's ~/.claude directory."""
    home = os.environ.get("CLAUDE_HOME")
    if home:
        return Path(home)
    return Path.home() / ".claude"


def pairs_dir() -> Path:
    p = claude_home() / "pairs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def registry_path() -> Path:
    return pairs_dir() / "registry.json"


def lock_path() -> Path:
    return pairs_dir() / "registry.json.lock"


def profiles_dir() -> Path:
    p = pairs_dir() / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def archive_dir() -> Path:
    p = pairs_dir() / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def async_dir() -> Path:
    p = pairs_dir() / "async"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = pairs_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agents_dir() -> Path:
    p = claude_home() / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


REGISTRY_VERSION = 3


def _load_unlocked() -> Registry:
    """Read + validate the registry. Always called under the file lock.

    v3 migration (v0.13.0, backend-neutral vocabulary): legacy permission
    spellings (``bypassPermissions``/``acceptEdits``/``default``/``dontAsk``)
    become the neutral levels, a ``[1m]`` model suffix moves into
    ``context_window``, and ``backend`` is inferred. The PairSpec validator
    does the per-entry work on every load; this function persists the result
    ONCE (in place, atomically) when the on-disk version is older, so other
    readers (wait.py, the CLI subcommands) see the migrated file too.
    """
    path = registry_path()
    if not path.exists():
        _CORRUPT.discard(str(path))
        return Registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # v0.13.0 (caught by the first Codex pair's review): a corrupt file used
        # to read as an EMPTY registry, and the next mutation would then
        # atomically replace it with that empty view — every pair gone. Now:
        # keep a copy of the bad file once, remember that this load was a
        # fallback, and let ``locked_registry`` refuse to write over it.
        try:
            bad = path.with_name(f"registry.corrupt-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json")
            if not any(path.parent.glob("registry.corrupt-*.json")):
                bad.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        _CORRUPT.add(str(path))
        logger.warning("registry.json is not valid JSON (%s); treating as empty for reads and "
                       "REFUSING writes until it is fixed or restored", e)
        return Registry()
    _CORRUPT.discard(str(path))
    # Migration: inject dict-key as `name` for legacy entries that omit it.
    pairs_obj = data.get("pairs") or {}
    if isinstance(pairs_obj, dict):
        for key, val in list(pairs_obj.items()):
            if isinstance(val, dict) and "name" not in val:
                val["name"] = key
    try:
        on_disk_version = int(data.get("version", 2) or 2)
    except (TypeError, ValueError):
        on_disk_version = 2
    try:
        reg = Registry.model_validate(data)
    except Exception:
        # Last-resort: skip malformed entries
        cleaned: dict = {}
        for key, val in pairs_obj.items():
            try:
                cleaned[key] = PairSpec.model_validate({**val, "name": val.get("name", key)})
            except Exception:
                continue
        reg = Registry(version=on_disk_version, pairs=cleaned)
    if on_disk_version < REGISTRY_VERSION:
        reg.version = REGISTRY_VERSION
        try:
            # Keep the pre-migration file: an MCP process still running
            # pre-v0.13.0 code cannot parse the neutral spellings and would
            # DROP those entries if it ever wrote the registry back. The
            # backup makes that recoverable; restarting old sessions after
            # install is the real fix (see CHANGELOG 0.13.0).
            backup = path.with_name(f"registry.v{on_disk_version}.backup.json")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            _save_unlocked(reg)
        except Exception:
            pass  # read-only media etc. — in-memory migration still applies
    return reg


def _save_unlocked(reg: Registry) -> None:
    path = registry_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(reg.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked_registry() -> Iterator[Registry]:
    """Hold the file lock for read-modify-write. Persist on context exit if changed."""
    lock = FileLock(str(lock_path()), timeout=30)
    with lock:
        reg = _load_unlocked()
        before = reg.model_dump_json()
        yield reg
        after = reg.model_dump_json()
        if before != after:
            if str(registry_path()) in _CORRUPT:
                raise PairError(
                    f"refusing to write {registry_path()}: the file on disk is not valid JSON "
                    f"and writing would replace every registered pair with this empty view. "
                    f"Fix it by hand or restore it (a copy was kept as registry.corrupt-*.json; "
                    f"the pre-0.13 backup is registry.v2.backup.json), then retry."
                )
            _save_unlocked(reg)


def load() -> Registry:
    """Read registry without holding the lock (for read-only views)."""
    lock = FileLock(str(lock_path()), timeout=10)
    with lock:
        return _load_unlocked()


def get_pair(name: str) -> PairSpec:
    reg = load()
    if name not in reg.pairs:
        raise PairNotFound(name)
    return reg.pairs[name]


def add_pair(spec: PairSpec) -> None:
    with locked_registry() as reg:
        if spec.name in reg.pairs:
            raise PairAlreadyExists(spec.name)
        reg.pairs[spec.name] = spec


def remove_pair(name: str) -> PairSpec:
    with locked_registry() as reg:
        if name not in reg.pairs:
            raise PairNotFound(name)
        return reg.pairs.pop(name)


def update_pair(name: str, **fields) -> PairSpec:
    with locked_registry() as reg:
        if name not in reg.pairs:
            raise PairNotFound(name)
        spec = reg.pairs[name]
        updated = spec.model_copy(update=fields)
        reg.pairs[name] = updated
        return updated
