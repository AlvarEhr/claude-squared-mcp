# claude-squared

Maintenance work and callback experiments after v0.13.0 are documented in
[the Codex maintenance handoff](docs/codex-maintenance-2026-09-08.md).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.txt)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/AlvarEhr/claude-squared-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AlvarEhr/claude-squared-mcp/actions/workflows/ci.yml)
[![MCP](https://img.shields.io/badge/MCP-server-green.svg)](https://modelcontextprotocol.io/)

A local MCP server that exposes long-running coding-agent CLI sub-sessions as addressable "pairs" — **Claude Code** sessions and, since v0.13.0, **OpenAI Codex** threads, behind the same tools and one backend-neutral vocabulary. Gives the calling Claude session true recursion (children can spawn their own sub-agents), persistent context across turns, per-pair specialization (system prompt, allowed tools, MCP scope), and native slash-command support via stream-json (Claude).

## Why

Claude Code's built-in `Agent` tool spawns single-shot sub-agents that can't recurse, can't be addressed by name across turns, and can't have specialized configs. `Agent Teams` adds named teammates but those still can't spawn their own sub-agents (no `Agent` tool inside them).

A pair is a `claude --print --resume <uuid>` session (or a `codex exec resume <thread-id>` thread) that you address by name. The MCP wraps the lifecycle so a pair becomes a first-class teammate that:
- Spawns its own sub-agents (it has the full `Agent` tool by default)
- Survives across your context compactions (registry on disk)
- Has specialized config pinned at create (system prompt, allowed tools, MCP scope)
- Supports native `/compact`, `/context`, `/skill-name` via stream-json (Claude)
- Auto-tracks token usage and warns at ≥60% context fill
- Can run on either backend — mixed fleets (a Claude reviewer next to a Codex implementer) use one set of tools and one vocabulary

## Install

```bash
pip install -e .
```

Requires Python ≥3.10 and at least one backend CLI: `claude` (Claude Code 2.1.117+ for `--session-id` support) and/or `codex` (the Codex CLI, 0.153+; the Desktop-managed build under `%LOCALAPPDATA%/OpenAI/Codex/bin/` is found automatically, or set `CLAUDE_PAIR_CODEX_PATH`).

## Install — two paths

### As a Claude Code CLI MCP server

```bash
claude mcp add --scope user pair --transport stdio -- python -m claude_squared
claude mcp list   # should show: pair: python -m claude_squared - ✓ Connected
```

In a fresh Claude Code session, you'll see `mcp__pair__*` tools available. A user-scope registration propagates to Claude Desktop sessions too.

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

Restart Claude Desktop after installing. (Don't install both paths — you'd get duplicate tools.)

> **Upgrading to v0.13.0**: the registry migrates in place to the backend-neutral vocabulary (a `registry.v2.backup.json` is kept). **Restart every open Claude session after installing** — an MCP process still running pre-0.13 code can't parse the new spellings and would drop those pairs if it ever wrote the registry back.

## Quick start

```python
# In a Claude Code session with this MCP loaded:

# Create a Claude pair
pair_create(name="reviewer", purpose="Reviews diffs",
            system_prompt_append="You are a senior code reviewer focusing on security.",
            allowed_tools=["Read", "Glob", "Grep", "Bash(git diff*)"])

# Create a Codex pair (model alias picks the current-generation Sol; 1m window for big repos)
pair_create(name="impl", backend="codex", model="sol", context_window="1m",
            permission_mode="workspace", cwd="/path/to/repo")

# Send a message — the reply comes back with a footer (ctx %, model, duration, log range)
pair_send(name="reviewer", message="Review the changes in src/auth.py")

# When context fills up (≥60% triggers a warning in the footer)
pair_compact(name="reviewer")  # native /compact via stream-json (Claude only)
pair_compact(name="reviewer", steering_prompt="Focus on what was reviewed and any unresolved findings.")
```

## Backends: Claude Code and Codex

| | Claude (`backend="claude"`, default) | Codex (`backend="codex"`) |
|---|---|---|
| Session | `claude --print --resume <uuid>`; warm stream-json runtime kept between sends | `codex exec --json … resume <thread-id>`; one process per turn, thread named by Codex |
| Models | any alias/id the CLI accepts (`opus`, `claude-opus-5`, `fable` …) | slugs from `~/.codex/models_cache.json` (`gpt-5.6-sol` …) or tier aliases `sol` / `terra` / `luna` / `astra` |
| Default model | `opus` (floating alias → the CLI's current Opus; 1M window) | current generation, **Sol > Terra > Luna**, read live from the cache |
| Never a default | Fable (plan-gated; warns, sticks once set) | Astra (plan-gated; warns, sticks once set) |
| Effort | low/medium/high/xhigh/max — default `high` | low/medium/high/xhigh/max + `ultra` (multi-agent; where the cache lists it) — default `high` |
| Context window | `default` (1M on Opus 5 / Fable already) or `1m` (`[1m]` tier) | `default` ≈ 258k usable, `1m` ≈ 828k usable (server clamps the marketing 1M) |
| Compact / skills / `/context` | native via stream-json | `pair_compact` drives the `codex app-server` (`thread/compact/start`; also auto-compacts near the limit); no slash channel (`pair_invoke` n/a); `pair_context` reads the rollout (zero inference) |
| Sub-agents | Agent tool; self-woken turns tracked | Codex's own delegation (`ultra`); no self-woken machinery |
| Permission enforcement | permission-mode classifier | OS sandbox (`read-only` / `workspace-write`) + the `codex-auto-review` "guardian" for escalations at `auto` |
| Footer extras | cost in USD | `📊 plan usage` (weekly quota %), `🛡 guardian review` verdicts |

**Floating vs pinned models.** A family/tier alias (`opus`, `sol`) follows whatever generation the backend currently resolves it to — a 5.6 → 5.7 bump upgrades the pair automatically. A numbered id (`claude-opus-5`, `gpt-5.6-sol`) stays pinned; you get a one-time `🆕` nudge when a newer generation of that tier is listed. Codex deprecation notices from the cache (e.g. `gpt-5.4-mini` retiring) surface the same way.

**Context window for Codex.** The default window is what Codex itself uses (~258k usable tokens). `context_window="1m"` passes `model_context_window=1000000` (what the Codex app writes); the server clamps it to the model's ceiling — 828,400 usable on the 5.6 family — and the auto-compact threshold is set to 90% of that effective window (the documented 900,000 would sit above the clamp and never fire). It costs more tokens per turn. **If the pair will scour large files or whole repos, pick `1m`** — a compaction (manual or automatic) costs a full-context turn and loses detail, so the window is the budget. `pair_create` says so when a Codex pair is created with the default window.

### Permission levels (one vocabulary, translated per backend)

| Level | Meaning | Claude `--permission-mode` | Codex `exec` |
|---|---|---|---|
| `read-only` | reads only; every write/exec is denied **and reported back to you** | `manual` (`default` on older CLIs) | `-s read-only` |
| `plan` | Claude's planning workflow (reads + a written plan) | `plan` | `-s read-only` (nearest) |
| `workspace` | edits + commands inside cwd/`extra_dirs` without review; outside → denied and reported | `acceptEdits` | `-s workspace-write` |
| `auto` (default) | in-workspace work auto-approved; escalations judged by the backend's own reviewer | `auto` | `--approve-for-me` (workspace-write sandbox + guardian review) |
| `unrestricted` | no gates at all (Codex: no sandbox, network included) | `bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox` |

Old spellings (`default`, `dontAsk`, `acceptEdits`, `bypassPermissions`, `workspace-write`, `approve-for-me` …) are accepted as input aliases forever; outputs and the registry use the neutral names. Nuances: Claude's `plan` still allows Bash, so it is looser than `read-only`; Claude's `workspace` auto-accepts edits but Bash still needs permission (denied and reported headless), while Codex's `workspace-write` allows sandboxed commands. On Windows, Codex needs `[windows] sandbox = "elevated"` in `~/.codex/config.toml` for `workspace-write` (the Codex app writes it; without it the sandbox silently runs read-only — the pair's footer says so).

A denied action produces a `⛔ PAIR HANDOFF` block in the reply with the pair's level, what was blocked, and the remedy (re-send with `override_permission_mode="unrestricted"` after the user authorizes, or widen `extra_dirs`). At `auto`, a Codex escalation is judged by the guardian model; its verdict and rationale appear as `🛡 guardian review: allow|deny — …`.

### Codex specifics worth knowing

- **Config isolation.** Pairs run with `--ignore-user-config` (your `~/.codex/config.toml` mounts MCP servers, Desktop plugins and hooks a pair shouldn't inherit — including this MCP itself) and re-add a whitelist of keys the sandbox needs: `windows.sandbox`, `sandbox_workspace_write`, `model_provider(s)`, `web_search`, `shell_environment_policy`. Extra `-c key=value` overrides go through `backend_options={"config": {...}}`.
- **Per-call model + effort + sandbox.** Codex never inherits them from the thread; the adapter re-passes them on every send, exactly like `claude --model`.
- **System prompt.** `codex exec` has no system-prompt flag; `system_prompt_append` / `profile_name` become the thread's first user message (persisted in the thread history).
- **Threads are tagged** `thread_source = claude-squared` in Codex's own store, so they're distinguishable in the Codex app.
- **`pair_rewind` works** (Codex resumes from the rollout JSONL); the adapter re-syncs Codex's sqlite history projection so `pair_fork` keeps working afterwards.
- **`pair_compact` works** through `codex app-server` (stdio JSON-RPC: `thread/resume` → `thread/compact/start`); `codex exec` itself has no compaction command (a literal `/compact` is just text to the model). The daemon starts the Desktop's plugin MCP servers first (~10 s); this MCP is excluded so nothing recurses. Codex compaction takes no steering text, and its post-compaction size is an estimate until the next reply's footer.
- **`pair_stop`** tree-kills the in-flight turn (the thread resumes cleanly on the next send); from another MCP process it writes the same stop marker the terminal `stop` command uses.
- **Unavailable models** (not on your ChatGPT plan) are refused by the API with HTTP 400 — `pair_create` warns when a slug isn't listed in the cache, and a failed turn is labeled `⛔ MODEL UNAVAILABLE`.

## Tools

### Lifecycle
- `pair_create(name, purpose, model?, effort?, permission_mode?, backend?, context_window?, system_prompt_append?, profile_name?, allowed_tools?, mcp_whitelist?, cwd?, extra_dirs?, persistent?, ultracode?, fallback_model?, allowed_invocations?, backend_options?, initial_message?, session_id?, parent_model?)`
- `pair_adopt(name, session_id, model?, effort?, permission_mode?, cwd?, backend?, context_window?)` — register an existing claude session / codex thread
- `pair_forget(name, archive=True)` — remove from registry; optionally archives transcript

### Communication
- `pair_send(name, message, timeout_seconds=45, hard_timeout_seconds?, override_model?, override_effort?, override_permission_mode?)` — sync, FIFO-queued
- `pair_send_async(name, message, hard_timeout_seconds?, ...)` — returns task_id immediately
- `pair_poll(task_id_or_name, with_turn_log?, wait_seconds?)` — check async status

### Inspection
- `pair_list()` — short list (Codex pairs show as `codex:<model>`, `[1m]` windows flagged)
- `pair_info(name)` — full details + transcript path
- `pair_transcript(name, last_n=10)` — tail recent turns (Claude JSONL or Codex rollout)
- `pair_status(name)` — liveness (active / slow / likely-hung; Codex: in-flight process)
- `pair_actions(name?)` — discoverability: curated commands + (if name) pair-installed skills (Claude)

### Mutation
- `pair_update(name, model?, effort?, permission_mode?, context_window?, backend_options?, allowed_tools?, allowed_invocations?, cwd?, extra_dirs?, ultracode?, fallback_model?, purpose?)` — the backend is fixed; a model from the other backend is refused
- `pair_clear(name, archive_old=True)` — rotate to a fresh session/thread; pinned config preserved
- `pair_compact(name, steering_prompt?, timeout_seconds=45, compact_timeout_seconds=600)` — Claude: native /compact via stream-json; Codex: `codex app-server` `thread/compact/start` (steering ignored). Async-wrapped, degrades gracefully to an async handle past the sync cap.
- `pair_fork(name, new_name?)` — branch a pair into a new independent pair, keeping both (Claude: native `--fork-session`; Codex: native `exec fork`)
- `pair_rewind_points(name, last_n?)` — list user-message boundaries to rewind to, with after-context
- `pair_rewind(name, to_point, archive?)` — rewind the conversation to before a chosen user message (conversation-only, pre-rewind transcript archived)

### Skills / commands
- `pair_invoke(name, skill_name, args?)` — invoke a slash command via stream-json (Claude). Server-side allow-list enforcement (`PairSpec.allowed_invocations`) — see "Per-pair invocation allow-list" below.
- `pair_context(name)` — rich token-usage breakdown: Claude via /context (one inference), Codex from the rollout (zero inference)
- `pair_actions(name?)` — list curated MCP-level actions; if `name` given also probes the pair's installed slash commands and marks each ✓/✗ against the current allow-list

### Per-user defaults
- `pair_settings_get()` — show writable defaults + file paths + read-only env knobs + the permission-level table + Codex models listed for your plan
- `pair_settings_set(model?, effort?, permission_mode?, backend?, context_window?, persistent?, ultracode?, fallback_model?, extra_dirs?, allowed_invocations?)` — fill defaults for new pairs (per-call args ALWAYS override defaults)
- `pair_settings_reset()` — delete defaults file → fall back to hardcoded fallbacks (claude / `opus`, high, auto, default window)

> **Ultracode (v0.9.10+, Claude)**: Anthropic's "Ultracode" mode (xhigh effort + dynamic workflows) is surfaced via `--settings '{"ultracode": true}'`, **not** `--effort ultracode`. Use `pair_create(ultracode=True)` or `pair_settings_set(ultracode=True)`. On Codex the equivalent opt-in is `effort="ultra"` (maximum reasoning + automatic task delegation).

> **Model-handling hardening (v0.11.0+)**: Anthropic silently downgrades a session's model when a conversation trips a cyber/bio safety classifier, and flags subscription/trial model access as revocable. The `pair_send` reply footer surfaces both: **`🔄 MODEL CHANGED`** when the model that actually ran differs from what the pair requested, and **`⚠ SAFETY/BLOCK SIGNAL`** on a content-safety refusal (`⚠ TURN ENDED ABNORMALLY` for transient API errors). Set **`fallback_model`** (Claude) so a send whose primary is unavailable transparently continues on the fallback. A **`🆕 newer model available`** notice fires when a newer generation of the pair's family/tier is available (Claude: compared against your parent session; Codex: against the models cache). *Limitation*: a hard **pause** that returns no result at all looks like a slow turn (an async handle), not a flagged signal.

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
python -m claude_squared context <pair>      # just the context fill % (zero inference)
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
server honors within ~1s (Claude: graceful in-band interrupt, the pair stays
alive; Codex: the one-shot turn process is tree-killed, the thread resumes on
the next send). It confirms with Y/N unless you pass `-y`, and only stops the
*current* turn (queued sends still run).

`list` / `info` / `context` are pure disk reads (the context % comes from the
session JSONL's last turn or the Codex rollout's last `token_count`, so it's
free). The full categorized `/context` breakdown is only available through the
MCP `pair_context` tool (a small inference on a Claude pair; free on Codex).

> **Why not an in-chat `/pair-info` slash command?** A Claude Code plugin
> *can't* add a true client-side, model-free slash command like the built-in
> `/usage` — every plugin `/command` is a skill that renders into a prompt and
> triggers a model turn, and MCP prompts/resources feed the model too. The
> terminal subcommands above are the clean model-free path. (See CHANGELOG
> v0.9.11.)

## State on disk

| Path | Purpose |
|---|---|
| `~/.claude/pairs/registry.json` | Pair registry (filelock-protected; v3 since 0.13.0, `registry.v2.backup.json` kept at migration) |
| `~/.claude/pairs/defaults.json` | Per-user defaults for new pairs |
| `~/.claude/pairs/profiles/<name>.md` | Reusable system-prompt profiles for `pair_create(profile_name=...)` |
| `~/.claude/pairs/archive/<name>-<ts>.jsonl` | Archived transcripts on `pair_forget(archive=True)`, `pair_clear`, `pair_rewind` |
| `~/.claude/pairs/async/<task_id>.json` | Async task state (poll-able across process restarts) |
| `~/.claude/pairs/logs/<name>/main.log` | The pair's activity log (both backends) + `main.idx.json` T-N index |
| `~/.claude/agents/<name>.md` | Custom agent definitions (visible to all Claude sessions globally) |
| `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` | Claude session transcripts (managed by the claude CLI, not us) |
| `~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl` | Codex thread rollouts (managed by the codex CLI; what `exec resume` reads) |
| `~/.codex/state_*.sqlite`, `thread_history_*.sqlite` | Codex's thread index + history projection (read for paths/verdicts; written only by `pair_rewind`'s re-sync) |
| `~/.codex/models_cache.json` | Codex model availability for your plan (read on every create/send) |

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
   `python`.

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

## Self-woken turns (v0.12.0, Claude)

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
turn opens. Codex pairs run one process per turn and have no self-woken machinery.

## Mid-flight config changes

`pair_update` propagation depends on the field category — three buckets:

| Category | Fields | When change takes effect |
|---|---|---|
| Per-send | `model`, `effort`, `permission_mode`, `context_window`, `backend_options`, `ultracode`, `fallback_model` | Next `pair_send` (registry write + runtime eviction → respawn with new values; Codex: every send is a fresh process) |
| Server-side | `allowed_invocations` | Next `pair_invoke` — no eviction needed (MCP-layer enforcement, not pinned to CLI subprocess). Mutable freely. |
| Pinned-at-create | `allowed_tools`, `mcp_whitelist`, `system_prompt_append` | **Only after `pair_clear`** — the existing session was started with the OLD values; rotation creates a fresh session with the new pinned config |
| Pinned-at-spawn | `cwd`, `extra_dirs` | Next spawn after eviction. A Claude `cwd` change ALSO moves the session JSONL across project dirs (rejected with recovery hint if the move fails); Codex threads aren't cwd-keyed |

## Per-pair invocation allow-list (v0.8.1+)

`PairSpec.allowed_invocations: list[str] | None` gates which slash commands the calling agent may run via `pair_invoke`. Patterns use `fnmatch` glob syntax (stdlib).

| Value | Meaning |
|---|---|
| `None` (default) | Allow all (backward-compat with pre-v0.8.1) |
| `["clear", "compact", "mcp__claude_ai_*"]` | Allow only matching skills |
| `[]` | Deny all (explicit lockdown) |

Mutable via `pair_update(allowed_invocations=...)` **without runtime eviction** (server-side check, not pinned to the CLI subprocess). Settable as a per-user default via `pair_settings_set` — but `[]` (deny-all) is **refused** as a global default since it would silently break every fresh pair (same foot-gun guard as `unrestricted`).

**Threat model**: this is **safety rails, not enforcement**. `pair_invoke(name, "clear")` is blocked when `clear` isn't in the list, but `pair_send(name, "please clear yourself")` can still cause the pair to self-invoke `/clear` via natural language. The value is preventing **accidental** main-agent missteps on first-class commands like `/clear`, NOT adversarial protection.

## Design notes

- **One vocabulary, translated per backend.** Permission levels, effort names, `context_window` and model aliases are backend-neutral on the tool surface; each adapter maps them to native flags. The old Claude spellings stay accepted as input aliases so nothing an agent learned before 0.13.0 breaks.
- **`--model` and `--effort` re-passed every call** because they don't persist on resume in either CLI.
- **`--append-system-prompt`, `--allowed-tools`, `--strict-mcp-config` pinned at create** because they DO persist (Claude).
- **Per-pair FIFO lock** in the server: concurrent `pair_send` to the same pair queue automatically (cross-process too; Codex's own thread-store lock is the belt).
- **Two execution paths in the Claude adapter**: a warm `--print --input-format stream-json` runtime for normal sends; a one-shot stream-json subprocess for slash commands (compact/context/invoke). **Codex is one `exec` process per turn** (phase 1; the `codex app-server` daemon is the warm-runtime candidate).
- **Auto is the default permission level** — `unrestricted` is refused as a global default.
- **Dynamic model availability.** The Claude CLI is the authority for Claude ids (unknown families pass through); `~/.codex/models_cache.json` is the authority for Codex (default choice, aliases, effort levels, windows, deprecations) — nothing model-specific is hardcoded beyond the never-default policy.
- **Path encoding** for `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` lookups uses a single source: `cli_paths.encode_cwd_for_project()`.
- **CCD/Cowork users**: this MCP is intended to be loaded by vanilla `claude` CLI sessions. The CCD harness loads MCPs differently and may or may not surface these tools.

## Limits / known issues

- **Idle Claude pairs expire: the Claude CLI deletes old session transcripts.** Claude Code runs a transcript-retention cleanup governed by `cleanupPeriodDays` in `~/.claude/settings.json` (unset → 30 days). It deletes exactly the session JSONLs a pair resumes from. **If you rely on long-lived pairs, set `cleanupPeriodDays` to a large value** (e.g. `36500`) — this MCP never writes your `settings.json`. It cannot be disabled (`0` is a trap: it fails validation and historically disabled transcript *writing*). The sweep runs on interactive/desktop startup, not on the headless calls pairs use. Recovery: the pair's own `main.log` survives; `pair_clear(name)` rotates onto a fresh session with all pinned config.
- **Premium models warn, they don't block.** Plan-gated models (Claude Fable — own weekly limit, faster burn; Codex Astra — frontier tier, most usage-hungry) raise a `💳 PREMIUM MODEL` notice whenever a pair is *switched* onto one, and are never chosen as defaults, but stick once set. The table encodes commercial terms and is dated in-source; verify before trusting it.
- **Codex has no slash-command channel** (`pair_invoke` hard-errors with the reason); `pair_compact` works via the app-server but costs a full-context turn and loses detail — pick `context_window="1m"` for large work.
- **A corrupt `registry.json` is refused for writes.** It reads as empty (a copy is kept as `registry.corrupt-<ts>.json`) and any mutation raises until the file is fixed or restored — previously the next mutation would have overwritten every pair with the empty view.
- **Codex `queue`** (Codex's native message queue) is deliberately not exposed: a message queued by one MCP process would be consumed by another process's send.
- Gemini adapter not implemented (Gemini's `--resume` uses index, not UUID — needs more design work).
- Permission denials are surfaced but not retried automatically; the calling agent decides what to do.
- No automatic compaction on Claude; the warning at ≥60% is informational. Caller must invoke `pair_compact`.

## License

MIT (see LICENSE.txt).
