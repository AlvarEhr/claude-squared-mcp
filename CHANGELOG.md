# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.9] — 2026-05-31

Ergonomics: the v0.7-era unique-prefix resolution in `pair_poll` was
asymmetric — `wait.py` and `python -m claude_squared wait` required the full
UUID. Copying the 8-char prefix from a `pair_status` listing into the
background watcher silently hit `exit 2` (not-found). Now aligned.

Also fixes a v0.9.8 miss: `_cmd_wait` (the `python -m claude_squared wait`
in-package equivalent of `wait.py`) was never brought up to v0.9.8 exit-code
parity. It still mapped every terminal failure to exit 1, missing the
4=orphan / 5=stopped / 6=crashed dispatch. Anyone hitting the fallback path
(when wait.py install fails at server startup) would silently get the
pre-v0.9.8 codes.

### Changed
- **`wait.py <arg>`** and **`python -m claude_squared wait <arg>`** now share
  `pair_poll`'s exact resolution ladder: exact task id → pair name → unique
  task-id prefix. Ambiguous prefix exits 2 with `"ambiguous: prefix 'X'
  matches N tasks: ..."` (showing up to 5 matches with `+M more` if longer).
  Don't retry an ambiguous prefix in the FS-race loop — adding files in 1s
  won't change multi-match to single-match. Empty prefix returns `[]` so we
  never accidentally match-all.
- The not-found message wording is now uniform across both watchers:
  `"not found: 'X' is not a task id, prefix, or pair name"`.
- Usage strings for both watchers updated to name all three resolution paths
  AND the full exit-code list including 4/5/6 (the v0.9.8 codes that the
  in-package usage message had never been updated for).

### Fixed
- **`_cmd_wait` v0.9.8 exit-code parity** (the miss). Now also returns
  `5` for `status="stopped"`, `4` for `ORPHANED: ` failures, and `6` for
  `CRASHED: ` failures — matching the standalone wait.py behavior shipped
  in v0.9.8. Pre-v0.9.9 fallback-path users would have seen every failure
  bucketed to `1` even when wait.py itself was distinguishing them.

### Smoke
`tests/smoke_v099.py` — 12 functions, 24 assertions:
- wait.py: unique prefix resolves · ambiguous exits 2 with clear message
  · exact id still works · pair name still works · clearer not-found message.
- `python -m claude_squared wait`: unique prefix · stopped→5 · CRASHED→6
  · ORPHANED→4 · generic failed→1 · ambiguous→2.
- Helper: `async_tasks.find_task_by_prefix` semantics.

All prior smoke files green (`smoke_v097` was updated to use a
wording-stable not-found substring since v0.9.9 refactored the message).

## [0.9.8] — 2026-05-31

Five bug-fix clusters from a real-workload report against v0.9.7. The two
biggest were diagnosed against on-disk evidence (4 failed `async/*.json` tasks
all between 10:21 and 10:54 minutes; 10 `compact_boundary` events in webdash's
JSONL across pair_compact `-32001` reports).

### Fixed

- **Idle evictor was killing legitimate long turns** (the source of every
  `failed: CLIError: pair runtime exited mid-turn (code ?)` report observed).
  `last_activity` was only bumped at send entry and result, so a turn lasting
  >10 min looked idle to the evictor and got tree-killed at the next 60s
  cycle. Two protections, belt-and-suspenders:
    1. `_evict_idle` now skips runtimes whose `_current_scope` is set
       (mid-turn). The scope is opened at send-stdin-write and cleared via
       a new outer `try/finally` that fires on every exit path — normal
       return, CLIError, CommandTimeout, any unhandled exception. Without
       the finally, a leaked scope would permanently protect a zombie
       runtime; with it, the signal is reliable.
    2. `_append_main_log_line` now bumps `last_activity` (in addition to
       `_last_log_activity_at`), so a turn producing log lines won't look
       idle even if the scope check were ever bypassed. Side benefit:
       `pair_runtimes` reports an accurate "last activity" mid-turn instead
       of a stale send-entry timestamp.
  **Behavior change**: a genuinely wedged turn (claude.exe stuck with no
  output) is no longer auto-rescued at 10 min. Use `hard_timeout_seconds`
  (None by default) or `pair_stop` for explicit recovery; `pair_status`
  still reports likely-hung at 120s+.

- **`pair_compact` hit host RPC -32001 timeout** even though the compact
  often DID land server-side (verified: 10 `compact_boundary` events in
  webdash's JSONL across the incidents). pair_compact now uses the same
  async-task machinery as `pair_send` (v0.7.1 pattern): graceful sync-cap
  degradation past the RPC-hold cap, work continues in the background,
  caller polls via `pair_poll(name)` or the Bash watcher. New parameter
  layout:
    - `timeout_seconds` (default 45s) is now your **stated patience** —
      the sync wait. Bounded by `CLAUDE_PAIR_SYNC_CAP_SECONDS`.
    - `compact_timeout_seconds` (default 600s) is the **hard ceiling** on
      the compact subprocess — the pre-v0.9.8 `timeout_seconds` semantic.
  `_build_compact_runner` parallels `_build_send_runner`. `AsyncTaskState.result`
  is widened to `SendResult | CompactResult | None` (Pydantic smart-union
  disambiguates via unique fields; pre-v0.9.8 task files deserialize
  unchanged). `pair_poll` dispatches the rendering via `isinstance` to a
  new `_fmt_compact_result` for CompactResult tasks.

- **`pair_status` falsely reported "runtime live in another MCP process"**
  for up to 120s after a local crash, because main.log mtime was still
  fresh from the last pre-crash log line and the in-process lock was no
  longer held. Three-part fix:
    1. **Local-corpse detection**: if our PairRuntime is still in the
       registry but its `.proc.returncode` is set, report it directly as
       "local corpse (claude.exe exited code X); next pair_send will
       respawn from JSONL." No more falling through to cross-process
       inference for a corpse we can identify directly.
    2. **Cross-process activity requires an in-flight task**: pre-v0.9.8
       the branch fired on any recent mtime — now also requires
       `list_running_task_ids_for_pair` to return non-empty.
    3. **Stale-mtime branch**: recent log activity + no inflight work +
       no live runtime is surfaced as "no runtime (cold; last log
       activity Xs ago, no in-flight work — likely a recent crash or
       eviction)" instead of the misleading "live elsewhere" message.

- **`CLIError "(code ?)"`** when the runtime exited mid-turn — root cause
  was a race between `is_alive()` returning False (line 881) and reading
  `self.proc.returncode` (line 887): a concurrent `stop()` could null
  `self.proc` between those two points (pre-v0.9.8 evictor path). Snapshot
  `self.proc` + `self._collect_stderr()` once, up front, before
  constructing the error. The error message now reads
  `(exit <code>)` when an exit code is available, or
  `(exit unknown — runtime cleaned up concurrently)` when it isn't.

- **`wait.py` conflated terminal states** behind a single exit code 1.
  Now distinct codes per state:
    - `0` done · `1` failed (work error) · `2` not-found · `3` timeout
    - `4` orphaned (MCP server died) · **`5` stopped (deliberate cancel
      via `pair_stop`)** · **`6` crashed (claude.exe died mid-turn)**
      · `64` usage
  The `stopped` branch is the genuine bug fix: pre-v0.9.8 `status="stopped"`
  fell through into the polling loop and silently timed out at 1800s
  (3). The `crashed` branch routes via a new `CRASHED: ` prefix on the
  runtime-exit `CLIError` message, parallel to the existing `ORPHANED: `
  prefix. `_format_task_error` preserves both prefixes bare so the
  on-disk error string starts at position 0 for wait.py's `startswith`
  dispatch.

### Added

- `_fmt_compact_result(r: CompactResult)` — formatter parallel to
  `_fmt_send_result`. Shows the pre→post token delta, retention %,
  duration, and trigger source. Used by `pair_poll` when the task's
  result is a CompactResult.
- `_build_compact_runner(name, steering_prompt, *, compact_timeout_seconds, ...)`
  — runner closure parallel to `_build_send_runner`. Holds the
  cross-process pair lock for the whole compact, evicts the warm runtime
  BEFORE invoking compact (compaction rewrites the session JSONL).
- `models.CompactResult` is now a valid type for `AsyncTaskState.result`.
- `CRASHED_ERROR_PREFIX` constant in `runtime.py` and `async_tasks.py`
  (mirrored bare in `_wait_script.py`). Used to distinguish "claude.exe
  died mid-turn" from generic work errors at the wait.py exit-code layer.
- `async_tasks._format_task_error(e)` — preserves supervision-class
  prefixes (ORPHANED/CRASHED) at position 0 of the stored error string;
  wraps other errors with the conventional `<TypeName>: <message>`.
- `pair_poll` now renders `CRASHED` failures distinctly from generic
  failures: "⚠ CRASHED (pair 'X') — the pair's claude.exe subprocess
  exited mid-turn. Partial state may have persisted in the JSONL; the
  runtime will respawn from the persisted session on the next pair_send."
- `pair_status` local-corpse branch: "local corpse (claude.exe exited
  code X ~Ys ago); next pair_send will respawn from JSONL."

### Notes

- **/context display semantics** (Bug 7 from the v0.9.7 batch report):
  the apparent "headline % doesn't match category sum" in
  `pair_context` output is an artifact of /context's own display —
  headline is cache-aware (what's actually re-sent each turn), categories
  show static loadout sizes (cache-blind). We pass `raw_markdown`
  through verbatim from /context; not actionable on our side. Documented
  in HANDOFF "Known caveats."
- **Pair PowerShell sandbox `$env:PATHEXT='.CPL'`** (Bug 6 from the
  same batch): we don't customize the pair subprocess environment
  (`runtime.start` passes no `env=` to Popen — inheritance only). The
  PATHEXT thing is claude.exe's own PowerShell sandbox initialization,
  not us. Documented in HANDOFF "Known caveats."

### Smoke

`tests/smoke_v098.py` — 15 test functions, 31 assertions:
- A: evictor mid-turn skip / non-mid-turn eviction / last_activity bump
- B: AsyncTaskState ↔ CompactResult round-trip · backward compat for
  SendResult · `_fmt_compact_result` · pair_poll polymorphic render
- C: pair_status local-corpse detection
- D: CRASHED_ERROR_PREFIX consistency · `_format_task_error` preserves
  prefixes · CLIError stringifies correctly with CRASHED prefix
- E: wait.py exit 5 (stopped) · exit 6 (CRASHED) · exit 1 (regression)
  · exit 4 (regression)
All v0.7-v0.9.7 smoke tests still green.

## [0.9.7] — 2026-05-30

Completes v0.9.6: the **background watcher now accepts a pair name too**, so the
entire async-handle hint is name-based and consistent (v0.9.6 only did
`pair_poll`).

### Added
- **`wait.py <pair_name>`** resolves to that pair's latest task (stdlib scan
  mirroring `latest_task_id_for_pair`), with a one-tick retry for the
  filesystem race right after task creation. Exact task ids still work
  unchanged. The same support was added to the `python -m claude_squared wait`
  fallback path (`__main__._cmd_wait`).

### Changed
- **The async-handle Bash-watcher command now uses the pair name** (e.g.
  `wait.py reviewer`) instead of the raw UUID — matching the `pair_poll` hints,
  so nothing in the hint requires copying a UUID. The exact task id is still
  printed on the `Async task:` line for targeting an older task explicitly.
- `wait.py` / `wait` not-found message and usage updated to mention pair names.

### Note
- Resolving a name targets the LATEST task for that pair (same semantics as
  `pair_poll`). For the auto-generated watcher this is always the just-started
  task. Pass the explicit task id to target an older one.

## [0.9.6] — 2026-05-30

Ergonomics: agents kept mistyping the UUID task_id when polling. Now you can
poll by the pair's NAME — which you always know — and the async handle steers
you there.

### Added
- **`pair_poll` accepts a pair name.** Resolution order: exact task_id → pair
  name (resolves to that pair's most-recently-started task) → unique task-id
  prefix. So `pair_poll("reviewer")` just works. The output names the concrete
  task it resolved to, so there's never a silent mismatch. Polling by name
  always targets the LATEST task for that pair — pass an explicit id for an
  older one. New helper `async_tasks.latest_task_id_for_pair`.
- The unknown-ref error now names both options ("No async task or pair named
  '…' — pass a pair name, a full task_id, or a unique id prefix").

### Changed
- **Async-handle poll hints now show the pair name** instead of the raw UUID —
  e.g. `pair_poll('reviewer')` — since that's the hard-to-mistype path. The
  Bash watcher line still uses the exact task id (it's pasted programmatically,
  not retyped). Backward-compatible: the helper falls back to the task id when
  no pair name is available.

### Backward compatibility
- Purely additive. Existing UUID and prefix polling are unchanged; the canonical
  task id is still a UUID (a pair has many tasks over its life, so the id can't
  *be* the name).

## [0.9.5] — 2026-05-30

Bug fix from live testing: a pair's async task was reported `failed` ("MCP server
died before completion") even though the work fully succeeded — the git commit
landed, files were written — and the watcher gave no notification for 47 minutes.

Root cause: the owning MCP server process was killed mid-turn (host watchdog on a
long, heavy, post-compaction turn). But the pair's `claude` subprocess runs in its
OWN process group, so it was orphaned and ran to completion — committing the work —
while the MCP layer lost the turn-completion event. The task then sat `running`
until a later server's startup sweep flatly marked it `failed`. So "failed"
conflated a supervision event with a work error, and detection was slow.

### Fixed
- **Orphaned tasks are now surfaced as a distinct supervision event, not a flat
  failure.** The orphan error carries an `ORPHANED: ` marker, and `pair_poll`
  renders "⚠ ORPHANED — NOT a work failure; the work may have completed" with
  recovery guidance (verify via `pair_transcript` + git/file state, then
  `pair_send` to resume), instead of "failed: ...".
- **Prompt orphan detection — no more waiting for a future server's sweep.** New
  `async_tasks.reap_orphan(task_id)` finalizes a `running` task whose `owner_pid`
  is confirmed dead, *on observation*; `pair_poll` calls it so a manual poll
  reflects reality immediately.
- **`wait.py` detects a dead owner directly** (stdlib PID-liveness check) and
  exits within one poll cycle with a new **exit code 4 (orphaned)**, instead of
  sitting at "running" until the 1800s timeout — the direct cause of the 47-minute
  silent wait. It also maps an already-swept `ORPHANED:` task to exit 4.

### Notes
- The MCP server's death itself is host-driven (watchdog/OOM/crash) and not
  preventable from our side — v0.9.5 is about detecting it promptly and reporting
  it honestly. Recovery is always `pair_send` to resume (the session JSONL
  persists). See HANDOFF "Orphaned-but-completed work".
- `wait.py` exit codes are now: 0=done, 1=failed (work error), 2=not-found,
  3=timeout, 4=orphaned, 64=usage.

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
