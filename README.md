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
- `pair_create(name, purpose, model, effort, permission_mode, system_prompt_append?, profile_name?, allowed_tools?, mcp_whitelist?, cwd?, extra_dirs?, persistent?, ultracode?, fallback_model?, allowed_invocations?, initial_message?, session_id?, parent_model?)`
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
- `pair_update(name, model?, effort?, permission_mode?, allowed_tools?, allowed_invocations?, cwd?, extra_dirs?, ultracode?, fallback_model?, purpose?)`
- `pair_clear(name, archive_old=True)` — rotate to fresh session_id; pinned config preserved
- `pair_compact(name, steering_prompt?, timeout_seconds=45, compact_timeout_seconds=600)` — native /compact (async-wrapped, v0.9.8+; degrades gracefully to an async handle past the sync cap)
- `pair_fork(name, new_name?)` — branch a pair into a new independent pair, keeping both (v0.10.0; like `/fork`, via native `--fork-session`)
- `pair_rewind_points(name, last_n?)` — list user-message boundaries to rewind to, with after-context (v0.10.0)
- `pair_rewind(name, to_point, archive?)` — rewind the conversation to before a chosen user message (v0.10.0; like `/rewind`, conversation-only, pre-rewind transcript archived)

### Skills / commands
- `pair_invoke(name, skill_name, args?)` — invoke a slash command via stream-json. Server-side allow-list enforcement (`PairSpec.allowed_invocations`) — see "Per-pair invocation allow-list" below.
- `pair_context(name)` — invoke /context for rich token-usage breakdown
- `pair_actions(name?)` — list curated MCP-level actions; if `name` given also probes the pair's installed slash commands and marks each ✓/✗ against the current allow-list

### Per-user defaults
- `pair_settings_get()` — show writable defaults + file paths + read-only env knobs
- `pair_settings_set(model?, effort?, permission_mode?, persistent?, ultracode?, fallback_model?, extra_dirs?, allowed_invocations?)` — fill defaults for new pairs (per-call args ALWAYS override defaults)
- `pair_settings_reset()` — delete defaults file → fall back to hardcoded fallbacks

> **Ultracode (v0.9.10+)**: Anthropic added an "Ultracode" mode (xhigh effort + dynamic workflows for maximum thoroughness) to CLI 2.1.165. It's surfaced via `--settings '{"ultracode": true}'`, **not** `--effort ultracode` (the CLI rejects that). Use `pair_create(ultracode=True)` or set it as a default via `pair_settings_set(ultracode=True)`. Compatible with any explicit `effort` value — effort and ultracode are independent fields.

> **Model-handling hardening (v0.11.0+)**: Anthropic now silently downgrades a session's model (e.g. to Opus 4.8) when a conversation trips a cyber/bio safety classifier, and flags subscription/trial model access as revocable. The `pair_send` reply footer surfaces both: **`🔄 MODEL CHANGED`** when the model that actually ran differs from what the pair requested (a safety downgrade, a `fallback_model` kicking in, or a capacity fallback), and **`⚠ SAFETY/BLOCK SIGNAL`** on a content-safety refusal (`⚠ TURN ENDED ABNORMALLY` for transient API errors — distinct, so a 529 overload doesn't read as a safety block). Set **`fallback_model`** (e.g. `pair_create(name, fallback_model="claude-opus-4-8")`) so a send whose primary is unavailable — including a trial model that lost access — transparently continues on the fallback instead of hard-erroring. A **`🆕 newer model available`** notice fires when your parent session is on a newer version of the pair's model family (compared against the parent, not a hardcoded list — so it can't go stale). *Limitation*: a hard **pause** that returns no result at all looks like a slow turn (an async handle), not a flagged signal — detection covers refuse-and-return and downgrade-and-continue, not a silent hang.

### Custom agents (global)
- `pair_agent_define(name, description, prompt, tools?, model?)` — write `~/.claude/agents/<name>.md`
- `pair_agent_list()` — list defined agents

## Terminal commands (run them yourself — no agent, no inference)

The tools above are **agent-facing** — Claude calls them. These are **for you**:
read-only subcommands on the `python -m claude_squared` entry point that read
`~/.claude/pairs/` directly and **never involve the agent or cost an inference**.

```bash
python -m claude_squared list                # all pairs: name, model, turns, last active, purpose
python -m claude_squared info <pair>         # full config + zero-inference context fill %
python -m claude_squared context <pair>      # just the context fill % (zero inference, from JSONL)
python -m claude_squared poll <task|pair>    # async task status (resolves id / pair name / prefix)
python -m claude_squared transcript <pair> [N]  # tail the last N conversation turns
python -m claude_squared status <pair>       # liveness from task files + main.log recency
python -m claude_squared log <pair> [N]      # tail the last N main.log activity lines
python -m claude_squared wait <task|pair>    # block until an async task finishes (background watcher)
python -m claude_squared stop <pair> [-y]    # interrupt a pair's current turn (asks Y/N; the one mutating cmd)
python -m claude_squared --help              # list these commands
python -m claude_squared                     # (no args) run the MCP server — what host configs invoke
```

`stop` is the only mutating terminal command — it writes a marker that the
server's runtime honors with a graceful in-band interrupt within ~1s (the pair
stays alive and can be re-sent). It confirms with Y/N unless you pass `-y`, and
only stops the *current* turn (queued sends still run).

`list` / `info` / `context` are pure disk reads (the context % comes from the
session JSONL's last turn, so it's free). The full categorized `/context`
breakdown — the big per-category token table — is only available through the
MCP `pair_context` tool, which costs a small inference on the pair.

> **Why not an in-chat `/pair-info` slash command?** A Claude Code plugin
> *can't* add a true client-side, model-free slash command like the built-in
> `/usage` — every plugin `/command` is a skill that renders into a prompt and
> triggers a model turn, and MCP prompts/resources feed the model too. The
> terminal subcommands above are the clean model-free path. (See CHANGELOG
> v0.9.11.)

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
   # Exit codes (v0.9.8): 0=done, 1=failed (work error), 2=not-found,
   #   3=timeout (default 1800s), 4=orphaned (MCP server died — supervision
   #   event, not a work failure), 5=stopped (pair_stop), 6=crashed
   #   (claude.exe died mid-turn). On notification, call pair_poll(task_id).
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

## Self-woken turns (v0.12.0)

A pair that launches background work — `Agent(run_in_background=True)`, a
background `Bash`, or a `Workflow` — ends its turn with a **placeholder** reply
("recon's out, I'll synthesize when it lands"). Claude Code's notify-and-resume
then fires (the persistent runtime keeps stdin open) and the pair **resumes on
its own** with the real deliverable. claude-squared tracks that continuation as
its own async task, so nothing about it is hidden:

- the placeholder reply's footer says `⏳ BACKGROUND WORK LAUNCHED (…)` — don't
  re-send; the continuation is coming;
- `pair_status(name)` says *idle — but N background tasks from the last turn
  are still running* while the work is out, then **self-woken turn in
  progress** (the active / slow / likely-hung gradient applies) once the pair
  wakes;
- `pair_poll(name, wait_seconds=30)` waits for the wake-up — even before the
  self-woken task exists — and shows its reply; `pair_poll(name)` resolves to
  the *latest* task, which may be the self-woken one: it's labeled, and the
  latest `pair_send` task is named next to it (the `wait.py` watcher by name
  works once the task exists);
- your **next** `pair_send` queues behind an in-progress continuation (FIFO — its
  result is never mistaken for your answer; a send from *another* MCP process
  queues behind it too) and its footer lists
  `⏮ N SELF-WOKEN TURN(S) completed since your last send` with task ids, log
  ranges and cost;
- `main.log` shows `=== SELF-WOKEN TURN (task …) ===`, `[background launch: …]`
  and the CLI's `task_notification` so the wake-up has a visible cause.

Sub-agent use is **not** gated — blocking fan-out stays the recommended shape,
and background launches are simply tracked. One caveat: a continuation that goes
silent for a full idle period (10 min) is finalized as `ABANDONED:` by a reaper so
it can't sit in flight forever; if the work later resumes, a fresh self-woken
turn opens.

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

- **Idle pairs expire: the Claude CLI deletes old session transcripts.** Claude Code runs a transcript-retention cleanup governed by `cleanupPeriodDays` in `~/.claude/settings.json` (unset → the CLI's own default, 30 days). It deletes exactly the session JSONLs a pair resumes from, so a pair left untouched for longer becomes unresumable and the next `pair_send` fails with `SessionMissing`. **If you rely on long-lived pairs, set `cleanupPeriodDays` to a large value** (e.g. `36500`) — this MCP cannot do it for you (it never writes your `settings.json`). It **cannot be disabled**: the minimum is `1`, and `0` is a trap — it fails validation, and [historically](https://github.com/anthropics/claude-code/issues/23710) disabled transcript *writing* altogether. Don't reach for `CLAUDE_CODE_SKIP_PROMPT_HISTORY` either; it disables transcript writes and breaks every pair. Note the sweep runs on interactive/desktop startup, not on the headless `claude -p` calls pairs use — so pairs never trigger it, and you can't test it through them. Recovery when it does happen: the pair's own `~/.claude/pairs/logs/<name>/main.log` is written by this MCP and is **not** affected, so the content survives; `pair_clear(name)` rotates the pair onto a fresh session while preserving all pinned config, after which it works normally. You lose the resumable conversation, not the record of it.
- **Premium models warn, they don't block (v0.12.0).** Plan-gated model families (currently Fable — own weekly limit, faster usage burn, included on Max 20x but not on Pro) raise a `💳 PREMIUM MODEL` confirmation notice whenever a pair is *switched* onto one (`pair_create` / `pair_update` / `pair_settings_set` / a per-send `override_model`) rather than being refused — it's the user's call, not the MCP's. The table encodes Anthropic's commercial terms and is dated in-source; verify before trusting it.
- Gemini adapter not implemented (Gemini's `--resume` uses index, not UUID — needs more design work).
- Permission denials are surfaced but not retried automatically; the calling agent decides what to do.
- No automatic compaction; the warning at ≥60% is informational. Caller must invoke `pair_compact`.
- Each `pair_send` spawns a fresh `claude --print --resume` subprocess (~2-4s overhead). For latency-sensitive use, a future "Option A" persistent stream-json process mode could amortize this.

## License

MIT (see LICENSE.txt).
