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

That runs the 17 maintained `tests/smoke_*.py` scripts (each prints `PASS:`
and exits non-zero on any failed assertion) plus the `unittest` suites in
`tests/test_maintenance*.py`. CI runs the same command on every push across
the OS/Python matrix. Four legacy scripts (`smoke.py`, `smoke_runtime.py`,
`smoke_streamjson.py`, `smoke_v05.py`) predate the v0.10 API and are not part
of the run.

Live tests exist for the Codex backend — `python tests/smoke_codex.py --live`
(a temporary `CLAUDE_HOME`, real `codex` auth, ~15 calls on the cheapest
model) — for Claude lifecycle races, `python tests/smoke_live_0140.py`
(a handful of short Opus turns), for `pair_handoff`,
`python tests/smoke_live_handoff.py` (Haiku + Luna turns; the size gate uses
a synthetic oversized session and spends nothing), and for connectors,
`python tests/smoke_live_connectors.py` (uses the throwaway server in
`tests/fixtures/mcp_probe_server.py`; it temporarily registers that server
with `codex mcp add` and always removes it). They cost real usage; run them
before a release, not in CI.

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
