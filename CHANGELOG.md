# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
