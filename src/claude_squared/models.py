"""Pydantic schemas for pair MCP I/O + the backend-neutral vocabularies.

v0.13.0 introduced a second backend (OpenAI Codex CLI) behind the same
``PairAdapter`` seam. Everything a caller types — permission level, effort,
context window, model alias — is backend-NEUTRAL here and translated to the
native flags inside each adapter. The old Claude-specific spellings
(``bypassPermissions``, ``acceptEdits``, ``[1m]`` model suffixes …) stay
accepted as INPUT aliases forever; outputs and the registry use the neutral
form.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Backend = Literal["claude", "codex"]
BACKENDS: tuple[str, ...] = ("claude", "codex")

# Kept as a public name for backward compatibility with older imports. Effort is
# validated per backend (see ``coerce_effort_for_model``), NOT by this alias —
# a hard Literal would make one codex pair with ``effort="ultra"`` poison the
# whole registry file (historian finding F3).
EffortLevel = str

# Tools that cannot function in a headless ``claude --print`` pair: there is no
# interactive UI to render them, so the CLI denies the call regardless of
# permission level (even unrestricted) — and any content the model composed
# *inside* the call (questions, options, prose) is lost with the denial rather
# than surfacing in the assistant text channel. We strip these from every pair's
# toolset at spawn (see ``ClaudeAdapter._common_create_args``) so the model
# routes that content back as plain text instead. Single source of truth, shared
# by the spawn-time disallow list and the permission-handoff formatter so the two
# can't drift. AskUserQuestion is the confirmed offender (a pair is addressable
# only by its orchestrator, so a clarifying question belongs in its text reply).
# Codex's equivalent (``request_user_input``) self-disables headless — nothing
# to strip there.
HEADLESS_INCOMPATIBLE_TOOLS = ("AskUserQuestion",)


# ============================================================================
# Permission levels — one vocabulary, translated per backend
# ============================================================================
#
# Loosest → strictest, each name says what the pair may DO rather than which
# CLI flag it comes from. Levels that exist natively on only one backend route
# to the nearest equivalent on the other; the table below is the contract.
#
#   level         meaning                                   Claude           Codex (exec)
#   ------------  ----------------------------------------  ---------------  ------------------------------
#   read-only     reads only; every write/exec is denied     default          -s read-only
#                 and REPORTED back to you (handoff)
#   plan          Claude's planning workflow (reads + a      plan             -s read-only  (nearest)
#                 written plan, no edits); Codex → read-only
#   workspace     edits + commands inside the workspace       acceptEdits      -s workspace-write
#                 (cwd + extra_dirs) without review; outside
#                 → denied and reported
#   auto          in-workspace actions auto-approved; risky   auto             --approve-for-me (workspace-
#                 / out-of-workspace actions reviewed by the                   write sandbox + escalations
#                 backend's own classifier/reviewer                            judged by the codex-auto-review
#                                                                              "guardian" model)
#   unrestricted  no gates at all: no sandbox, no approvals   bypassPermissions --dangerously-bypass-
#                                                                              approvals-and-sandbox
#
# Claude-specific nuance (documented, not encoded): ``plan`` still allows Bash,
# so it is looser than ``read-only`` on Claude; ``workspace`` (acceptEdits)
# auto-accepts EDITS but Bash still needs permission, which headless means
# "denied and reported" — Codex's workspace-write allows sandboxed commands.
# ``unrestricted`` on Codex also drops the network sandbox; on Claude there is
# no sandbox to drop. Gate it as hard as bypassPermissions always was.
PermissionLevel = Literal["read-only", "plan", "workspace", "auto", "unrestricted"]
PERMISSION_LEVELS: tuple[str, ...] = ("read-only", "plan", "workspace", "auto", "unrestricted")

# Input aliases — accepted forever, never emitted. Lower-cased lookup.
PERMISSION_ALIASES: dict[str, str] = {
    # Claude CLI spellings (pre-v0.13.0 canonical)
    "default": "read-only",
    "dontask": "read-only",
    "acceptedits": "workspace",
    "bypasspermissions": "unrestricted",
    # Codex spellings
    "workspace-write": "workspace",
    "approve-for-me": "auto",
    "danger-full-access": "unrestricted",
    # convenience
    "readonly": "read-only",
    "read_only": "read-only",
    "ro": "read-only",
    "edit": "workspace",
    "edits": "workspace",
    "bypass": "unrestricted",
    "full": "unrestricted",
    "full-access": "unrestricted",
    "none": "unrestricted",
}

# Neutral level → native Claude ``--permission-mode`` value. ``read-only`` maps
# to the ask-for-everything mode, spelled ``manual`` since CLI 2.1.258 (the
# older ``default`` is still accepted); ``ClaudeAdapter.native_permission``
# picks whichever spelling the installed CLI's --help lists.
CLAUDE_PERMISSION_MAP: dict[str, str] = {
    "read-only": "manual",
    "plan": "plan",
    "workspace": "acceptEdits",
    "auto": "auto",
    "unrestricted": "bypassPermissions",
}

# Neutral level → Codex exec flags: (sandbox, approval_flag). ``approval_flag``
# is one of None / "approve-for-me" / "bypass". Codex exec has NO ``-a`` flag
# (verified 0.153.4: "unexpected argument '-a'"); approval in exec is "never"
# unless one of the two flags below is given. ``--approve-for-me`` IMPLIES the
# workspace-write sandbox and the CLI rejects ``-s`` next to it, so the
# ``auto`` row's sandbox value is documentary — the adapter emits the flag alone.
CODEX_PERMISSION_MAP: dict[str, tuple[str | None, str | None]] = {
    "read-only": ("read-only", None),
    "plan": ("read-only", None),
    "workspace": ("workspace-write", None),
    "auto": ("workspace-write", "approve-for-me"),
    "unrestricted": (None, "bypass"),
}

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    "read-only": "reads only; every write/exec is denied and reported back (Claude: default; Codex: read-only sandbox)",
    "plan": "Claude planning workflow — reads + a written plan, no edits (Codex: routes to read-only)",
    "workspace": "edits + commands inside cwd/extra_dirs without review; outside → denied and reported (Claude: acceptEdits; Codex: workspace-write sandbox)",
    "auto": "in-workspace work auto-approved, escalations judged by the backend's own reviewer (Claude: auto; Codex: --approve-for-me guardian review)",
    "unrestricted": "no gates — no sandbox, no approvals (Claude: bypassPermissions; Codex: --dangerously-bypass-approvals-and-sandbox)",
}


def normalize_permission(value: str | None, *, default: str = "auto") -> str:
    """Map any accepted spelling to the neutral level. ``None`` → ``default``.

    Raises ``ValueError`` on an unknown name, listing what IS accepted.
    """
    if value is None:
        return default
    v = str(value).strip()
    if v in PERMISSION_LEVELS:
        return v
    lower = v.lower()
    if lower in PERMISSION_LEVELS:
        return lower
    if lower in PERMISSION_ALIASES:
        return PERMISSION_ALIASES[lower]
    raise ValueError(
        f"unknown permission level {value!r}. Use one of {list(PERMISSION_LEVELS)} "
        f"(old spellings still accepted: default/dontAsk→read-only, "
        f"acceptEdits→workspace, bypassPermissions→unrestricted)."
    )


def permission_native(level: str, backend: str) -> str:
    """Human-readable native mapping for a level on a backend (for messages)."""
    lvl = normalize_permission(level)
    if backend == "codex":
        sandbox, appr = CODEX_PERMISSION_MAP[lvl]
        if appr == "bypass":
            return "--dangerously-bypass-approvals-and-sandbox"
        if appr == "approve-for-me":
            return "--approve-for-me (workspace-write + guardian review)"
        return f"-s {sandbox}"
    return f"--permission-mode {CLAUDE_PERMISSION_MAP[lvl]}"


def permission_help_text() -> str:
    """One line per level — used by tool docstrings and settings output."""
    return "\n".join(f"  {lvl:12} {PERMISSION_DESCRIPTIONS[lvl]}" for lvl in PERMISSION_LEVELS)


# ============================================================================
# Backend inference + model-id helpers
# ============================================================================

# Codex tier aliases the user may type instead of a full slug. Resolved against
# the live models cache by ``codex_models.resolve_codex_model`` — never
# hardcoded to a generation here.
CODEX_TIER_ALIASES: tuple[str, ...] = ("sol", "terra", "luna", "astra")
_CODEX_SLUG_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?(?:-([a-z][a-z0-9-]*))?$")


def infer_backend(model: str | None) -> str:
    """Guess the backend from a model string. ``gpt-*`` / ``codex-*`` / a bare
    Codex tier alias → ``codex``; everything else (claude-*, opus, sonnet,
    haiku, fable, match-parent, unknown) → ``claude``."""
    m = (model or "").strip().lower()
    if not m:
        return "claude"
    if m.startswith("gpt-") or m.startswith("codex-") or m.startswith("o3") or m.startswith("o4"):
        return "codex"
    if m in CODEX_TIER_ALIASES or m == "codex":
        return "codex"
    return "claude"


def parse_codex_model(model: str) -> tuple[tuple[int, ...], str | None] | None:
    """``'gpt-5.6-luna'`` → ``((5, 6), 'luna')``; ``'gpt-5.5'`` → ``((5, 5), None)``;
    ``'gpt-6-astra'`` → ``((6,), 'astra')``. ``None`` when it isn't a gpt slug
    (``codex-auto-review``, a bare tier alias, a Claude id …)."""
    m = (model or "").strip().lower()
    mt = _CODEX_SLUG_RE.match(m)
    if not mt:
        return None
    major, minor, tier = mt.group(1), mt.group(2), mt.group(3)
    gen: tuple[int, ...] = (int(major),) + ((int(minor),) if minor is not None else ())
    return gen, tier


def is_pinned_model(model: str, backend: str | None = None) -> bool:
    """"Pinned" = the string names a specific numbered generation
    (``claude-opus-5``, ``gpt-5.6-sol``). "Floating" = a family/tier alias
    (``opus``, ``sol``) that follows whatever generation the backend currently
    resolves it to. Drives the auto-upgrade-on-generation-bump policy."""
    m = (model or "").strip().lower()
    be = backend or infer_backend(m)
    if be == "codex":
        return parse_codex_model(m) is not None
    _fam, ver = parse_model_id(m)
    return bool(ver)


def short_model_label(model: str) -> str:
    """Footer label: ``claude-opus-5`` → ``opus``; ``gpt-5.6-luna`` → ``luna``;
    ``gpt-5.5`` → ``gpt-5.5``; anything else unchanged."""
    m = (model or "").strip()
    if m.lower().startswith("claude-"):
        parts = m.split("-")
        return parts[1] if len(parts) > 1 else m
    parsed = parse_codex_model(m)
    if parsed and parsed[1]:
        return parsed[1]
    return m


# ============================================================================
# Effort
# ============================================================================
#
# Both backends share the names low/medium/high/xhigh/max; Codex adds ``ultra``
# ("maximum reasoning with automatic task delegation" — its ultracode-like
# multi-agent mode, opt-in, never a default). Per-model capability:
#   - Claude Opus / Fable / unknown families: all five (CLI is the authority)
#   - Claude Sonnet: low/medium/high
#   - Claude Haiku: no effort knob at all
#   - Codex: whatever ``models_cache.json`` lists for the slug (dynamic), falling
#     back to the six-level set when the cache is unavailable.
# Unsupported requests are coerced to the NEAREST supported level with a
# transparency message; the PairSpec validator re-applies the same rule so
# back-door writes can't store an invalid combo.
#
# DEFAULT EFFORT = "high" on both backends (user policy 2026-09-07): pairs are
# told what to do; raise per pair when the task warrants it.
DEFAULT_EFFORT = "high"
EFFORT_LEVELS_CLAUDE: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_LEVELS_CODEX_FALLBACK: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
_EFFORT_RANK = {None: -1, "low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}


def _model_family(model: str) -> str:
    """Reduce a Claude model string ('claude-sonnet-4-6', 'opus', etc.) to its
    family: 'opus' / 'sonnet' / 'haiku' / 'unknown'. Codex ids → 'unknown'."""
    m = (model or "").lower()
    if infer_backend(m) == "codex":
        return "unknown"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "unknown"


def _allowed_efforts(model: str, backend: str | None = None) -> list[str]:
    """Effort levels the model accepts. Empty list = model has no effort knob."""
    be = backend or infer_backend(model)
    if be == "codex":
        try:
            from claude_squared.codex_models import resolve_codex_model, supported_efforts
            slug = model
            try:
                slug, _ = resolve_codex_model(model)  # floating alias → today's slug
            except Exception:
                pass
            levels = supported_efforts(slug)
        except Exception:
            levels = None
        return list(levels) if levels else list(EFFORT_LEVELS_CODEX_FALLBACK)
    fam = _model_family(model)
    if fam == "sonnet":
        return ["low", "medium", "high"]
    if fam == "haiku":
        return []
    return list(EFFORT_LEVELS_CLAUDE)


def coerce_effort_for_model(model: str, effort: str | None,
                            backend: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(coerced_effort, transparency_message_or_None)``.

    ``None`` passes through untouched (= let the CLI default). An unsupported
    level is coerced to the nearest supported one by rank (Sonnet+xhigh →
    high; Codex luna+ultra → max; Haiku anything → None). Unknown effort
    strings on a model with a known level list are coerced too (with a message)
    rather than rejected, so a typo can't brick a registry entry.
    """
    if effort is None:
        return None, None
    eff = str(effort).strip().lower()
    allowed = _allowed_efforts(model, backend)
    if not allowed:
        return None, (
            f"model '{model}' doesn't support effort levels — using None (was '{eff}')."
        )
    if eff in allowed:
        return eff, None
    want = _EFFORT_RANK.get(eff)
    if want is None:
        fallback = DEFAULT_EFFORT if DEFAULT_EFFORT in allowed else allowed[-1]
        return fallback, (
            f"unknown effort '{eff}' for model '{model}' — using '{fallback}' "
            f"(supported: {', '.join(allowed)})."
        )
    ranked = sorted(allowed, key=lambda a: _EFFORT_RANK.get(a, 99))
    lower = [a for a in ranked if _EFFORT_RANK.get(a, 99) <= want]
    nearest = lower[-1] if lower else ranked[0]
    return nearest, (
        f"model '{model}' doesn't support effort '{eff}' — coerced to '{nearest}' "
        f"(supported: {', '.join(ranked)})."
    )


def default_effort_for_model(model: str, backend: str | None = None) -> str | None:
    """The default effort when none is specified: ``high`` everywhere except
    models with no effort knob (Haiku → None). Coerced against capability."""
    coerced, _ = coerce_effort_for_model(model, DEFAULT_EFFORT, backend)
    return coerced


# ============================================================================
# Context window (neutral): "default" or "1m"
# ============================================================================
#
# One field expresses the INTENT; the adapters translate and the footers report
# the MEASURED window (Claude: modelUsage.contextWindow; Codex: the rollout's
# token_count.model_context_window). Claude: "1m" appends the ``[1m]`` tier
# suffix to the model id (redundant on Opus 5 / Fable, which are 1M bare —
# harmless). Codex: "1m" passes ``-c model_context_window=1000000 -c
# model_auto_compact_token_limit=900000``; the server clamps it to the model's
# ``max_context_window`` (872k on the 5.6 family → 828,400 usable; default
# 272k → 258,400 usable). Verified 0.153.4, 2026-09-07.
ContextWindow = Literal["default", "1m"]
_CONTEXT_WINDOW_ALIASES = {
    "default": "default", "standard": "default", "normal": "default", "200k": "default",
    "256k": "default", "272k": "default", "small": "default",
    "1m": "1m", "1000000": "1m", "1000k": "1m", "extended": "1m", "large": "1m",
    "max": "1m", "million": "1m",
}


def normalize_context_window(value: str | None) -> str:
    if value is None:
        return "default"
    v = str(value).strip().lower().replace("[", "").replace("]", "")
    if v in _CONTEXT_WINDOW_ALIASES:
        return _CONTEXT_WINDOW_ALIASES[v]
    raise ValueError(f"unknown context_window {value!r}; use 'default' or '1m'.")


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
    """
    m = (model or "").strip().lower()
    m = re.sub(r"\[[^\]]*\]", "", m)      # drop [1m] / [200k] tier suffixes
    m = re.sub(r"-\d{6,8}$", "", m)       # drop trailing -YYYYMMDD snapshot date
    return m.strip()


def split_model_tier(model: str) -> tuple[str, str]:
    """``'claude-opus-5[1m]'`` → ``('claude-opus-5', '1m')``; no suffix →
    ``(model, 'default')``. Used to migrate ``[1m]`` into ``context_window``."""
    m = (model or "").strip()
    mt = re.search(r"\[([^\]]*)\]\s*$", m)
    if not mt:
        return m, "default"
    bare = m[: mt.start()].strip()
    try:
        cw = normalize_context_window(mt.group(1))
    except ValueError:
        cw = "default"
    return bare, cw


def parse_model_id(model: str) -> tuple[str | None, tuple[int, ...]]:
    """Parse a Claude model id/alias into ``(family, version_tuple)``.

    ``'claude-opus-4-8'`` → ``('opus', (4, 8))``; ``'opus'`` → ``('opus', ())``;
    ``'claude-opus-4-6-fast'`` → ``('opus', (4, 6))``. ``(None, ())`` when no
    leading family token parses. Codex slugs are handled by
    ``parse_codex_model`` — this returns ``(None, ())`` for them so the Claude
    drift/substitution logic stays silent.
    """
    m = normalize_model_id(model)
    if infer_backend(m) == "codex":
        return None, ()
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
    """Note if ``served`` looks like a downgrade/substitution of ``requested``
    (Claude). ``None`` when either side is unparseable or for Codex ids."""
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
    """If ``candidate`` is a NEWER version of the same Claude family as
    ``current``, return the candidate's normalized id; else ``None``."""
    cf, cv = parse_model_id(current)
    nf, nv = parse_model_id(candidate)
    if cf is None or nf is None or cf != nf:
        return None
    if cv and nv and nv > cv:
        return normalize_model_id(candidate)
    return None


# --- Premium (plan-gated, separate-limit, never-default) models --------------
# Families/tiers whose availability and cost depend on the subscription plan.
# NOT blocked — the user may legitimately want one — but selecting one is a
# usage/spending decision the *user* must make, so we surface a confirmation at
# every switch point. These are also NEVER chosen as a default (policy
# 2026-09-07): Claude never defaults to Fable, Codex never to Astra — but a pair
# explicitly set to one STAYS on it.
#
# THIS TABLE ROTS — it encodes commercial terms that change without notice and
# are not discoverable from the CLIs. Each entry carries the date it was last
# confirmed; re-verify before trusting an old one.
_PREMIUM_FAMILIES: dict[str, str] = {
    # Fable 5: included on Max 20x (own weekly limit, higher usage burn);
    # NOT included on Pro (billed as extra usage credits); Max 5x: the user
    # is on Max 5x as of 2026-09-07 and can select it.
    "fable": "is a premium model: it has its own separate weekly usage limit, "
             "consumes usage faster than standard models, and is only included "
             "on some plans (Max 20x: included; Pro: not included, billed as "
             "extra usage credits) (confirmed 2026-08-07)",
}
_PREMIUM_CODEX_TIERS: dict[str, str] = {
    # gpt-6-astra (2026-09-03): the frontier tier above the 5.6 family; listed
    # only on upgraded ChatGPT plans (Pro / Business / Enterprise; the user's
    # plan lists it since 2026-09-07) and by far the most usage-hungry.
    "astra": "is the frontier Codex tier: plan-gated (Pro/Business/Enterprise) "
             "and the most usage-hungry model on the plan's weekly limit "
             "(confirmed 2026-09-07)",
}
NEVER_DEFAULT_CLAUDE_FAMILIES: tuple[str, ...] = ("fable",)
NEVER_DEFAULT_CODEX_TIERS: tuple[str, ...] = ("astra",)


def premium_model_note(model: str) -> str | None:
    """Warn when ``model`` is a premium (plan-gated) family/tier; else ``None``.
    Advisory — nothing here blocks the model."""
    m = (model or "").strip()
    if not m:
        return None
    if infer_backend(m) == "codex":
        parsed = parse_codex_model(m)
        tier = parsed[1] if parsed else (m.lower() if m.lower() in CODEX_TIER_ALIASES else None)
        reason = _PREMIUM_CODEX_TIERS.get(tier or "")
        if reason is None:
            return None
        return (
            f"⚠ PREMIUM MODEL: '{m.lower()}' {reason}. This is allowed, but it is a "
            f"usage/spending decision — confirm the user explicitly asked for "
            f"{tier} before continuing, and switch back to the current-generation "
            f"default (e.g. 'sol') when done."
        )
    family, _ = parse_model_id(m)
    if family is None:
        return None
    reason = _PREMIUM_FAMILIES.get(family)
    if reason is None:
        return None
    return (
        f"⚠ PREMIUM MODEL: '{normalize_model_id(m)}' {reason}. "
        f"This is allowed, but it is a usage/spending decision — confirm the "
        f"user explicitly asked for {family} before continuing, and switch back "
        f"to a standard model (e.g. 'opus') when done."
    )


# ============================================================================
# Schemas
# ============================================================================

class PairSpec(BaseModel):
    """Persistent pair configuration stored in the registry."""

    # Preserve additive fields written by a newer compatible process.
    model_config = {"extra": "allow"}

    name: str = Field(..., description="Unique addressable name")
    # Which CLI runs this pair. Fixed at create (a Claude session can't become a
    # Codex thread); inferred from the model when not given.
    backend: Backend = "claude"
    session_id: str = Field(..., description="Claude session UUID / Codex thread id")
    purpose: str = ""
    # v0.12.0: self-woken turns that completed since the last pair_send, parked
    # here by the runtime's reader thread (see runtime._record_self_woken) and
    # popped by the next send for its ⏮ footer. On the spec — not in runtime
    # memory — because the next send respawns the runtime (see is_stale) and
    # because any MCP process should see them. Capped at the newest 25.
    self_woken_pending: list[dict[str, Any]] = Field(default_factory=list)
    # Model id or alias. Bare (no ``[1m]`` — that lives in ``context_window``).
    # Floating aliases (``opus``, ``sol``) follow the backend's current
    # generation; pinned ids (``claude-opus-5``, ``gpt-5.6-sol``) stay put.
    model: str = "opus"
    # Nullable since haiku has no effort knob. The adapter omits the effort arg
    # when this is None. Default "high" (policy 2026-09-07) on both backends;
    # the validator coerces it against the model's capability.
    effort: str | None = DEFAULT_EFFORT
    # Neutral level (see PERMISSION_LEVELS). Aliases are normalized on load.
    permission_mode: str = "auto"
    # v0.13.0: "default" | "1m". Intent only — footers report the measured window.
    context_window: str = "default"
    # v0.13.0: per-backend escape hatch for knobs with no neutral equivalent.
    # Documented keys — codex: ``config`` (dict of extra ``-c key=value``
    # overrides), ``args`` (list of raw extra exec args), ``sandbox`` /
    # ``approval`` (native override of the permission translation:
    # sandbox ∈ read-only|workspace-write|danger-full-access, approval ∈
    # none|approve-for-me|bypass). Claude: none yet (every native mode is
    # reachable through the level aliases).
    backend_options: dict[str, Any] = Field(default_factory=dict)
    system_prompt_append: str | None = None
    profile_name: str | None = None  # references ~/.claude/pairs/profiles/<name>.md
    allowed_tools: list[str] | None = None
    mcp_whitelist: list[str] | None = None  # None = strict empty MCP config
    # MCP-level safety rail on ``pair_invoke``: which slash commands the calling
    # agent may invoke through the structured channel. ``None`` = allow all;
    # ``[]`` = explicit lockdown (deny all). Patterns use ``fnmatch`` glob syntax.
    # Safety rails, not enforcement — see README "Per-pair invocation allow-list".
    allowed_invocations: list[str] | None = None
    cwd: str | None = None
    extra_dirs: list[str] | None = None  # additional --add-dir paths beyond cwd
    persistent: bool = False  # if True, runtime never evicted; otherwise 10-min idle eviction
    # v0.9.10: Ultracode mode (Claude) — ``--settings '{"ultracode": true}'`` at
    # every spawn. NOT an effort value. Codex's analog is ``effort="ultra"``.
    ultracode: bool = False
    # v0.11.0: automatic fallback model(s) — Claude ``--fallback-model``. Codex
    # has no analog (hard-errors at create if set on a codex pair).
    fallback_model: str | None = None
    # v0.11.0: dedup marker for the "newer model available" notice.
    last_drift_notice: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    turn_count: int = 0
    total_cost_usd: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values):
        """Safety-net normalization on EVERY load/construct: legacy permission
        spellings → neutral; ``[1m]`` in the model → ``context_window``;
        backend inferred when absent; effort coerced against the model so
        back-door writes (pair_update, registry migration, hand edits) can't
        store an invalid combo. Messages are surfaced at the API boundary,
        not here."""
        if not isinstance(values, dict):
            return values
        model = values.get("model", "opus") or "opus"
        bare, cw = split_model_tier(str(model))
        if bare != model:
            values["model"] = bare
            if cw == "1m":
                values["context_window"] = "1m"
        if not values.get("backend"):
            values["backend"] = infer_backend(values.get("model"))
        if "permission_mode" in values and values["permission_mode"] is not None:
            values["permission_mode"] = normalize_permission(values["permission_mode"])
        if "context_window" in values and values["context_window"] is not None:
            try:
                values["context_window"] = normalize_context_window(values["context_window"])
            except ValueError:
                values["context_window"] = "default"
        if values.get("backend_options") is None:
            values["backend_options"] = {}
        if "effort" in values:
            coerced, _msg = coerce_effort_for_model(
                values.get("model", "opus"), values["effort"], values.get("backend"),
            )
            values["effort"] = coerced
        return values


class Registry(BaseModel):
    model_config = {"extra": "allow"}
    version: int = 3
    pairs: dict[str, PairSpec] = Field(default_factory=dict)


class ContextStatus(BaseModel):
    tokens_used: int
    tokens_max: int
    percent: float
    warning: str | None = None
    # Absent on older stored results: do not assume their fallback was native.
    window_source: Literal["reported", "estimated"] | None = None


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
    # None on backends that bill by plan quota rather than USD (Codex).
    cost_usd: float | None = 0.0
    duration_ms: int
    permission_denials: list[PermissionDenial] = Field(default_factory=list)
    context: ContextStatus | None = None
    cache_read_tokens: int = 0
    needs_action: str | None = None
    # Audit pointer: the line range in the pair's main.log this turn produced.
    log_path: str | None = None
    log_line_start: int | None = None
    log_line_end: int | None = None
    # Sub-agent logs spawned during this turn (one entry per Agent tool_use).
    subagent_logs: list[str] = Field(default_factory=list)
    # v0.11.0 model-handling hardening — see ClaudeAdapter._build_send_result.
    model_substitution: str | None = None
    safety_signal: str | None = None
    # "refusal" | "usage_limit" | "model_unavailable" | "error"
    safety_kind: str | None = None
    stop_reason: str | None = None
    drift_note: str | None = None
    premium_note: str | None = None
    # v0.12.0 self-woken turns (Claude runtime only).
    background_launches: list[str] = Field(default_factory=list)
    self_woken_completed: list[dict[str, Any]] = Field(default_factory=list)
    self_woken_waited_s: float | None = None
    terminal_reason: str | None = None
    # v0.13.0: which backend produced this turn (footers/labels).
    backend: str = "claude"
    # v0.13.0 (Codex): plan-quota position from the rollout's rate_limits —
    # "15% of the weekly limit used, resets 2026-09-14 …". The USD cost analog.
    plan_usage: str | None = None
    # v0.13.0 (Codex, auto level): verdicts the codex-auto-review "guardian"
    # returned for escalations this turn ("allow — rationale" / "deny — …").
    guardian_notes: list[str] = Field(default_factory=list)
    # v0.13.0: free-form transparency notes the adapter wants surfaced once
    # (e.g. "sandbox degraded to read-only: windows.sandbox missing").
    notes: list[str] = Field(default_factory=list)


class CompactResult(BaseModel):
    name: str
    session_id: str
    pre_tokens: int
    post_tokens: int
    duration_ms: int
    trigger: str
    summary_preview: str | None = None


class ContextReport(BaseModel):
    """Result of pair_context (Claude: /context; Codex: rollout token_count)."""

    name: str
    session_id: str
    model: str
    tokens_used: int
    tokens_max: int
    percent: float
    raw_markdown: str  # the full breakdown for callers that want detail


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
    # v0.9.8: ``SendResult | CompactResult``. Pydantic smart-union disambiguates
    # via unique fields: ``response`` → SendResult, ``pre_tokens`` → CompactResult.
    result: "SendResult | CompactResult | None" = None
    error: str | None = None
    # PID of the MCP server process that owns this task (orphan supervision).
    owner_pid: int | None = None
    # Additive metadata: old task files are treated as already executing.
    queued: bool = False
    execution_started_at: datetime | None = None


class ActionInfo(BaseModel):
    """Returned by pair_actions for discoverability."""

    actions: dict[str, str]  # name → one-line description
    pair_skills: list[str] | None = None  # if name was given, the slash_commands available in that pair
    pair_agents: list[str] | None = None
