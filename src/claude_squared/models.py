"""Pydantic schemas for pair MCP I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


PermissionMode = Literal["auto", "acceptEdits", "plan", "default", "dontAsk", "bypassPermissions"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
Backend = Literal["claude"]


# Per-model effort capability matrix (verified empirically by the user 2026-05-13):
#   - Opus 4.7 / 4.7-1M / 4.6: all 5 levels
#   - Sonnet (any version):    [low, medium, high]  (no xhigh / max)
#   - Haiku (any version):     no effort levels at all
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
    result: SendResult | None = None
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
