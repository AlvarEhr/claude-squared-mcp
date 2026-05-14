"""Registry persistence with filelock-based concurrency safety."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock

from claude_squared.errors import PairAlreadyExists, PairNotFound
from claude_squared.models import PairSpec, Registry


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


def _load_unlocked() -> Registry:
    path = registry_path()
    if not path.exists():
        return Registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Registry()
    # Migration: inject dict-key as `name` for legacy entries that omit it.
    pairs_obj = data.get("pairs") or {}
    if isinstance(pairs_obj, dict):
        for key, val in list(pairs_obj.items()):
            if isinstance(val, dict) and "name" not in val:
                val["name"] = key
    try:
        return Registry.model_validate(data)
    except Exception:
        # Last-resort: skip malformed entries
        cleaned: dict = {}
        for key, val in pairs_obj.items():
            try:
                cleaned[key] = PairSpec.model_validate({**val, "name": val.get("name", key)})
            except Exception:
                continue
        return Registry(version=data.get("version", 2), pairs=cleaned)


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
