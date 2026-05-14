"""Per-user defaults for new pairs (v0.8.0+).

Stored at ``~/.claude/pairs/defaults.json``, filelock-protected (reuses
``filelock`` like ``registry.py``). Each field is optional — None means "use
the hardcoded default" so a fresh install with no defaults file behaves
identically to pre-v0.8.0.

Read at the API boundary by ``pair_create`` to fill missing args; written by
``pair_settings_set``. Per-call args ALWAYS override defaults (preserves the
"intent always wins" principle from historian's design pass).

Hardcoded fallbacks (when neither the call nor the defaults file specifies):
    model = "opus"
    effort = "xhigh" (model-coerced for sonnet → "high", haiku → None)
    permission_mode = "auto"
    persistent = False
    extra_dirs = None

The special model value ``"match-parent"`` triggers JSONL-based detection of
the calling Claude Code session's model (see ``server.py``'s
``_resolve_match_parent``). Effort under match-parent mode uses
``default_effort_for_model`` for the detected model, NOT the parent's effort
(env-based effort detection is unreliable from a long-lived MCP child whose
``os.environ`` is frozen at spawn time).
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock
from pydantic import BaseModel, Field, field_validator

from claude_squared.models import EffortLevel, PermissionMode, coerce_effort_for_model
from claude_squared.registry import pairs_dir

logger = logging.getLogger(__name__)


class PairDefaults(BaseModel):
    """User-configurable defaults applied to ``pair_create`` when fields are unset.

    Each field is Optional. Unset (None) means "use the hardcoded default."
    """

    # Model defaults. Special string "match-parent" triggers JSONL detection.
    model: str | None = None
    # Effort can be explicitly set, or auto-derive from model via
    # ``default_effort_for_model`` if None.
    effort: EffortLevel | None = None
    # Permission mode. The setter REJECTS bypassPermissions as a default value
    # (foot-gun: every new pair would silently have no guardrails). Pass it
    # per-pair on pair_create where the decision is visible.
    permission_mode: PermissionMode | None = None
    # Whether new pairs should be marked persistent by default.
    persistent: bool | None = None
    # Extra --add-dir paths to whitelist on every new pair.
    extra_dirs: list[str] | None = None
    # Default allow-list for ``pair_invoke``. Mirrors ``PairSpec.allowed_invocations``.
    # The setter REJECTS empty list ``[]`` as a default (foot-gun: deny-all default
    # would silently break every fresh pair). Pass ``[]`` per-pair on pair_create
    # where the lockdown intent is explicit. To disable the field, omit it.
    allowed_invocations: list[str] | None = None

    @field_validator("permission_mode")
    @classmethod
    def _refuse_bypass_as_default(cls, v: PermissionMode | None) -> PermissionMode | None:
        if v == "bypassPermissions":
            raise ValueError(
                "permission_mode='bypassPermissions' cannot be set as a global default — "
                "every new pair would silently have no guardrails. Pass it per-pair on "
                "pair_create where the decision is visible."
            )
        return v

    @field_validator("allowed_invocations")
    @classmethod
    def _refuse_deny_all_as_default(cls, v: list[str] | None) -> list[str] | None:
        # Empty-list (deny-all) as a global default is a foot-gun: every new pair
        # would refuse all pair_invoke calls until the user noticed. Same principle
        # as the bypassPermissions guard above — force the explicit decision to
        # happen per-pair where it's visible.
        if v is not None and len(v) == 0:
            raise ValueError(
                "allowed_invocations=[] (deny-all) cannot be set as a global default — "
                "every new pair would silently refuse pair_invoke calls. Pass [] per-pair "
                "on pair_create where the lockdown intent is visible. To disable this "
                "default, omit the field or call pair_settings_reset."
            )
        return v


def defaults_path() -> Path:
    return pairs_dir() / "defaults.json"


def defaults_lock_path() -> Path:
    return pairs_dir() / "defaults.json.lock"


def _load_unlocked() -> PairDefaults:
    """Read defaults from disk; on any failure fall back to empty defaults
    (i.e. all hardcoded fallbacks). Logs a warning on corruption — never
    raises so a malformed file can't brick the entire MCP."""
    p = defaults_path()
    if not p.exists():
        return PairDefaults()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return PairDefaults.model_validate(data)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning(
            "defaults.json at %s is corrupt or unreadable (%s); falling back to "
            "hardcoded defaults. Inspect or delete the file to fix.",
            p, e,
        )
        return PairDefaults()


def _save_unlocked(d: PairDefaults) -> None:
    p = defaults_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(d.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    tmp.replace(p)


@contextmanager
def locked_defaults() -> Iterator[PairDefaults]:
    """Read-modify-write under filelock. Persists if the in-memory object
    changed during the context."""
    lock = FileLock(str(defaults_lock_path()), timeout=30)
    with lock:
        d = _load_unlocked()
        before = d.model_dump_json()
        yield d
        after = d.model_dump_json()
        if before != after:
            _save_unlocked(d)


def load_defaults() -> PairDefaults:
    """Read defaults without holding the lock (read-only views)."""
    lock = FileLock(str(defaults_lock_path()), timeout=10)
    with lock:
        return _load_unlocked()


def update_defaults(**fields) -> tuple[PairDefaults, list[str]]:
    """Apply ``fields`` to the stored defaults. Unknown fields → ValueError
    (don't silently drop). Returns ``(new_defaults, change_messages)`` where
    ``change_messages`` describes coercions/auto-resets done.

    Single-field UX: changing model alone may auto-reset effort to a
    model-appropriate default (Opus→xhigh, Sonnet→high, Haiku→None) — silent
    field clearing would surprise the user, so we surface the auto-reset in
    change_messages.
    """
    valid_fields = set(PairDefaults.model_fields.keys())
    unknown = set(fields.keys()) - valid_fields
    if unknown:
        raise ValueError(
            f"Unknown defaults field(s): {sorted(unknown)}. Valid fields: "
            f"{sorted(valid_fields)}."
        )

    messages: list[str] = []
    with locked_defaults() as d:
        # If model is changing and effort isn't being explicitly set in the
        # same call, auto-reset effort to the new model's appropriate default
        # so the agent doesn't end up with a stale incompatible (model, effort)
        # combo. Surface this so the user sees what happened.
        new_model = fields.get("model")
        explicit_effort = "effort" in fields
        if new_model is not None and not explicit_effort and new_model != d.model:
            # Only auto-reset if the current effort is incompatible with the new model.
            from claude_squared.models import default_effort_for_model
            current_effort = d.effort
            coerced, _ = coerce_effort_for_model(new_model, current_effort)
            new_default = default_effort_for_model(new_model)
            if coerced != current_effort:
                fields["effort"] = new_default
                messages.append(
                    f"effort auto-reset to '{new_default}' (was '{current_effort}', "
                    f"incompatible with new default model '{new_model}')."
                )

        # Apply explicit effort coercion + transparency message.
        if "effort" in fields:
            target_model = fields.get("model") or d.model or "opus"
            coerced, msg = coerce_effort_for_model(target_model, fields["effort"])
            if msg:
                messages.append(msg)
            fields["effort"] = coerced

        for k, v in fields.items():
            setattr(d, k, v)

        # Pydantic re-validates on assignment by default? No — BaseModel
        # doesn't unless validate_assignment=True. Validate manually by
        # round-tripping through model_validate.
        validated = PairDefaults.model_validate(d.model_dump())
        for k in fields:
            setattr(d, k, getattr(validated, k))
        return d.model_copy(), messages


def reset_defaults() -> None:
    """Remove the defaults file entirely; subsequent loads return empty
    defaults (all hardcoded fallbacks)."""
    p = defaults_path()
    lock = FileLock(str(defaults_lock_path()), timeout=10)
    with lock:
        if p.exists():
            p.unlink()
