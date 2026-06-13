"""Pydantic schemas for pair MCP I/O."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


PermissionMode = Literal["auto", "acceptEdits", "plan", "default", "dontAsk", "bypassPermissions"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
Backend = Literal["claude"]

# Tools that cannot function in a headless ``claude --print`` pair: there is no
# interactive UI to render them, so the CLI denies the call regardless of
# permission_mode (even bypassPermissions) — and any content the model composed
# *inside* the call (questions, options, prose) is lost with the denial rather
# than surfacing in the assistant text channel. We strip these from every pair's
# toolset at spawn (see ``ClaudeAdapter._common_create_args``) so the model
# routes that content back as plain text instead. Single source of truth, shared
# by the spawn-time disallow list and the permission-handoff formatter so the two
# can't drift. AskUserQuestion is the confirmed offender (a pair is addressable
# only by its orchestrator, so a clarifying question belongs in its text reply).
HEADLESS_INCOMPATIBLE_TOOLS = ("AskUserQuestion",)


# Per-model effort capability matrix (verified empirically by the user 2026-05-13):
#   - Opus 4.7 / 4.7-1M / 4.6: all 5 levels
#   - Sonnet (any version):    [low, medium, high]  (no xhigh / max)
#   - Haiku (any version):     no effort levels at all
#   - Fable 5 (2026-06-09):    handled as "unknown" family BY DESIGN —
#     permissive passthrough verified against CLI 2.1.170 (--effort xhigh
#     accepted alongside claude-fable-5[1m], no warning). New families stay
#     "unknown" unless their capability DIFFERS from the permissive set;
#     listing them here when it doesn't would be hardcoding with zero
#     behavior delta.
# When the requested effort isn't supported by the model, we coerce to the
# nearest available level and surface a one-shot transparency message via
# ``coerce_effort_for_model``. The Pydantic validator below also enforces
# this invariant on the PairSpec itself so back-door update paths can't
# bypass the coercion.
_EFFORT_RANK = {None: -1, "low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}


def _model_family(model: str) -> str:
    """Reduce a full model string ('claude-sonnet-4-6', 'opus', etc.) to its family.

    Returns 'opus' / 'sonnet' / 'haiku' / 'unknown'. The family decides effort
    capability — version doesn't matter for that question.
    """
    m = model.lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "unknown"


def _allowed_efforts(model: str) -> list[str]:
    """Effort levels the model accepts. Empty list = model has no effort knob."""
    fam = _model_family(model)
    if fam == "opus":
        return ["low", "medium", "high", "xhigh", "max"]
    if fam == "sonnet":
        return ["low", "medium", "high"]
    if fam == "haiku":
        return []
    # Unknown family: be permissive (let the CLI surface its own error).
    return ["low", "medium", "high", "xhigh", "max"]


def coerce_effort_for_model(model: str, effort: str | None) -> tuple[str | None, str | None]:
    """Return ``(coerced_effort, transparency_message_or_None)``.

    Coercion rules:
      - Haiku: any non-None effort → None ("model X doesn't support effort levels")
      - Sonnet with xhigh/max: → ``high`` ("model X doesn't support 'xhigh'; coerced to 'high'")
      - Otherwise: passthrough (None, low/medium/high stay; opus accepts all 5)

    Caller is responsible for surfacing ``transparency_message`` to the user
    once at the moment of coercion — this function is pure (returns the same
    message repeatedly for the same input, but the caller decides when to show).
    """
    fam = _model_family(model)
    if fam == "haiku":
        if effort is None:
            return None, None
        return None, (
            f"model '{model}' doesn't support effort levels — "
            f"using None (was '{effort}')."
        )
    if fam == "sonnet":
        if effort in (None, "low", "medium", "high"):
            return effort, None
        if effort in ("xhigh", "max"):
            return "high", (
                f"model '{model}' doesn't support effort '{effort}' — "
                f"coerced to 'high' (sonnet's max effort level)."
            )
        # Unknown effort string; let the Literal validation reject downstream.
        return effort, None
    # Opus + unknown families accept everything; passthrough.
    return effort, None


def default_effort_for_model(model: str) -> str | None:
    """The model-appropriate default effort when none is specified.

    Mirrors Claude Code's own out-of-the-box defaults:
      - Opus → xhigh
      - Sonnet → high
      - Haiku → None (no effort knob)
      - Unknown → xhigh (permissive default; CLI will reject if model truly invalid)
    """
    fam = _model_family(model)
    if fam == "opus":
        return "xhigh"
    if fam == "sonnet":
        return "high"
    if fam == "haiku":
        return None
    return "xhigh"


# --- Model-id parsing for downgrade / version-drift detection (v0.11.0) -------
# These are deliberately SEPARATE from ``_model_family`` above. ``_model_family``
# buckets every unknown family to "unknown" (right for the effort matrix, wrong
# here — two distinct new families would compare equal). The functions below
# parse the RAW family token + a full version tuple so they can tell
# ``claude-opus-4-8`` from ``claude-opus-4-9`` (the exact silent-downgrade case)
# without false-firing on ``[1m]`` tier suffixes or dated snapshot ids.

def normalize_model_id(model: str) -> str:
    """Strip tier suffixes and snapshot dates so a model id parses cleanly.

    ``'claude-fable-5[1m]'`` → ``'claude-fable-5'``;
    ``'claude-haiku-4-5-20251001'`` → ``'claude-haiku-4-5'``. Lowercased.
    Without this, ``[1m]`` makes every 1M pair look "substituted" and a dated
    snapshot's ``20251001`` parses as a giant version component.
    """
    m = (model or "").strip().lower()
    m = re.sub(r"\[[^\]]*\]", "", m)      # drop [1m] / [200k] tier suffixes
    m = re.sub(r"-\d{6,8}$", "", m)       # drop trailing -YYYYMMDD snapshot date
    return m.strip()


def parse_model_id(model: str) -> tuple[str | None, tuple[int, ...]]:
    """Parse a model id/alias into ``(family, version_tuple)``.

    ``'claude-opus-4-8'`` → ``('opus', (4, 8))``;
    ``'claude-fable-5[1m]'`` → ``('fable', (5,))``;
    ``'opus'`` (bare alias) → ``('opus', ())`` — family known, version unknown;
    ``'claude-opus-4-6-fast'`` → ``('opus', (4, 6))`` — trailing non-numeric
    (``-fast``) ignored. ``(None, ())`` when no leading family token parses
    (e.g. legacy ``claude-3-5-sonnet`` family-in-the-middle naming) — callers
    treat ``None`` as "can't reason, stay silent".
    """
    m = normalize_model_id(model)
    rest = m[len("claude-"):] if m.startswith("claude-") else m
    fam_parts: list[str] = []
    ver_parts: list[int] = []
    for part in rest.split("-"):
        if part.isdigit():
            ver_parts.append(int(part))
        elif ver_parts:
            break  # non-numeric after version started → stop (drop '-fast' etc.)
        else:
            fam_parts.append(part)
    family = "-".join(p for p in fam_parts if p) or None
    return family, tuple(ver_parts)


def model_substitution_note(requested: str, served: str) -> str | None:
    """Note if ``served`` looks like a downgrade/substitution of ``requested``.

    Flags when (a) the served family differs from the requested family, or
    (b) same family but the served version is strictly LOWER than an *explicitly
    requested* version. An alias request (no explicit version, e.g. ``'opus'``)
    never flags within-family — an alias means "whatever the CLI resolves", so
    any same-family serve is acceptable. ``None`` when either side is
    unparseable (stay silent rather than false-alarm). Worded "this turn ran on"
    because ``--fallback-model`` retries the primary each turn — substitution is
    per-turn, not sticky.
    """
    rf, rv = parse_model_id(requested)
    sf, sv = parse_model_id(served)
    if rf is None or sf is None:
        return None
    if rf != sf:
        return f"requested family '{rf}' but this turn ran on '{normalize_model_id(served)}'"
    if rv and sv and sv < rv:
        return (f"requested '{normalize_model_id(requested)}' but this turn ran on "
                f"'{normalize_model_id(served)}' (older version, same family)")
    return None


def newer_version_available(current: str, candidate: str) -> str | None:
    """If ``candidate`` is a NEWER version of the same family as ``current``,
    return the candidate's normalized id; else ``None``.

    Both must parse to the same family with explicit version tuples and
    ``candidate_version > current_version``. Used for the "newer model in your
    family is available" notice (compared against the parent session's model —
    self-healing, no hardcoded 'latest' table to rot).
    """
    cf, cv = parse_model_id(current)
    nf, nv = parse_model_id(candidate)
    if cf is None or nf is None or cf != nf:
        return None
    if cv and nv and nv > cv:
        return normalize_model_id(candidate)
    return None


class PairSpec(BaseModel):
    """Persistent pair configuration stored in the registry."""

    name: str = Field(..., description="Unique addressable name")
    backend: Backend = "claude"
    session_id: str = Field(..., description="UUID of underlying session")
    purpose: str = ""
    model: str = "opus"
    # Nullable since haiku has no effort knob. The runtime/adapter omits the
    # ``--effort`` CLI arg when this is None. Default is xhigh (opus' default);
    # the model_validator below rewrites it to None for haiku, 'high' for
    # sonnet+xhigh/max combos, etc., as a safety net against back-door updates.
    effort: EffortLevel | None = "xhigh"
    permission_mode: PermissionMode = "auto"
    system_prompt_append: str | None = None
    profile_name: str | None = None  # references ~/.claude/pairs/profiles/<name>.md
    allowed_tools: list[str] | None = None
    mcp_whitelist: list[str] | None = None  # None = strict empty MCP config
    # MCP-level safety rail on ``pair_invoke``: which slash commands the calling
    # agent may invoke through the structured channel. ``None`` = allow all
    # (backward compat with pre-v0.8.1 — no surprise lockdowns). ``[]`` = explicit
    # lockdown (deny all). Patterns use ``fnmatch`` glob syntax — e.g.
    # ``["clear", "compact", "context", "mcp__claude_ai_*"]``.
    #
    # Threat-model note: this is **safety rails, not enforcement**. It blocks the
    # explicit ``pair_invoke(name, "X")`` channel only. A natural-language
    # ``pair_send(name, "please clear yourself")`` can still cause the pair to
    # self-invoke ``/clear``. The value is preventing **accidental** main-agent
    # missteps on first-class commands, not adversarial protection.
    #
    # Mutability: server-side enforcement layer, so ``pair_update`` changes take
    # effect on the next ``pair_invoke`` call WITHOUT runtime eviction (unlike
    # ``allowed_tools`` which is pinned at CLI startup and needs ``pair_clear``).
    allowed_invocations: list[str] | None = None
    cwd: str | None = None
    extra_dirs: list[str] | None = None  # additional --add-dir paths beyond cwd
    persistent: bool = False  # if True, runtime never evicted; otherwise 10-min idle eviction
    # v0.9.10: Ultracode mode — "xhigh effort + dynamic workflow orchestration"
    # per claude CLI 2.1.165+. When True, the adapter appends ``--settings
    # '{"ultracode": true}'`` to every spawn so the CLI activates ultracode at
    # the session level. Compatible with any ``effort`` setting (effort and
    # ultracode are independent fields in the CLI's internal data model; the
    # CLI sets effort to xhigh by default under ultracode but honors an
    # explicit --effort override). Default False — backward compatible.
    #
    # NOTE: ``--effort ultracode`` is NOT a valid effort value (verified: the
    # CLI rejects it with a warning). Ultracode is a SETTINGS key, not an
    # effort level — see HANDOFF.md "Critical CLI behaviors we depend on" for
    # the discovery story (binary strings: ``ultracodeKeywordTrigger``,
    # ``ultracode \xB7 xhigh effort + dynamic workflows for maximum
    # thoroughness``).
    ultracode: bool = False
    # v0.11.0: automatic fallback model(s) when the primary is overloaded or
    # unavailable. Passed to the CLI as ``--fallback-model`` (comma-separated
    # list, tried in order; the CLI re-tries the primary at the start of each
    # user turn, so a fallback is per-turn, not sticky). The main guard against
    # losing access to a subscription/trial-flagged model: with this set, a send
    # transparently continues on the fallback instead of hard-erroring, and the
    # substitution detector surfaces "ran on fallback Y instead of X". ``None``
    # = no fallback (default; the send hard-errors if the model is unavailable).
    fallback_model: str | None = None
    # v0.11.0: dedup marker for the "newer model in your family is available"
    # notice. Stores the parent-version normalized id we last warned about for
    # this pair, so the send-time check fires once per NEW release rather than
    # every send or every runtime spawn. See server._build_send_runner drift check.
    last_drift_notice: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    turn_count: int = 0
    total_cost_usd: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _coerce_effort_for_model(cls, values):
        """Safety-net coercion: ensure (model, effort) pair is internally consistent.

        Surface-level coercion via ``coerce_effort_for_model`` happens at the API
        boundary (pair_create / pair_settings_set) so the user gets a transparency
        message. This validator ensures any back-door path (pair_update,
        registry migration, manual edit) ALSO ends up with a valid combo —
        without surfacing messages here, since the validator can't reach the
        agent's response stream.
        """
        if not isinstance(values, dict):
            return values
        model = values.get("model", "opus")
        if "effort" in values:
            coerced, _msg = coerce_effort_for_model(model, values["effort"])
            values["effort"] = coerced
        return values


class Registry(BaseModel):
    version: int = 2
    pairs: dict[str, PairSpec] = Field(default_factory=dict)


class ContextStatus(BaseModel):
    tokens_used: int
    tokens_max: int
    percent: float
    warning: str | None = None


class PermissionDenial(BaseModel):
    tool_name: str
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None


class CreateResult(BaseModel):
    name: str
    session_id: str
    status: Literal["ready"] = "ready"
    transcript_path: str | None = None
    initial_response: str | None = None


class SendResult(BaseModel):
    name: str
    response: str
    session_id: str
    model_used: str
    cost_usd: float
    duration_ms: int
    permission_denials: list[PermissionDenial] = Field(default_factory=list)
    context: ContextStatus | None = None
    cache_read_tokens: int = 0
    needs_action: str | None = None
    # Audit pointer: the line range in the pair's main.log this turn produced.
    # Caller can fetch via pair_log(name, start=..., end=...) for retroactive review.
    log_path: str | None = None
    log_line_start: int | None = None
    log_line_end: int | None = None
    # Sub-agent logs spawned during this turn (one entry per Agent tool_use).
    subagent_logs: list[str] = Field(default_factory=list)
    # v0.11.0 model-handling hardening. All None on a normal turn; surfaced by
    # ClaudeAdapter._build_send_result and rendered by server._fmt_send_result.
    # The CLI SERVED a different model than the pair requested — a silent safety
    # downgrade (e.g. fable → opus-4-8 when a cyber/bio classifier trips) or
    # ``--fallback-model`` kicking in. Per-turn note (see model_substitution_note).
    model_substitution: str | None = None
    # The turn may have been blocked/paused or errored. Message text only —
    # ``safety_kind`` says how to LABEL it so a transient API overload isn't
    # mislabeled as a safety block (the boy-who-cried-wolf failure).
    safety_signal: str | None = None
    # "refusal" = genuine content-safety refusal (the only one that earns the
    # SAFETY-BLOCK banner); "error" = api_error_status / is_error (transient
    # overload, rate-limit, or tool error — rendered as a generic "abnormal").
    safety_kind: str | None = None
    # Raw stop_reason from the result envelope, kept when it's anything other
    # than the normal 'end_turn' (transparency for the human to interpret).
    stop_reason: str | None = None
    # "A newer model in this pair's family is available" — set by the server's
    # once-per-spawn send-time drift check (pair version vs parent version).
    drift_note: str | None = None


class CompactResult(BaseModel):
    name: str
    session_id: str
    pre_tokens: int
    post_tokens: int
    duration_ms: int
    trigger: str
    summary_preview: str | None = None


class ContextReport(BaseModel):
    """Result of pair_context (invokes /context in stream-json)."""

    name: str
    session_id: str
    model: str
    tokens_used: int
    tokens_max: int
    percent: float
    raw_markdown: str  # the full /context output for callers that want detail


class PairListItem(BaseModel):
    name: str
    purpose: str
    model: str
    backend: Backend
    last_active_at: datetime
    turn_count: int


class PairInfo(PairSpec):
    transcript_path: str | None = None
    transcript_exists: bool = False


class AsyncTaskState(BaseModel):
    task_id: str
    pair_name: str
    message: str
    # "stopped" = user called pair_stop while the task was running; distinct
    # from "failed" because the work was deliberately cancelled, not erroring.
    status: Literal["running", "done", "failed", "stopped"]
    started_at: datetime
    finished_at: datetime | None = None
    # v0.9.8: widened from ``SendResult | None`` to also carry ``CompactResult``.
    # pair_compact now goes through the same async-task machinery as pair_send
    # (graceful sync-cap degradation, so long compacts return an async handle
    # instead of host RPC timeout -32001). A "done" compact task's result has
    # the CompactResult shape. Pydantic smart-union disambiguates via unique
    # fields: ``response`` → SendResult, ``pre_tokens`` → CompactResult.
    # pair_poll dispatches its rendering via ``isinstance`` to pick the right
    # formatter. Existing pre-v0.9.8 task files (where result is always a
    # SendResult dict) deserialize cleanly under the smart-union semantics.
    result: "SendResult | CompactResult | None" = None
    error: str | None = None
    # PID of the MCP server process that owns this task. Set at start_task;
    # atexit cleanup only sweeps tasks owned by os.getpid(), and a startup
    # sweep marks tasks owned by no-longer-alive PIDs as failed. Without this
    # field, an MCP server shutdown would trash tasks being worked on by
    # other coexisting MCP processes (CLI install + Desktop install share
    # ~/.claude/pairs/async/ on disk). Optional for backward compatibility
    # with task state files written before this field existed.
    owner_pid: int | None = None


class ActionInfo(BaseModel):
    """Returned by pair_actions for discoverability."""

    actions: dict[str, str]  # name → one-line description
    pair_skills: list[str] | None = None  # if name was given, the slash_commands available in that pair
    pair_agents: list[str] | None = None
