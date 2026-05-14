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
`.mcpb` to `dist/claude-squared-<version>.mcpb`, and unpacks it into the
per-OS Claude Extensions directory. Restart Claude Desktop to pick up the new
bundle.

## Running tests

The smoke tests are pure-stdlib unit tests on the resolution / coercion /
storage logic — no MCP server or `claude` CLI needed:

```bash
PYTHONIOENCODING=utf-8 python tests/smoke_v08.py
PYTHONIOENCODING=utf-8 python tests/smoke_v081.py
```

Each smoke script exits with a clear `PASS:` line on success and propagates
non-zero on any failed assertion. CI runs both on every push.

End-to-end testing requires a live `claude` CLI install. There's no automated
harness for that yet — manual test pass via the actual MCP tools is the
current pattern (see commit messages around v0.8.2 / v0.9.0 for examples).

## Code organization

See [README.md](./README.md) for the user-facing API and design notes. Source
layout under `src/claude_squared/`:

- `server.py` — FastMCP server, all `@mcp.tool` registrations
- `runtime.py` — `PairRuntime` long-running subprocess + idle eviction
- `adapters/claude.py` — wraps the `claude` CLI (one-shot + stream-json paths)
- `models.py` — Pydantic schemas (`PairSpec`, `SendResult`, etc.) +
  per-model effort coercion
- `registry.py` — JSON registry on disk + filelock concurrency
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
- [ ] Smoke tests pass (`smoke_v08.py` + `smoke_v081.py`)
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
