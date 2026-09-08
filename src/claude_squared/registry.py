"""Registry persistence with filelock-based concurrency safety."""

from __future__ import annotations

import hashlib
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
# v0.14.0: entries that failed validation, per registry path → {key: (raw
# JSON value, reason)}. They are kept VERBATIM and written back untouched on
# every save, so one unreadable pair never freezes writes for the rest
# (preserve AND stay available — review decision on the maintenance branch,
# which refused all writes instead). Root-level damage — unparseable file,
# non-object root/pairs, bad or newer ``version`` — still makes the file
# read-only via ``_unsafe_registry``.
_QUARANTINE: dict[str, dict[str, tuple[object, str]]] = {}


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
            # Named by content, so every MCP process that loads the same bad
            # file keeps ONE copy between them (review catch: a per-process
            # timestamp name wrote a full copy per process).
            raw = path.read_bytes()
            copy = path.with_name(f"registry.corrupt-{hashlib.sha256(raw).hexdigest()[:12]}.json")
            if not copy.exists():
                copy.write_bytes(raw)
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
    pairs_obj = data.get("pairs", {})
    if not isinstance(pairs_obj, dict):
        _unsafe_registry(path, "pairs must be an object")
        return Registry()
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
    held: dict[str, tuple[object, str]] = {}
    for key, val in pairs_obj.items():
        try:
            if not isinstance(val, dict):
                raise ValueError("entry is not an object")
            # Legacy entries omit ``name``: validate with the dict key injected,
            # without mutating the raw value (a quarantined raw stays verbatim).
            spec = PairSpec.model_validate({**val, "name": val.get("name", key)})
            if spec.name != key:
                raise ValueError("pair name does not match its registry key")
            cleaned[key] = spec
        except (TypeError, ValueError) as e:
            held[key] = (val, _short_reason(e))
    reg = Registry.model_validate({**data, "version": on_disk_version, "pairs": cleaned})
    if reasons:
        _unsafe_registry(path, "; ".join(reasons))
        return reg
    _CORRUPT.discard(str(path))
    _READ_ONLY_REASONS.pop(str(path), None)
    previous = _QUARANTINE.get(str(path), {})
    _QUARANTINE[str(path)] = held
    for key, (_raw, why) in held.items():
        if key not in previous:
            logger.warning("Registry %s: entry %r is unreadable (%s); kept verbatim on disk and "
                           "hidden from the tools until repaired", path, key, why)
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
    # Quarantined entries ride along untouched. A live pair under the same
    # name wins (the only way to get one is repairing the entry by hand, or
    # deleting it and re-creating the pair).
    for key, (raw, _why) in _QUARANTINE.get(str(path), {}).items():
        data["pairs"].setdefault(key, raw)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _short_reason(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    # pydantic's first line is "N validation error(s) for PairSpec" — the field
    # detail is on the next lines; keep a compact "field: message" when present.
    if len(text) >= 3 and "validation error" in first:
        first = f"{text[1].strip()}: {text[2].strip()}"
    return first[:160]


def quarantined() -> dict[str, str]:
    """Entries of the current registry this process could not validate:
    name → reason. They stay on disk verbatim and are invisible to every tool
    until repaired (edit ``registry.json`` by hand, or delete the entry)."""
    return {key: why for key, (_raw, why) in _QUARANTINE.get(str(registry_path()), {}).items()}


def _quarantine_error(name: str) -> PairError:
    why = quarantined().get(name, "unknown")
    return PairError(
        f"Pair '{name}' exists in {registry_path()} but its entry is unreadable ({why}). "
        "It was kept verbatim; repair the entry by hand (then any tool call re-reads it), "
        "or delete it from the file to reuse the name."
    )


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


def assert_writable() -> None:
    """Preflight mutating backend work before any process or transcript changes."""
    with locked_registry():
        pass


def get_pair(name: str) -> PairSpec:
    reg = load()
    if name not in reg.pairs:
        if name in quarantined():
            raise _quarantine_error(name)
        raise PairNotFound(name)
    return reg.pairs[name]


def add_pair(spec: PairSpec) -> None:
    with locked_registry() as reg:
        if spec.name in reg.pairs:
            raise PairAlreadyExists(spec.name)
        if spec.name in quarantined():
            raise _quarantine_error(spec.name)
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
