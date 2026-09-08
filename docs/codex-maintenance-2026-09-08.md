# Codex maintenance handoff — 8 September 2026

## Ownership and pause point

This document records Codex's work after Claude released v0.13.0. The base is
`4e437c1` on `main`; the separate branch is `codex/maintenance-2026-09-08`.
Claude's original checkout and installed editable package have not been changed.
This is a **work-in-progress checkpoint**, not a release or a final review sign-off.

The user requested minimal edits to existing docs, one new account of this work,
compatible fixes, and substantial investigation of completion callbacks. The user
chose **a tested prototype and documented design first**, rather than integrating
the private Desktop interface into the production MCP. Large architecture changes
still require discussion. Work paused at the user's request on 8 September.

## Implemented in this branch

- `registry.py` and `models.py`: preserve the original registry when schema
  validation rejects entries or a newer/invalid version is encountered. Reads can
  expose valid entries, but writes and migration of a partial view are refused.
  Original bytes are backed up. Unknown stored permissions now fail validation
  instead of silently becoming `auto`. Additive unknown fields, including null
  extras, survive read/modify/write. The filename and schema version remain unchanged.
- `async_tasks.py`, `models.py`, `errors.py`, and `server.py`: add queued/execution
  metadata and per-task cancellation markers usable across processes. Queued
  sends/compactions check cancellation before invoking a backend. Default stop
  selects executing tasks; `drain_queue=True` also cancels queued tasks. Captured
  results survive a completion/stop race. Queued tasks do not borrow another
  task's live log. Waiting on an already completed task returns immediately;
  cross-process completion is observed through bounded disk checks.
- `adapters/codex.py`, `tool_details.py`, and `server.py`: save full started and
  completed Codex item events under `logs/<pair>/codex-tool-items/T-N.json`, with
  a run/task identifier. New Codex `pair_tool_detail` calls use those records.
  Completion-only IDs are scoped to the current exec invocation, so repeated CLI
  item IDs do not reuse an earlier turn's T-N. Older logs without full records
  report that limitation; no pair reset is required for future recording.
- `server.py`: recognize both Claude's millisecond and Codex's token/failure turn
  markers, fixing live log scoping. Verbose rewind output includes the projection
  repair note.
- `adapters/codex.py`: an incompatible required projection table causes all
  SQLite repair deletes to roll back, rather than advancing the cursor after
  partial failure. The optional realtime table may be absent. The rollout itself
  is still truncated first; a repair failure is reported and is not a claim of
  cross-file atomicity.
- `__main__.py`: terminal Claude context reporting uses a captured window from a
  result for the same session. When none exists, the historical fallback is
  explicitly labelled an estimate rather than presented as an observed window.
- `tests/smoke_codex.py`: its offline half uses synthetic model-cache, config,
  registry, and executable fixtures and a guard against subprocess launches.
  The explicit live test body remains separate. `CODEX_HOME` is not redirected.
- `scripts/run_offline_tests.py` and `.github/workflows/ci.yml`: run the 17
  maintained offline smoke scripts in separate isolated processes plus discovery
  of `tests/test_maintenance*.py`, across the existing OS/Python matrix.
- `tests/test_maintenance.py`: 15 new regressions covering registry preservation,
  unknown permissions, additive fields, queued stop/drain, cancellation from a
  second process, completion races, log boundaries, durable tool details,
  projection rollback, and captured context-window reporting.

The new test runner/CI/fixture work was delegated to the existing Astra pair.
The parent reviewed its changes and executed the tests with the pinned Python
interpreter, because Python execution was unavailable in the pair's sandbox.

## Validation at the pause point

- Before edits: all **17 existing offline suites passed**.
- After edits: **18 suites passed, 0 failed**, including **15 new maintenance
  tests**. No live Claude calls were made. The full log is in the originating
  Codex task's `work/maintenance-tests.log`.
- Run again with `python scripts/run_offline_tests.py` using an interpreter with
  the already-installed dependencies. This does not require a package reinstall.
- A final independent review and broader targeted edge-case checks have **not**
  yet been completed. In particular, review cancellation races around task
  promotion/stop, foreign self-woken work, task-state write failures, and the
  best-effort behavior of old/incomplete tool-detail records.

## Completion callback investigation

### Established before this branch

Isolated tests using the real Codex app-server and a scripted loopback model
provider established that:

1. A background PostToolUse hook delivers context to the next model request in
   an active turn. If the turn ends first, it finishes but does not start another
   model request; output is deferred until another turn begins.
2. `turn/start` with `input: []` and `toolOutput` starts a new turn in an idle
   conversation while retaining the payload as a function-call output.
3. MCP calls in codex-cli 0.153.4 carry `_meta.threadId` and turn metadata. The
   MCP initialization capabilities advertised elicitation, not sampling.

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server#start-a-turn)
describes the tool-output callback and says it queues output if a regular turn
is already active. The direct active-turn queuing case remains to be tested.

### Windows Desktop connection found in this pass

The installed Desktop starts its normal app-server through private stdio. The
managed-daemon helpers are Unix-only on the tested build. A warm pair runtime
alone would not provide a connection into the Desktop caller.

However, the bundled `codex-app-tools` server implements a separate Windows
named-pipe bridge. Its environment variable is `CODEX_APP_TOOLS_PIPE_PATH`.
The wire format is a 4-byte little-endian length followed by UTF-8 JSON-RPC.
The bridge accepts `tools/list`, `tools/call`, and `tools/cancel`; it is not a
general app-server endpoint. The packaged `send_message_to_thread` route reaches
the Desktop's existing turn coordinator and uses tool-output delivery.

Read-only inspection identified an app-tools pipe owned by the running Desktop
process. `tools/list` returned the expected catalog, including
`send_message_to_thread`. Other similarly named pipes rejected that method.
**Do not guess a production pipe from its prefix**: use an explicit supplied path
or investigate reliable environment forwarding/discovery. No production discovery
or callback integration has been added.

`experiments/codex_desktop_bridge.py` is an isolated, opt-in prototype:

- `--pipe <identified path>` reads the catalog.
- `--send-smoke --thread-id <disposable thread> --completed-turn-id <old turn>`
  sends one self-addressed test completion, optionally after `--delay` seconds.
- It verifies the pipe owner's executable, bounds framed I/O, and supplies no
  model or permission overrides. It is not imported by the MCP server.
- This depends on a **private, version-specific Desktop protocol** and is not
  offered as a supported production API.

### Actual Desktop smoke result and remaining question

A disposable Luna/read-only pair was created through the native MCP. Its initial
CLI turn ended. The prototype then connected to the actual Desktop app-tools
bridge after a delay and called `send_message_to_thread`, addressing the same
test thread and supplying its **already-completed source turn ID**.

- The bridge accepted the request.
- The actual Desktop started and completed a new turn on that idle thread.
- The model replied `pair-ready`, **not** the requested smoke token.
- Desktop task inspection reported the completed turn but no projected message
  items; the rollout contained the final `pair-ready` response.

Therefore **transport access and actual Desktop wake-up are demonstrated**, but
the end-to-end smoke's expected response was not achieved. Do not report that
test as a full semantic success. Next inspect the delivered tool-output envelope
and use an initial user instruction that explicitly anticipates a later completion
event. The original last user instruction was the create probe's exact
`pair-ready` request, which may explain the repeated response; this is a
hypothesis, not a verified diagnosis. Also verify model/permission preservation,
active-turn behavior, duplicate handling, reconnects, and the UI projection gap.

The disposable pair is removed and its thread archived after this probe. The
retained `takeover-astra-mtsi8nk8` pair is available for continued review.

## Remaining maintenance work

1. Review and refine the implemented fixes, add missing targeted regressions,
   and run the offline runner again after any changes.
2. Fix/verify release packaging. The tracked root `claude-squared.mcpb` still
   contains v0.12.1. The published v0.13.0 asset matches the local dist bundle but
   omits the final `pair_update` alias/display-note fix from the tagged source.
   The build script still relies on a manual root-bundle copy. Add version/source
   verification and automatic refresh; avoid silently changing published assets.
   No build, version bump, reinstall, tag, push, or release has been performed.
3. Continue the callback prototype as described above. Any production private-IPC
   adapter or broader architectural change must be discussed with the user first.
4. Update this one document with final changes, tests, limits, and merge guidance.
   Existing docs should receive minimal pointers only, as requested.

## Stale existing documentation to revisit after merge

- CHANGELOG/HANDOFF still contain claims that Codex compaction is unavailable.
- HANDOFF's command-shape section still says prompts always use DEVNULL, although
  prompts now use a pipe that is closed after writing.
- CONTRIBUTING omits the Codex adapter and maintained offline suites.
- Async guidance assumes Claude Code's Bash completion notification semantics.
- CLI inspection is described as pure read although registry loading can migrate
  validated old data. The historical May design note is explicitly historical and
  should remain so.

No large rewrite, warm Codex runtime, registry filename migration, or production
callback transport is part of this checkpoint.
