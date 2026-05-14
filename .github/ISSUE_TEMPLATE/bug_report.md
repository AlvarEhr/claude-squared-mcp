---
name: Bug report
about: Something isn't working as documented or expected
title: "[bug] "
labels: bug
assignees: ''
---

## What happened

<!-- Describe the bug. What you observed vs what you expected. One paragraph is fine. -->

## Reproduction

<!-- Smallest steps that show the bug. The exact tool calls + their responses are usually most useful. -->

```
pair_create(name="...", ...)
pair_send(name="...", message="...")
# error / wrong output
```

## Environment

- **OS**: <!-- Windows / macOS / Linux + version -->
- **Install path**: <!-- Claude Desktop extension / Claude Code CLI MCP / both -->
- **claude CLI version**: <!-- output of `claude --version` -->
- **claude-squared version**: <!-- from `pair_settings_get` or `pip show claude-squared` -->
- **Python version**: <!-- output of `python --version` -->

## Logs

<!-- Relevant snippets from ~/.claude/pairs/logs/<pair-name>/main.log if useful.
     Trim aggressively — context window is finite for whoever triages this. -->

```
(paste log lines here)
```

## Anything else

<!-- Suspected cause, related issues, things you've already tried, etc. -->
