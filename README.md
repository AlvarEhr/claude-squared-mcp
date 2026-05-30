# claude-squared

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.txt)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/AlvarEhr/claude-squared-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AlvarEhr/claude-squared-mcp/actions/workflows/ci.yml)
[![MCP](https://img.shields.io/badge/MCP-server-green.svg)](https://modelcontextprotocol.io/)

A local MCP server that exposes long-running Claude Code CLI sub-sessions as addressable "pairs". Gives the calling Claude session true recursion (children can spawn their own sub-agents), persistent context across turns, per-pair specialization (system prompt, allowed tools, MCP scope), and native slash-command support via stream-json.

## Why

Claude Code's built-in `Agent` tool spawns single-shot sub-agents that can't recurse, can't be addressed by name across turns, and can't have specialized configs. `Agent Teams` adds named teammates but those still can't spawn their own sub-agents (no `Agent` tool inside them).

A pair is a `claude --print --resume <uuid>` session that you address by name. The MCP wraps the lifecycle so a pair becomes a first-class teammate that:
- Spawns its own sub-agents (it has the full `Agent` tool by default)
- Survives across your context compactions (registry on disk)
- Has specialized config pinned at create (system prompt, allowed tools, MCP scope)
- Supports native `/compact`, `/context`, `/skill-name` via stream-json
- Auto-tracks token usage and warns at ≥60% context fill

## Install

```bash
pip install -e .
```

Requires Python ≥3.10 and the `claude` CLI installed (Claude Code 2.1.117+ for `--session-id` support).

## Install — two paths

### As a Claude Code CLI MCP server

```bash
claude mcp add --scope user pair --transport stdio -- python -m claude_squared
claude mcp list   # should show: pair: python -m claude_squared - ✓ Connected
```

In a fresh Claude Code session, you'll see `mcp__pair__*` tools available.

### As a Claude Desktop extension (MCPB bundle)

Build and install:

```bash
python scripts/build_and_install_extension.py --install
```

This packs an `.mcpb` to `dist/claude-squared-<version>.mcpb` and extracts it into
your platform's Claude Extensions directory:
- Windows: `%APPDATA%\Claude\Claude Extensions\local.claude-squared\`
- macOS: `~/Library/Application Support/Claude/Claude Extensions/local.claude-squared/`
- Linux: `~/.config/Claude/Claude Extensions/local.claude-squared/`

Restart Claude Desktop after installing.

Manual install: drag the `.mcpb` into Claude Desktop → extensions panel, or extract into
the per-OS path above. (On the Windows-Store packaged Claude Desktop, the
`LocalCache\Roaming\Claude\...` directory mirrors the regular `Roaming\Claude\...` —
both point to the same files.)

## Quick start

```python
# In a Claude Code session with this MCP loaded:

# Create a pair
pair_create(name="reviewer", purpose="Reviews diffs",
            system_prompt_append="You are a senior code reviewer focusing on security.",
            allowed_tools=["Read", "Glob", "Grep", "Bash(git diff*)"])

# Send a message
result = pair_send(name="reviewer", message="Review the changes in src/auth.py")
print(result["response"])
print(result["context"])  # {tokens_used, tokens_max, percent, warning?}

# When context fills up (≥60% triggers a warning in result.context.warning)
pair_compact(name="reviewer")  # native /compact via stream-json
# Or with custom steering — focus on conversational arc + binding rules + in-flight state
pair_compact(name="reviewer", steering_prompt="Focus on what was reviewed and any unresolved findings.")
```

## Tools

### Lifecycle
- `pair_create(name, purpose, model, effort, permission_mode, system_prompt_append?, profile_name?, allowed_tools?, mcp_whitelist?, cwd?, extra_dirs?, persistent?, allowed_invocations?, initial_message?, session_id?, parent_model?)`
- `pair_adopt(name, session_id, ...)` — register an existing claude session
- `pair_forget(name, archive=True)` — remove from registry; optionally archives transcript

### Communication
- `pair_send(name, message, timeout_seconds=300, override_model?, override_effort?, override_permission_mode?)` — sync, FIFO-queued
- `pair_send_async(name, message, timeout_seconds=600, ...)` — returns task_id immediately
- `pair_poll(task_id)` — check async status

### Inspection
- `pair_list()` — short list
- `pair_info(name)` — full details + transcript path
- `pair_transcript(name, last_n=10)` — tail recent turns from JSONL
- `pair_actions(name?)` — discoverability: curated commands + (if name) pair-installed skills

### Mutation
- `pair_update(name, model?, effort?, permission_mode?, allowed_tools?, allowed_invocations?, cwd?, extra_dirs?, purpose?)`
- `pair_clear(name, archive_old=True)` — rotate to fresh session_id; pinned config preserved
- `pair_compact(name, steering_prompt?, timeout_seconds=600)` — native /compact

### Skills / commands
- `pair_invoke(name, skill_name, args?)` — invoke a slash command via stream-json. Server-side allow-list enforcement (`PairSpec.allowed_invocations`) — see "Per-pair invocation allow-list" below.
- `pair_context(name)` — invoke /context for rich token-usage breakdown
- `pair_actions(name?)` — list curated MCP-level actions; if `name` given also probes the pair's installed slash commands and marks each ✓/✗ against the current allow-list

### Per-user defaults
- `pair_settings_get()` — show writable defaults + file paths + read-only env knobs
- `pair_settings_set(model?, effort?, permission_mode?, persistent?, extra_dirs?, allowed_invocations?)` — fill defaults for new pairs (per-call args ALWAYS override defaults)
- `pair_settings_reset()` — delete defaults file → fall back to hardcoded fallbacks

### Custom agents (global)
- `pair_agent_define(name, description, prompt, tools?, model?)` — write `~/.claude/agents/<name>.md`
- `pair_agent_list()` — list defined agents

## State on disk

| Path | Purpose |
|---|---|
| `~/.claude/pairs/registry.json` | Pair registry (filelock-protected) |
| `~/.claude/pairs/profiles/<name>.md` | Reusable system-prompt profiles for `pair_create(profile_name=...)` |
| `~/.claude/pairs/archive/<name>-<ts>.jsonl` | Archived transcripts on `pair_forget(archive=True)` and `pair_clear` |
| `~/.claude/pairs/async/<task_id>.json` | Async task state (poll-able across process restarts) |
| `~/.claude/agents/<name>.md` | Custom agent definitions (visible to all Claude sessions globally) |
| `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` | Underlying claude session transcripts (managed by claude CLI, not us) |

## Async handles

Every `pair_send` goes through async-task machinery internally. If your wait
expires (`timeout_seconds` exceeded, or > server's RPC-hold cap of
`CLAUDE_PAIR_SYNC_CAP_SECONDS`, default 45s), the response is a "still running,
here's the task_id" handle — work continues, no second turn is queued.

Three ways to consume the handle:

1. **Notification-driven (recommended for long tasks)**: background-run the
   waiter; the harness fires you a completion notification when the task ends.

   ```python
   task_id = pair_send_async(name="scout", message="long task...")
   Bash(run_in_background=True,
        command=f"python ~/.claude/pairs/wait.py {task_id}")
   # Bash exits 0=done, 1=failed, 2=not-found, 3=timeout (default 1800s).
   # On notification, call pair_poll(task_id) for the response.
   ```

   The MCP server installs `~/.claude/pairs/wait.py` on startup — a
   stdlib-only script that polls the on-disk task state. Works regardless of
   whether `claude_squared` is importable from the agent's PATH-resolved
   `python` (which Desktop installs typically aren't, since the package is
   bundled via PYTHONPATH inside the MCP server's own subprocess).

2. **Manual quick status**: `pair_poll(task_id)` returns one-line status; if
   `status="done"` includes the full response text. **You can poll by pair
   name** — `pair_poll("scout")` resolves to that pair's most-recent task, so
   you don't have to copy the UUID (the output names the concrete task it
   picked). Pass an explicit id only when you need an older task.

3. **Live or just-completed turn content**:
   `pair_poll(name_or_task_id, with_turn_log=True)` shows the in-flight turn
   (running) or the just-completed turn (terminal status), with `[T-N]` tags
   drillable via `pair_tool_detail`. Use this for ALL statuses —
   `pair_transcript` is the broader conversation browser, not task-bound.

**Orphaned tasks** (`status` shows `⚠ ORPHANED`): the owning MCP server died
mid-turn (host watchdog / crash). This is a supervision event, *not* a work
error — the pair's `claude` subprocess runs in its own process group and usually
completes the work anyway. Verify via `pair_transcript` + your git/file state,
then `pair_send` to resume from the persisted session JSONL. `wait.py` reports
this with exit code 4.

**Universal fallback** (when `python` isn't on the agent's shell PATH):

```bash
until grep -q '"status": "done"\|"status": "failed"' \
    ~/.claude/pairs/async/<task_id>.json 2>/dev/null; do sleep 5; done
```

Same on-disk state file (`~/.claude/pairs/async/<task_id>.json`); the MCP writes
atomically so a watcher in a different process (different MCP install) sees the
result.

## Mid-flight config changes

`pair_update` propagation depends on the field category — three buckets:

| Category | Fields | When change takes effect |
|---|---|---|
| Per-send | `model`, `effort`, `permission_mode` | Next `pair_send` (registry write + runtime eviction → respawn with new values) |
| Server-side | `allowed_invocations` | Next `pair_invoke` — no eviction needed (MCP-layer enforcement, not pinned to CLI subprocess). Mutable freely. |
| Pinned-at-create | `allowed_tools`, `mcp_whitelist`, `system_prompt_append` | **Only after `pair_clear`** — the existing CLI subprocess was started with the OLD values; rotation creates a fresh session with the new pinned config |
| Pinned-at-spawn | `cwd`, `extra_dirs` | Next runtime spawn after eviction. `cwd` change ALSO moves the session JSONL across project dirs (rejected with recovery hint if the move fails) |

## Per-pair invocation allow-list (v0.8.1+)

`PairSpec.allowed_invocations: list[str] | None` gates which slash commands the calling agent may run via `pair_invoke`. Patterns use `fnmatch` glob syntax (stdlib).

| Value | Meaning |
|---|---|
| `None` (default) | Allow all (backward-compat with pre-v0.8.1) |
| `["clear", "compact", "mcp__claude_ai_*"]` | Allow only matching skills |
| `[]` | Deny all (explicit lockdown) |

Mutable via `pair_update(allowed_invocations=...)` **without runtime eviction** (server-side check, not pinned to the CLI subprocess). Settable as a per-user default via `pair_settings_set` — but `[]` (deny-all) is **refused** as a global default since it would silently break every fresh pair (same foot-gun guard as `bypassPermissions`).

**Threat model**: this is **safety rails, not enforcement**. `pair_invoke(name, "clear")` is blocked when `clear` isn't in the list, but `pair_send(name, "please clear yourself")` can still cause the pair to self-invoke `/clear` via natural language. The value is preventing **accidental** main-agent missteps on first-class commands like `/clear`, NOT adversarial protection.

## Design notes

- **`--model` and `--effort` re-passed every call** because they don't persist on resume in claude CLI.
- **`--append-system-prompt`, `--allowed-tools`, `--strict-mcp-config` pinned at create** because they DO persist.
- **Per-pair FIFO lock** in the server: concurrent `pair_send` to the same pair queue automatically.
- **Two execution paths in the adapter**: `--print --resume` for normal sends (fast, single JSON envelope); stream-json subprocess for slash commands (compact/context/invoke).
- **Auto-mode is the default `permission_mode`** — `--dangerously-skip-permissions` is intentionally not exposed.
- **Path encoding** for `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` lookups uses a single source: `cli_paths.encode_cwd_for_project()` (mirrors the CLI's `/[^a-zA-Z0-9]/g → "-"` regex). Three call sites (`adapters/claude.py`, `runtime.py`, `server.py`) import from this module, eliminating drift risk when the CLI changes its encoding.
- **CCD/Cowork users**: this MCP is intended to be loaded by vanilla `claude` CLI sessions. The CCD harness loads MCPs differently and may or may not surface these tools.

## Limits / known issues

- Gemini adapter not implemented (Gemini's `--resume` uses index, not UUID — needs more design work).
- Permission denials are surfaced but not retried automatically; the calling agent decides what to do.
- No automatic compaction; the warning at ≥60% is informational. Caller must invoke `pair_compact`.
- Each `pair_send` spawns a fresh `claude --print --resume` subprocess (~2-4s overhead). For latency-sensitive use, a future "Option A" persistent stream-json process mode could amortize this.

## License

MIT (see LICENSE.txt).
