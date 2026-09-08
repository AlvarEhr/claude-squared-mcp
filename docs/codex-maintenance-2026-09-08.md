# Codex maintenance handoff — 8 September 2026

> **Review outcome (v0.14.0):** merged into `main` after review by the main Claude
> session, the historian pair and an independent Astra pass. The review fixes and
> the decisions that differ from this account (registry quarantine instead of
> write refusal, interrupt-once, the compatibility stop marker, identity-checked
> stops) are recorded in `CHANGELOG.md` 0.14.0 and the engineering handoff. This
> document is kept as the Codex agent's own account of the branch.

## Ownership and current review state

This document records Codex's work after Claude released v0.13.0. The base is
`4e437c1` on `main`; the separate branch is `codex/maintenance-2026-09-08`.
Claude's original checkout and installed editable package have not been changed.
The first checkpoint was `8a432e2`. The compatible maintenance fixes and callback
prototype have now been refined and validated for Claude's review. This is not
a published release; nothing has been merged into `main` or installed globally.

The user requested minimal edits to existing docs, one new account of this work,
compatible fixes, and substantial investigation of completion callbacks. The user
chose **a tested prototype and documented design first**, rather than integrating
the private Desktop interface into the production MCP. Large architecture changes
still require discussion. Work paused and resumed at the user's request on
8 September. The sections below include the subsequent review and experiments.

## Implemented in this branch

- `registry.py` and `models.py`: preserve the original registry when schema
  validation rejects entries or a newer/invalid version is encountered. Reads can
  expose valid entries, but writes and migration of a partial view are refused.
  Original bytes are backed up. Unknown stored permissions now fail validation
  instead of silently becoming `auto`. Additive unknown fields, including null
  extras, survive read/modify/write. Mutating backend operations now preflight
  registry writability before spawning work or changing a transcript. The
  filename and schema version remain unchanged.
- `async_tasks.py`, `models.py`, `errors.py`, and `server.py`: add queued/execution
  metadata and per-task cancellation markers usable across processes. Queued
  sends/compactions check cancellation before invoking a backend. Default stop
  selects executing tasks; `drain_queue=True` also cancels queued tasks. Captured
  results survive a completion/stop race. Queued tasks do not borrow another
  task's live log. Waiting on an already completed task returns immediately;
  cross-process completion is observed through bounded disk checks.
- The follow-up cancellation review added a short per-pair control lock around
  promotion and stop selection. Local process/runtime identity is checked before
  interruption, preventing a stopped task's successor from being killed. A
  Claude send remains queued until local implicit work ends. Foreign self-woken
  work explicitly requires its owner to interrupt it; unrelated queued sends
  are preserved. Cancellation-file failures do not erase local stop identities.
  Finalization retains a local result if terminal persistence fails and retries
  on observation. State publication retries transient Windows replacement
  conflicts so file-based watchers see completion without requiring an MCP poll.
- `adapters/codex.py`, `tool_details.py`, and `server.py`: save full started and
  completed Codex item events under `logs/<pair>/codex-tool-items/T-N.json`, with
  a run/task identifier. New Codex `pair_tool_detail` calls use those records.
  Completion-only IDs are scoped to the current exec invocation, so repeated CLI
  item IDs do not reuse an earlier turn's T-N. Older logs without full records
  report that limitation; no pair reset is required for future recording.
  Malformed sidecar records and failed detail writes do not suppress ordinary
  completion log lines.
- `server.py`: recognize both Claude's millisecond and Codex's token/failure turn
  markers, fixing live log scoping. Verbose rewind output includes the projection
  repair note.
- `adapters/codex.py`: an incompatible required projection table causes all
  SQLite repair deletes to roll back, rather than advancing the cursor after
  partial failure. The optional realtime table may be absent. The rollout itself
  is still truncated first; a repair failure is reported and is not a claim of
  cross-file atomicity.
- `__main__.py`: terminal Claude context reporting uses a captured window from a
  result for the same session/model which covers the latest transcript usage.
  `ContextStatus.window_source` distinguishes native and fallback windows. Older
  results without provenance, or samples older than the latest usage, are not
  presented as observed. The historical fallback is explicitly labelled estimated.
- `tests/smoke_codex.py`: its offline half uses synthetic model-cache, config,
  registry, and executable fixtures and a guard against subprocess launches.
  The explicit live test body remains separate. `CODEX_HOME` is not redirected.
- `scripts/run_offline_tests.py` and `.github/workflows/ci.yml`: run the 17
  maintained offline smoke scripts in separate isolated processes plus discovery
  of `tests/test_maintenance*.py`, across the existing OS/Python matrix.
- `tests/test_maintenance.py`: regressions covering registry preservation,
  unknown permissions, additive fields, queued stop/drain, cancellation from a
  second process, completion races, log boundaries, durable tool details,
  projection rollback, and captured context-window reporting.
- `tests/test_maintenance_async_review.py`: 14 targeted regressions for stop/
  promotion ordering, I/O failure handling, implicit-turn waiters, and process
  identity. `tests/test_maintenance_packaging.py`: five small fixture-based
  regressions for stale source, version drift, stable-copy refresh, and required
  vendored runtime files, and Git's Python line-ending conversions.
- `scripts/build_and_install_extension.py`: validates source/manifest version
  agreement before building or bumping, verifies the full source inventory and
  contents inside the zip (allowing Python CRLF/LF conversion), checks runtime/
  staged metadata, and atomically refreshes
  the stable-name bundle. Failed verification leaves the previous versioned
  bundle intact. `--verify BUNDLE` performs a read-only check. Exclusions are
  relative to the staging directory, so an ancestor named `build` cannot exclude
  the whole package.

The new test runner/CI/fixture work was delegated to the existing Astra pair.
The parent reviewed its changes and executed the tests with the pinned Python
interpreter, because Python execution was unavailable in the pair's sandbox.

## Validation

- Before edits: all **17 maintained offline suites passed** (the runner's allow-list;
  `smoke.py`, `smoke_runtime.py`, `smoke_streamjson.py` and `smoke_v05.py` are legacy
  pre-v0.10 scripts that fail for unrelated reasons and are not part of it).
- At the first pause: 18 suites passed, including 15 maintenance tests.
- Final offline run: **18 suites passed, 0 failed**, including **40 maintenance
  tests**. No live Claude calls were made. The full latest log is in the
  originating Codex task's `work/final-maintenance-tests.log`.
- Validation was executed on Windows with Python 3.11. The broader OS/Python
  CI matrix is configured, but was not executed remotely in this pass.
- Run again with `python scripts/run_offline_tests.py` using an interpreter with
  the already-installed dependencies. This does not require a package reinstall.
- A second independent Astra review identified cancellation and I/O edge cases;
  the corresponding fixes and deterministic regressions were then implemented
  and executed by the parent. Claude should still review the combined branch,
  particularly the control-lock ordering and persistent-runtime changes.
- Both development bundle copies were rebuilt and verified byte-for-byte against
  the source. Their SHA-256 is
  `d3c25dcc4a1a8c9d049235535f6358858aa3990acd45fb41b18faafd196f4570`.
  They still declare **0.13.0**, because the release version is left to the
  maintainer. Do not publish this development build over the existing v0.13.0
  asset; choose the next version and rebuild before releasing.
- The rebuilt extension also passed a real stdio startup smoke test in empty
  temporary pair state: initialize, discovery of **30 tools**, and `pair_list`.
  Repeat with `python experiments/packaged_mcp_smoke.py claude-squared.mcpb`.
  Its advertised server version is FastMCP's default library version; bundle
  version/source validation uses the manifest and package source instead.

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

### First actual Desktop smoke: wake-up but wrong reply

A disposable Luna/read-only pair was created through the native MCP. Its initial
CLI turn ended. The prototype then connected to the actual Desktop app-tools
bridge after a delay and called `send_message_to_thread`, addressing the same
test thread and supplying its **already-completed source turn ID**.

- The bridge accepted the request.
- The actual Desktop started and completed a new turn on that idle thread.
- The model replied `pair-ready`, **not** the requested smoke token.
- Desktop task inspection reported the completed turn but no projected message
  items; the rollout contained the final `pair-ready` response.

That first attempt demonstrated transport access and wake-up, but not the desired
reply. Inspection later confirmed that its completion payload was present as a
`function_call_output`, wrapped in `codex_delegation`; model and permissions were
preserved. The last user instruction was still the exact `pair-ready` create
probe. The follow-up test below corrected the completion contract.

The disposable pair is removed and its thread archived after this probe. The
retained `takeover-astra-mtsi8nk8` pair is available for continued review.

### Follow-up Desktop smoke: idle and active delivery passed

A fresh Luna/read-only pair first received a user instruction to reply
`READY_FOR_CALLBACK` now and, on later `codex_app.send_message_to_thread` tool
outputs, consume the anticipated completion token. After that turn completed:

1. A delayed, self-addressed callback using the completed source turn ID woke the
   actual Desktop task. The response was exactly `DESKTOP_CALLBACK_CONTRACT_OK`.
2. Two more callbacks were sent in quick succession, each over a fresh pipe
   connection, carrying `DESKTOP_CALLBACK_ACTIVE_A` and then
   `DESKTOP_CALLBACK_ACTIVE_B`. Both payloads appeared in **one** active Desktop
   turn. Its final response was exactly `DESKTOP_CALLBACK_ACTIVE_B`.
3. Every recorded turn retained `gpt-5.6-luna`, read-only sandboxing, and approval
   policy `never`. No model/permission overrides were supplied by the prototype.
4. All test pairs were forgotten and their test threads archived.

This demonstrates a working completion contract, idle wake-up, reconnection, and
delivery into an active turn in the tested Desktop build. The prototype's JSON
response only acknowledges delivery; it explicitly does not claim to verify the
model reply. Those replies were separately confirmed in the rollout.

The app-tools pipe changed after the Desktop restart, so an explicitly supplied
path requires rediscovery. Automatic environment forwarding/discovery, restart
recovery, duplicate suppression, multi-window behavior, and a stable public
interface remain unproven. The Desktop inspection/projection gap also remains:
task status reported completion while projected message items were empty; the
rollout contained the actual replies. No production adapter has been added.

## What Claude should review and decide next

Suggested relay: "Please review the separate `codex/maintenance-2026-09-08`
branch against `4e437c1`, beginning with this document. Do not assume the
development bundle is a new release. The maintenance changes are implemented and
the private Desktop callback is a prototype only. After review, decide what to
merge and which version to release. Focus especially on cancellation ordering,
live Claude runtime behavior, and the private-bridge limitations below."

1. Review `git diff 4e437c1..codex/maintenance-2026-09-08`, especially registry
   recovery and cancellation/control-lock ordering. Run
   `python scripts/run_offline_tests.py` after any adjustments.
2. Confirm the intended stop behavior for foreign self-woken work (explicit owner
   handoff). The existing blocking Claude one-shot paths still cannot be
   interrupted mid-call. Local unsaved-result recovery cannot survive process
   loss before disk persistence recovers. Updated cancellation participants need
   fresh MCP processes; old processes do not understand the new markers.
3. With Claude usage available, perform an isolated live Claude persistent-runtime
   stop/resume check. This pass used offline Claude regressions and live Codex
   Desktop callbacks, not a live Claude lifecycle check.
4. Before releasing, choose a new patch version and rebuild. The original root
   bundle was v0.12.1, and the published v0.13.0 asset omitted the tagged final
   display fix; the new build checks prevent that drift. The development bundle
   reuses the existing vendored Windows dependencies. Review platform/ABI support
   separately before advertising the same binary bundle on other platforms.
5. Discuss any production callback adapter with the user first. The working
   private-pipe prototype does not yet provide a supported discovery mechanism or
   durable/deduplicated callback delivery. A warm-runtime rewrite is not justified
   merely to obtain this Desktop callback path.

No version bump, reinstall, merge, tag, push, or release has been performed.
Existing docs received only the README pointer to this document.

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
callback transport is part of this branch.
