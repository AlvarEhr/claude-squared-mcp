"""Dynamic Codex model availability — read from ``~/.codex/models_cache.json``.

The Codex CLI refreshes this cache from the server on every run; it lists ONLY
the models the signed-in plan can use (``visibility: "list"``), each with its
supported reasoning levels, context windows, priority and any scheduled
``upgrade``/retirement. Nothing model-specific is hardcoded here beyond the
POLICY knobs (which tiers may be defaults, in what order) — the cache is the
authority, exactly like the Claude CLI is for Claude ids.

Shape observed (codex-cli 0.153.4, 2026-09-07): top-level ``fetched_at``,
``etag``, ``client_version``, ``models[]``; per model ``slug``,
``display_name``, ``visibility`` ("list" | "hide"), ``priority`` (ascending =
server's preferred order: astra 1, sol 6, terra 7, luna 8, gpt-5.5 12 …),
``supported_reasoning_levels`` ([{effort, description}]),
``default_reasoning_level``, ``context_window`` (272000),
``effective_context_window_percent`` (95), ``max_context_window`` (872000 on
the 5.6/6 family, = the "1M" ceiling), ``upgrade`` ({model, migration_markdown,
retirement_at} for deprecated slugs).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from claude_squared.models import (
    CODEX_TIER_ALIASES,
    NEVER_DEFAULT_CODEX_TIERS,
    parse_codex_model,
)

# Preference order WITHIN the current generation when picking a default
# (user policy 2026-09-07: Sol > Terra > Luna). Tiers not listed here (e.g. a
# future name) rank after these by cache priority. Astra is excluded from
# defaults by ``NEVER_DEFAULT_CODEX_TIERS`` but stays selectable + sticky.
CODEX_DEFAULT_TIER_ORDER: tuple[str, ...] = ("sol", "terra", "luna")

# Marketing "1M" → the config values the Desktop app writes; the server clamps
# to the model's max_context_window (× effective percent) at runtime.
CODEX_1M_CONFIG: dict[str, int] = {
    "model_context_window": 1_000_000,
    "model_auto_compact_token_limit": 900_000,
}


def codex_home() -> Path:
    home = os.environ.get("CODEX_HOME")
    if home:
        return Path(home)
    return Path.home() / ".codex"


def models_cache_path() -> Path:
    return codex_home() / "models_cache.json"


def load_models_cache() -> dict[str, Any] | None:
    """The parsed cache, or ``None`` when missing/unreadable (callers degrade
    to permissive behavior + a note — never a hard failure)."""
    p = models_cache_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_models(include_hidden: bool = False) -> list[dict[str, Any]]:
    cache = load_models_cache() or {}
    out: list[dict[str, Any]] = []
    for m in cache.get("models") or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        if not include_hidden and (m.get("visibility") or "list") != "list":
            continue
        out.append(m)
    return out


def model_info(slug: str) -> dict[str, Any] | None:
    s = (slug or "").strip().lower()
    for m in list_models(include_hidden=True):
        if str(m.get("slug", "")).lower() == s:
            return m
    return None


def is_listed(slug: str) -> bool | None:
    """True/False when the cache is readable; ``None`` when it isn't."""
    if load_models_cache() is None:
        return None
    info = model_info(slug)
    return bool(info and (info.get("visibility") or "list") == "list")


def supported_efforts(slug: str) -> list[str] | None:
    info = model_info(slug)
    if not info:
        return None
    levels = info.get("supported_reasoning_levels") or []
    out = [str(l.get("effort")) for l in levels if isinstance(l, dict) and l.get("effort")]
    return out or None


def context_windows(slug: str) -> tuple[int, int] | None:
    """``(default_usable, max_usable)`` token windows for the slug, applying the
    cache's ``effective_context_window_percent`` (= what the rollout's
    ``model_context_window`` reports: 258,400 / 828,400 on the 5.6 family)."""
    info = model_info(slug)
    if not info:
        return None
    try:
        pct = float(info.get("effective_context_window_percent") or 100) / 100.0
        base = int(info.get("context_window") or 0)
        mx = int(info.get("max_context_window") or base)
        return int(base * pct), int(mx * pct)
    except (TypeError, ValueError):
        return None


def _tier_rank(tier: str | None) -> int:
    if tier in CODEX_DEFAULT_TIER_ORDER:
        return CODEX_DEFAULT_TIER_ORDER.index(tier)
    return len(CODEX_DEFAULT_TIER_ORDER)


def _candidates_for_default() -> list[tuple[tuple[int, ...], str | None, int, str]]:
    """(generation, tier, priority, slug) for every listed gpt-* model that
    is allowed to be a default."""
    out = []
    for m in list_models():
        slug = str(m.get("slug"))
        parsed = parse_codex_model(slug)
        if not parsed:
            continue  # codex-auto-review, gpt-reserve etc.
        gen, tier = parsed
        if tier in NEVER_DEFAULT_CODEX_TIERS:
            continue
        if m.get("upgrade"):
            continue  # scheduled for retirement — never a default
        try:
            prio = int(m.get("priority") or 999)
        except (TypeError, ValueError):
            prio = 999
        out.append((gen, tier, prio, slug))
    return out


def codex_default_model() -> tuple[str | None, str]:
    """Pick the default Codex model DYNAMICALLY.

    Policy: only the CURRENT numbered generation (the highest ``major.minor``
    that has a listed, default-eligible model), and within it Sol > Terra >
    Luna (then by cache priority). Astra is never a default. Returns
    ``(slug_or_None, reason)`` — ``None`` when the cache is unreadable or lists
    nothing eligible (the caller must then require an explicit model).
    """
    if load_models_cache() is None:
        return None, f"models cache not readable at {models_cache_path()}"
    cands = _candidates_for_default()
    if not cands:
        return None, "the models cache lists no default-eligible gpt-* model on this plan"
    # Prefer tiered models (sol/terra/luna family) when any exist; a bare
    # numbered slug (gpt-5.5) is an older generation without tiers.
    tiered = [c for c in cands if c[1] is not None]
    pool = tiered or cands
    top_gen = max(c[0] for c in pool)
    in_gen = [c for c in pool if c[0] == top_gen]
    in_gen.sort(key=lambda c: (_tier_rank(c[1]), c[2]))
    gen, tier, prio, slug = in_gen[0]
    others = ", ".join(c[3] for c in in_gen[1:]) or "none"
    return slug, (f"current generation {'.'.join(map(str, gen))}; preference "
                  f"{' > '.join(CODEX_DEFAULT_TIER_ORDER)}; also listed: {others}")


def resolve_codex_model(model: str | None) -> tuple[str, str | None]:
    """Turn what the caller typed into a concrete slug.

    - ``None`` / ``"codex"`` → the dynamic default.
    - a tier alias (``sol`` / ``terra`` / ``luna`` / ``astra``) → the newest
      listed generation carrying that tier (floating: re-resolved at every
      spawn, so a generation bump upgrades the pair automatically).
    - a full slug → itself, with an availability note when the cache doesn't
      list it (the CLI will reject with HTTP 400 if it truly isn't on the plan).
    Returns ``(slug, transparency_note_or_None)``.
    """
    m = (model or "").strip().lower()
    if not m or m == "codex":
        slug, why = codex_default_model()
        if slug is None:
            raise ValueError(
                f"no default Codex model could be determined ({why}); pass an "
                f"explicit model such as 'gpt-5.6-sol'."
            )
        return slug, f"codex default model → '{slug}' ({why})."
    if m in CODEX_TIER_ALIASES:
        best: tuple[tuple[int, ...], str] | None = None
        for mm in list_models():
            parsed = parse_codex_model(str(mm.get("slug")))
            if parsed and parsed[1] == m:
                if best is None or parsed[0] > best[0]:
                    best = (parsed[0], str(mm.get("slug")))
        if best is None:
            listed = ", ".join(str(x.get("slug")) for x in list_models()) or "(cache unreadable)"
            raise ValueError(
                f"no listed Codex model carries the tier '{m}' on this plan "
                f"(listed: {listed}). Pass an explicit slug or another tier."
            )
        return best[1], f"'{m}' → '{best[1]}' (floating alias: follows the newest listed generation)."
    listed = is_listed(m)
    if listed is False:
        names = ", ".join(str(x.get("slug")) for x in list_models())
        return m, (f"'{m}' is NOT listed as available on this plan (listed: {names}); "
                   f"the CLI will reject it with HTTP 400 if so — consider one of the listed ids.")
    return m, None


def newer_generation_available(slug: str) -> str | None:
    """For a PINNED slug (``gpt-5.6-sol``), the listed slug of the same tier in
    a NEWER generation (``gpt-5.7-sol``), else ``None``. Drives the nudge for
    pinned pairs; floating aliases upgrade themselves."""
    parsed = parse_codex_model(slug)
    if not parsed or parsed[1] is None:
        return None
    gen, tier = parsed
    best: tuple[tuple[int, ...], str] | None = None
    for m in list_models():
        p2 = parse_codex_model(str(m.get("slug")))
        if p2 and p2[1] == tier and p2[0] > gen:
            if best is None or p2[0] > best[0]:
                best = (p2[0], str(m.get("slug")))
    return best[1] if best else None


def upgrade_note(slug: str) -> str | None:
    """The cache's own deprecation notice for a slug scheduled for retirement."""
    info = model_info(slug)
    if not info:
        return None
    up = info.get("upgrade")
    if not isinstance(up, dict):
        return None
    target = up.get("model")
    when = up.get("retirement_at")
    md = str(up.get("migration_markdown") or "").strip().replace("\n", " ")
    parts = [f"'{slug}' is scheduled for retirement"]
    if when:
        parts.append(f"({when})")
    if target:
        parts.append(f"— Codex suggests '{target}'")
    if md:
        parts.append(f": {md[:160]}")
    return " ".join(parts)


def describe_availability() -> str:
    """One-paragraph summary for settings/create messages."""
    cache = load_models_cache()
    if cache is None:
        return f"Codex models cache not readable ({models_cache_path()})."
    rows = []
    for m in sorted(list_models(), key=lambda x: int(x.get("priority") or 999)):
        slug = m.get("slug")
        levels = ",".join(l.get("effort", "?") for l in (m.get("supported_reasoning_levels") or []) if isinstance(l, dict))
        cw = context_windows(str(slug))
        win = f"{cw[0]//1000}k/{cw[1]//1000}k" if cw else "?"
        flag = " (retiring)" if m.get("upgrade") else ""
        rows.append(f"{slug}{flag} [{levels}; ctx {win}]")
    fetched = cache.get("fetched_at", "?")
    return f"Codex models listed for this plan (cache {fetched}): " + "; ".join(rows)
