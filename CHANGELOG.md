# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.4] — 2026-05-29

Bug fix from live testing: a pair created with `permission_mode="bypassPermissions"`
had an `AskUserQuestion` call blocked anyway, mislabeled as an auto-mode permission
denial, with the suggested bypass remedy inapplicable (already in bypass) — and the
~3 KB of report content the model composed inside the call was lost with the denial.

### Fixed
- **`AskUserQuestion` (and any headless-incompatible tool) is now stripped from
  every pair's toolset at spawn** via `--disallowed-tools`. A headless
  `claude --print` pair has no interactive UI to render `AskUserQuestion`, so the
  CLI denied it regardless of `permission_mode` (even `bypassPermissions`) — and
  whatever the model composed inside the call (questions, options, prose) was
  dropped with the denied call instead of surfacing as assistant text. With the
  tool removed, the model puts clarifying questions in its plain-text reply, which
  routes back to the orchestrator intact. New shared constant
  `models.HEADLESS_INCOMPATIBLE_TOOLS` is the single source of truth, used by both
  the spawn-time disallow list and the handoff formatter. (A pair is addressable
  only by its orchestrator, so it should never have `AskUserQuestion` — same as
  teammates/sub-agents.)
- **Permission-handoff message is now mode-aware and tool-aware.** It previously
  hardcoded "blocked by auto-mode" regardless of the pair's actual
  `permission_mode`, and always recommended retrying with `bypassPermissions` —
  useless when the pair was already in that mode. Now it reports the actual
  `permission_mode`, and partitions denials: headless-incompatible tools get a
  structural remedy ("cannot run headless; bypassPermissions will NOT help;
  re-request the content as plain text"), while genuine permission denials get the
  bypass remedy. Mixed denials get both.

### Note
- The spawn-time fix is pinned at session start, so it only applies to NEWLY
  created pairs. An existing pair needs `pair_clear` (or `pair_forget` +
  `pair_create`) to pick up the stripped toolset.

## [0.9.3] — 2026-05-29

Cleanup pass prompted by the Opus 4.8 release. **No urgent fix was needed** —
the model-alias passthrough auto-adapted to 4.8 the moment the CLI updated
(`--model opus` → `claude-opus-4-8`), validating the "don't hardcode versions"
design. These are the quality improvements surfaced while investigating.

### Changed
- **Hardened `match-parent` detection.** The MCP server's
  `CLAUDE_CODE_SESSION_ID` is frozen at spawn, so it goes stale whenever the
  server outlives the Claude session that launched it — and the exact-session
  JSONL lookup then silently fell back to the `opus` alias. New ladder adds a
  recency fallback: when the env-var session isn't found, detect the live
  parent from the **newest non-pair JSONL in the same cwd** modified within
  45s. Registered pair sessions are excluded (no feedback loop); concurrent
  sessions (multiple recent JSONLs) are treated as ambiguous and fall back to
  `opus` with an explicit message naming why. The transparency message now
  honestly reports a stale/unset env var and points at `parent_model=` as the
  reliable escape hatch.
- **1M context-window fallback.** `adapters/claude.py` previously defaulted to
  200k when the CLI didn't report a `contextWindow`. Now infers 1,000,000 for
  model names containing `1m` before that fallback — keeps the context-fill %
  honest on million-context pairs. (The current CLI reports the window
  correctly for Opus 4.8 1M; this is defensive for models that don't.)

### Fixed (docs)
- **Effort-default documentation accuracy.** The `pair_create` docstring and the
  `_HARDCODED_DEFAULTS` comment implied the default effort was `xhigh` / "derived
  from model", but the plain-model path intentionally leaves effort unset (omits
  `--effort`, letting the CLI apply its own per-model default). Corrected both —
  and noted this is the more future-proof behavior: a default pair never asserts
  an effort token that a future CLI might rename (e.g. the "Extra" relabeling of
  `xhigh` observed in newer menus — still `xhigh` as the flag value as of CLI
  2.1.156, confirmed via probe).

### Notes
- Confirmed `--effort extra` is rejected by CLI 2.1.156; the valid set remains
  `low, medium, high, xhigh, max`. "Extra" is a display label only. No effort
  code change made; widening the `EffortLevel` type is held as a documented
  watchpoint until/unless the flag value actually renames.

## [0.9.2] — 2026-05-27

### Added
- **`pair_poll(task_id, wait_seconds=N)`** — block-poll mode. When
  `wait_seconds > 0`, the call blocks up to N seconds for the task to reach a
  terminal state (done/failed/stopped) before returning. Uses an in-process
  threading.Event for instant wakeup the moment the task transitions — no
  internal polling, no wasted cycles. Capped at `CLAUDE_PAIR_SYNC_CAP_SECONDS`
  (default 45s) since the host's RPC timeout caps how long we can hold the
  call open. Intended for hosts without background Bash (e.g. Claude Cowork)
  where the documented "fire wait.py via Bash" pattern isn't available —
  block-polling beats spam-polling every few seconds.

### Changed
- **Async-handle Tip trimmed** from ~60 words to ~20. Old version was a
  multi-clause paragraph explaining Microsoft Store stub edge cases; new
  version just says "Bash watcher = hands-off notification. No background
  Bash (e.g. Cowork)? Use pair_poll(wait_seconds=N) to avoid spam-polling."
- **Sync-cap degradation message** now mentions `CLAUDE_PAIR_SYNC_CAP_SECONDS`
  by name (so users know the cap is tunable), shows the remaining patience
  budget explicitly, and recommends the new `pair_poll(wait_seconds=N)`
  syntax to wait the rest.

## [0.9.1] — 2026-05-27

### Fixed
- **`pair_send_async`'s `timeout_seconds` parameter renamed to
  `hard_timeout_seconds`** (breaking API change). The old name shared the
  spelling of `pair_send.timeout_seconds` but with the opposite semantics:
  in `pair_send` it's the agent's stated patience (work continues regardless);
  in `pair_send_async` it was always the hard auto-kill ceiling. Caused real
  user failures — a session passed `timeout_seconds=5` to `pair_send_async`
  thinking it was patience, the underlying claude operation got killed at 5s,
  and the misleading error message ("Increase timeout_seconds, or use
  pair_send_async to fire-and-forget") sent them looking for a `pair_poll`
  bug. Now matches `pair_send.hard_timeout_seconds` naming.
- **`CommandTimeout` error message rewritten** to be specific about what
  happened ("auto-killed after Xs by the hard_timeout_seconds ceiling") and
  give concrete recovery steps ("re-fire with a larger value, or omit it
  entirely — None means no ceiling, recommended for most uses").
- **`pair_send_async` docstring** now warns explicitly that
  `hard_timeout_seconds` is NOT your patience and that the default `None`
  (no ceiling) is almost always what you want — long Opus + sub-agent runs
  can legitimately take 30+ minutes.

## [0.9.0] — 2026-05-14

First public release. Project rebranded from `claude-pair-mcp` to
`claude-squared`. Internal versions 0.1 through 0.8.x preceded this; the
notable user-facing changes since v0.8.0 are listed below. See the project
history (further down) for the abbreviated pre-public arc.

### Added
- **Project name**: `claude-squared`. Python package: `claude_squared`. MCP
  server registration: `claude-squared`. Tool function names unchanged
  (`pair_create`, `pair_send`, …) — the noun is "pair", the brand is
  "claude-squared".
- **Per-pair invocation allow-list** (`PairSpec.allowed_invocations`): server-side
  safety rail on `pair_invoke`. `None` = allow all (backward compat); `[]` =
  explicit lockdown (deny all); list of `fnmatch` glob patterns = allow if any
  matches. Mutable via `pair_update` without runtime eviction. Foot-gun guard
  refuses `[]` as a global default in `pair_settings_set`. `pair_actions` marks
  each available skill ✓/✗ when an allow-list is set.
- **User-configurable defaults** (`pair_settings_get` / `pair_settings_set` /
  `pair_settings_reset`): writable defaults for `model`, `effort`,
  `permission_mode`, `persistent`, `extra_dirs`, `allowed_invocations`. Stored
  at `~/.claude/pairs/defaults.json`, filelock-protected.
- **Match-parent model detection** (`pair_create(model="match-parent")`):
  detects the calling Claude Code session's model from the session JSONL.
  Falls back to `opus` if detection fails.
- **Per-model effort coercion** (`models.coerce_effort_for_model`): Sonnet
  xhigh/max → high, Haiku any → None, with a transparency message in the
  response.
- **Standalone wait script** (`~/.claude/pairs/wait.py`): stdlib-only async-task
  waiter. Installed by the MCP server on startup. Lets the agent's `Bash`
  watcher work regardless of whether `claude_squared` is importable from the
  agent's PATH-resolved Python (Desktop installs in particular).
- **Encoding consolidation** (`cli_paths.encode_cwd_for_project`): single
  source for the `/[^a-zA-Z0-9]/g → "-"` regex used to compute
  `~/.claude/projects/<encoded-cwd>/<session>.jsonl` paths. Three call sites
  (`adapters/claude.py`, `runtime.py`, `server.py`) now import from this
  module — eliminates the drift risk that existed when each maintained its
  own copy of the regex.
- **`pair_update` runtime hint**: when `allowed_tools` / `system_prompt_append`
  / `mcp_whitelist` change, the response now includes a clear note that the
  change is pinned to the existing session and won't take effect until
  `pair_clear` rotates the session_id.

### Changed
- **Async-handle Bash command** uses `sys.executable` (the MCP server's own
  Python) + `shlex.quote`, instead of bare `python`. Avoids the Microsoft
  Store Python stub failure on Windows hosts and survives spaces in install
  paths.
- **Tool docstring trim**: the heaviest docstrings (`pair_create`, `pair_send`,
  `pair_send_async`, `pair_update`, `pair_settings_set`) deduplicated and the
  long agent-pedagogy sections (async handles, mid-flight semantics) moved to
  README. ~3.5k token savings off the loaded MCP cost.
- **Verbose responses** for `pair_create` and `pair_update` now include
  `transparency_msgs` (effort coercions, auto-resets, mid-flight notes)
  alongside the spec, so JSON consumers see the same signals as text-mode
  agents.
- **Cross-platform install dir** in the build script: Windows
  `~/AppData/Roaming/Claude/Claude Extensions/`, macOS
  `~/Library/Application Support/Claude/Claude Extensions/`, Linux
  `~/.config/Claude/Claude Extensions/`.
- **Logo refresh**: 256×256 PNG, optimized to 86 KB.

### Fixed
- **Catastrophic lockdown bypass** (v0.8.2): `_coerce_to_str_list` collapsed
  `[]` → `None` via `return out or None`, defeating the `allowed_invocations`
  lockdown intent across `pair_settings_set`, `pair_create`, and `pair_update`.
  A "lockdown" pair could invoke `/init` successfully because the allow-list
  had silently become `None` (allow-all). Fixed via new `preserve_empty=True`
  parameter; wired into the 3 allow-list call sites only.
- **Verbose-msg drop** in `pair_create` / `pair_update`: `verbose=True`
  returned only the persisted spec JSON, dropping `transparency_msgs` so JSON
  consumers got silent state changes (effort coerced from xhigh → high without
  explanation). Fixed via new `_verbose_dump_with_msgs(spec, msgs)` helper.
- **`compaction-prompt` skill coupling**: removed all references to the
  private `compaction-prompt` skill from `pair_compact` and the context
  warning text. Substantive guidance inlined.

## Project history (pre-public, abridged)

The project went through 25+ internal iterations before the public release.
The abbreviated arc:

- **v0.1**: initial scaffold; basic `pair_create` / `pair_send` / `pair_forget`;
  one-shot subprocess per send.
- **v0.3**: persistent stream-json runtime per pair (~60% perf win on warm
  sends); `pair_persist`, `pair_runtimes`.
- **v0.4–v0.6**: per-pair log folder, sub-agent extraction (one-shot),
  sequential `[T-N]` tool_use tags + persistent index files, `pair_tool_detail`.
- **v0.7.x**: defensive list-parser; cross-process `_PairLock`; sync→async
  graceful degradation (`pair_send` returns a still-running handle on timeout
  instead of blocking the host RPC); `pair_stop` / `pair_status` /
  `pair_invoke` / `pair_compact` / `pair_context` polish; decoupled
  `timeout_seconds` (agent's stated patience) from `CLAUDE_PAIR_SYNC_CAP_SECONDS`
  (server's RPC-hold cap).
- **v0.8.0**: user-configurable defaults; match-parent; cross-platform
  install; personalization scrub for fork-friendliness.
- **v0.8.1**: invocation allow-list; encoding consolidation;
  `compaction-prompt` skill decoupling.
- **v0.8.2**: lockdown-bypass bug fix; manifest scrub.
- **v0.9.0**: project rename + token trim + standalone wait.py + this changelog.
