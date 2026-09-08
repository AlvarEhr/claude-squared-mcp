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
_READ_ONLY_REASONS: dict[str, str] = {}


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


def _unsafe_registry(path: Path, reason: str) -> None:
    """Keep the original bytes and prevent a partial view from being saved."""
    if str(path) not in _CORRUPT:
        try:
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
            path.with_name(f"registry.corrupt-{stamp}.json").write_bytes(path.read_bytes())
        except OSError:
            pass
    _CORRUPT.add(str(path))
    _READ_ONLY_REASONS[str(path)] = reason
    logger.warning("Registry %s: %s; refusing writes until repaired", path, reason)


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
        _READ_ONLY_REASONS.pop(str(path), None)
        return Registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as e:
        _unsafe_registry(path, f"not valid UTF-8 JSON ({e})")
        return Registry()
    if not isinstance(data, dict):
        _unsafe_registry(path, "the registry root must be an object")
        return Registry()
    # Migration: inject dict-key as `name` for legacy entries that omit it.
    pairs_obj = data.get("pairs", {})
    if not isinstance(pairs_obj, dict):
        _unsafe_registry(path, "pairs must be an object")
        return Registry()
    for key, val in pairs_obj.items():
        if isinstance(val, dict) and "name" not in val:
            val["name"] = key
    reasons: list[str] = []
    try:
        raw_version = data.get("version", 2)
        if isinstance(raw_version, bool) or str(raw_version) != str(int(raw_version)):
            raise ValueError("version must be an integer")
        on_disk_version = int(raw_version)
        if on_disk_version < 1:
            raise ValueError("version must be positive")
    except (TypeError, ValueError):
        on_disk_version = 2
        reasons.append("invalid registry version")
    if on_disk_version > REGISTRY_VERSION:
        reasons.append(f"version {on_disk_version} is newer than supported version {REGISTRY_VERSION}")
    cleaned: dict[str, PairSpec] = {}
    for key, val in pairs_obj.items():
        try:
            spec = PairSpec.model_validate(val)
            if spec.name != key:
                raise ValueError("pair name does not match its registry key")
            cleaned[key] = spec
        except (TypeError, ValueError):
            reasons.append(f"invalid pair entry {key!r}")
    reg = Registry.model_validate({**data, "version": on_disk_version, "pairs": cleaned})
    if reasons:
        _unsafe_registry(path, "; ".join(reasons))
        return reg
    _CORRUPT.discard(str(path))
    _READ_ONLY_REASONS.pop(str(path), None)
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
        except OSError:
            pass  # read-only media etc. — in-memory migration still applies
    return reg


def _save_unlocked(reg: Registry) -> None:
    path = registry_path()
    if str(path) in _CORRUPT:
        raise PairError(f"refusing to write {path}: {_READ_ONLY_REASONS.get(str(path), 'unsafe registry')}. "
                        "Repair or restore the original registry before retrying.")
    tmp = path.with_suffix(".tmp")
    data = reg.model_dump(mode="json", exclude_none=True)
    full = reg.model_dump(mode="json")
    for key in reg.model_extra or {}:
        data[key] = full[key]
    for name, spec in reg.pairs.items():
        for key in spec.model_extra or {}:
            data["pairs"][name][key] = full["pairs"][name][key]
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked_registry() -> Iterator[Registry]:
    """Hold the file lock for read-modify-write. Persist on context exit if changed."""
    lock = FileLock(str(lock_path()), timeout=30)
    with lock:
        reg = _load_unlocked()
        if str(registry_path()) in _CORRUPT:
            raise PairError(
                f"refusing to write {registry_path()}: "
                f"{_READ_ONLY_REASONS.get(str(registry_path()), 'unsafe registry')}. "
                "Repair or restore the original file; a backup is kept as registry.corrupt-*.json."
            )
        before = reg.model_dump_json()
        yield reg
        after = reg.model_dump_json()
        if before != after:
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
