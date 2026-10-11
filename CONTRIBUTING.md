# Contributing to claude-squared

Contributions welcome — bug reports, fixes, and discussion of design changes
all useful. This is a single-author project ("Alvar") at present, so don't be
surprised if PRs sit for a while; ping the issue thread if a review takes more
than a week.

## Development setup

Requirements:
- Python ≥ 3.10
- The `claude` CLI installed (Claude Code 2.1.117+ for `--session-id` support)

```bash
git clone https://github.com/AlvarEhr/claude-squared-mcp.git
cd claude-squared-mcp
pip install -e .
```

For the **CLI install** (tools surface in vanilla `claude` CLI sessions):

```bash
claude mcp add --scope user pair --transport stdio -- python -m claude_squared
claude mcp list   # should show: pair: python -m claude_squared - ✓ Connected
```

For the **Claude Desktop install** (build the bundled `.mcpb`):

```bash
python scripts/build_and_install_extension.py --install --clean
```

This vendors the Python deps into `extension/server/lib/`, packs the
`.mcpb` to `dist/claude-squared-<version>.mcpb` (verified against `src/`
before it replaces anything), and unpacks it into the per-OS Claude
Extensions directory. Restart Claude Desktop to pick up the new bundle. The
bundle is a release artifact (GitHub Releases), not tracked in git.

## Running tests

The maintained offline tests need no MCP server, no `claude` and no `codex`
CLI, and never touch your real `~/.claude/pairs` (each suite runs in its own
process with a temporary `CLAUDE_HOME`):

```bash
python scripts/run_offline_tests.py
```

That runs every suite listed in the script (27 as of 0.17.0): the maintained
`tests/smoke_*.py` scripts (each prints `PASS:` and exits non-zero on any
failed assertion) and the `unittest` suites in `tests/test_*.py`. CI runs the
same command on every push across the OS/Python matrix. Four legacy scripts
(`smoke.py`, `smoke_runtime.py`, `smoke_streamjson.py`, `smoke_v05.py`)
predate the v0.10 API and are not part of the run. A new test file needs its
own entry in `scripts/run_offline_tests.py`.

Live tests cost real usage; run them before a release — and after a Codex CLI
update — not in CI. Each uses a temporary `CLAUDE_HOME`; the Codex ones use
your real `codex` auth and delete the threads they create.

- `python tests/smoke_codex.py --live` — the Codex backend (~15 calls on the
  cheapest model)
- `python tests/smoke_live_0140.py` — Claude lifecycle races (a handful of
  short Opus turns)
- `python tests/smoke_live_handoff.py` — `pair_handoff` (Haiku + Luna turns;
  the size gate uses a synthetic oversized session and spends nothing)
- `python tests/smoke_live_connectors.py` — connectors (uses the throwaway
  server in `tests/fixtures/mcp_probe_server.py`; it temporarily registers
  that server with `codex mcp add` and always removes it)
- `python tests/smoke_live_v016.py` — the pair watcher, background
  protection, interim triage and context alert (four Haiku pairs, ~6–8 min)
- `python tests/smoke_live_codex_detach.py`,
  `python tests/smoke_live_codex_context.py` — Codex turns surviving their
  MCP server, and the Codex context alert (a few short Luna turns each)
- `python tests/smoke_live_billing.py [--real]` — API-key billing (0.18.0): a pair
  switched onto a FAKE key must fail with "Invalid API key" through the warm
  runtime, then answer back on the plan (three small Haiku turns of plan usage).
  `--real` uses `~/.claude/pairs/api_key` instead and spends one Haiku turn of
  API credits.
- `python tests/smoke_live_codex_update.py` — a running server following a
  Codex app update, including the binary vanishing between lookup and spawn
  (a create probe + three one-line Luna turns)

## Releasing

`python scripts/build_and_install_extension.py --bump patch` bumps the
version, refreshes the editable install's pip metadata (`pip show` and
`importlib.metadata` otherwise keep reporting the version from install time),
and packs `dist/claude-squared-<version>.mcpb`. A build without `--bump`
stops if that metadata lags the source, and prints the
`pip install -e . --no-deps` command that fixes it.

## Code organization

See [README.md](./README.md) for the user-facing API and design notes. Source
layout under `src/claude_squared/`:

- `server.py` — FastMCP server, all `@mcp.tool` registrations
- `runtime.py` — `PairRuntime` long-running subprocess + idle eviction
- `adapters/claude.py` — wraps the `claude` CLI (one-shot + stream-json paths)
- `adapters/codex.py` — wraps `codex exec --json` (one process per turn),
  rollout/sqlite readers, the app-server compaction client, rewind re-sync
- `codex_models.py` — Codex model policy from `~/.codex/models_cache.json`
  (defaults, floating aliases, effort levels, windows, deprecations)
- `tool_details.py` — per-T-N sidecar of full Codex item events behind
  `pair_tool_detail`
- `models.py` — Pydantic schemas (`PairSpec`, `SendResult`, etc.), the
  backend-neutral vocabularies + per-model effort coercion
- `registry.py` — JSON registry on disk + filelock concurrency, quarantine of
  unreadable entries
- `settings.py` — user-configurable defaults (`PairDefaults`)
- `cli_paths.py` — single source for the `claude` CLI's path-encoding regex
- `transcript.py` — JSONL → structured turns parser
- `agents.py` — custom agent definition writer (`~/.claude/agents/`)
- `async_tasks.py` — background `pair_send_async` task store + worker
- `_wait_script.py` — embedded source of the standalone `wait.py` waiter
- `__main__.py` — `python -m claude_squared` entry point (default: serve;
  subcommand: `wait`)

## Pull request checklist

Before opening a PR:
- [ ] `python scripts/run_offline_tests.py` passes (add a regression to
      `tests/test_maintenance*.py` or a new `tests/smoke_*.py` for behavior
      you changed)
- [ ] If you added a public-facing tool or arg, README + CHANGELOG updated
- [ ] If you touched a docstring, the first line is ≤ 80 chars (the deferred
      tool stub Claude Code shows)
- [ ] No leaked absolute paths from your machine in committed files
      (`grep -rn "/Users/<your-name>\|C:/Users/<your-name>" src/ tests/` is a
      good check)

## Reporting issues

Bug reports most useful when they include:
- The MCP install path (CLI / Desktop / both)
- The Claude Code CLI version (`claude --version`)
- The exact tool call + the runtime output / error
- Any relevant log snippets from `~/.claude/pairs/logs/<pair>/main.log`

## License

Contributions are accepted under the MIT License (see [LICENSE.txt](./LICENSE.txt)).
