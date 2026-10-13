# Codex Desktop completion adapter — implementation and work record

Status, 2026-10-13: implemented on `codex/desktop-callback-2026-10-06`, originally based on v0.16.2 / `8644cd8`. The branch keeps callbacks disabled by default and is not merged or released. The separate Codex-only `codex_squared` clone is registered with callbacks enabled; its current pinned revision is recorded in the local `installation.json`. App build allowlisting has been removed at Alvar's request; live protocol and identity checks remain. Active-session detection now normalizes Windows extended path prefixes for comparison. Original main and `pair` remain unchanged. This document separates the Codex agent's changes from Claude's earlier work.

## What changed

The adapter wakes the **calling Codex Desktop manager chat** when a pair settles, through the existing Desktop tool-output route. Either pair backend is supported. This is separate from Claude pairs waking themselves after background work.

`completion.py` owns caller capture, pending registrations, the dispatcher and delivery status. `desktop_bridge.py` owns the private, protocol-validated Windows pipe transport. Integration in `server.py` adds injected Context metadata to shared async entry points. Context is not an argument exposed to the model.

Covered entry points:

- `pair_send_async`.
- `pair_send` when its sync wait returns an async handle.
- An inline `pair_send` placeholder with authoritative unfinished background, wake-up or self-woken activity. Launch history by itself does not qualify.
- `pair_create(initial_message=...)`; verbose output also exposes task/notification information in its existing transparency messages.
- Timed-out `pair_compact` and `pair_handoff` briefing tasks.

Ordinary final inline replies do not arm a second notification. Other hosts retain their watcher guidance. Codex handles report whether a callback was armed and no longer promise Claude's background Bash notification mechanism. `pair_poll` includes delivery status where a record exists. The new `python -m claude_squared notifications` command reads records without starting an MCP, dispatcher, orphan sweep or model turn.

## Accepted boundary

Alvar approved the first release for managers **already using full access and approval policy `never`**. Child pairs can still be read-only. The adapter never changes a manager's or pair's permissions to enable delivery, and sends no model/effort/cwd/permission override.

Read-only/workspace/plan managers, custom permission profiles, remote callers, CLI/pair callers, missing/conflicting metadata and unverifiable contexts are refused. Results remain available through `pair_poll`.

The tested Desktop records full access as `sandbox_policy.type = danger-full-access` and, on new chats, `permission_profile = {type: disabled}` plus the built-in `active_permission_profile = {id: :danger-full-access}`. Only those exact built-in shapes or legacy absent fields are allowed. Unknown/custom profiles do not become full access. Default collaboration mode is required when that field exists.

October's prototype test showed a CLI-created read-only thread resuming with the app's full-access setting. The private route exposes no sandbox override or atomic expected-policy precondition. Restrictive manager modes remain unsupported: checking after generation would be too late.

## Enabling after integration

### Current Codex-only clone

Alvar requested a parallel installation on 2026-10-07. `codex_squared` runs a pinned exported snapshot under `%USERPROFILE%/.codex/mcp/codex-squared/releases/`, using existing Python 3.11/dependencies. The local `installation.json` records its current commit and path; earlier snapshots are retained unchanged. This is not an editable checkout or a replacement package installation. Local adjustments are the display name and self-recursive alias guard; each release's manifest records original/installed SHA-256 values.

The new Codex config entry supplies that snapshot's `src` through `PYTHONPATH` and `CLAUDE_PAIR_CODEX_COMPLETION=desktop`. The old `pair` entry and Claude configuration were not changed. Pair/task/activity state remains shared; no registry was copied or edited by hand. Child Codex execs still ignore user config, and the cloned connector selector refuses `codex_squared` itself.

In a fresh chat, use the `mcp__codex_squared__pair_*` tools, not the original `mcp__pair__*` namespace, for wake-up support. Existing chats retain the servers they already loaded. Codex's local app, CLI and IDE share this user-level Codex config; this registration was not added to Claude.

Verification: the isolated initialization/list smoke found the correct display name and all 31 tools; the clone manifest and original `pair` entry were verified. A fresh native Codex chat then called `codex_squared` create/send_async, ended its turn with `CODEX_SQUARED_WAITING`, woke from one completion notice, polled the finished result once and forgot only its disposable worker. Its final reply was `CLONE_WAKE_OK`; model, workspace and permissions stayed unchanged. The test chat was archived and evidence kept in this task's local outputs. No existing pair, MCP or app process was restarted or stopped.

### Future integration into the original installation

Do not merge or switch the installed editable checkout while Alvar's pairs are active. Integration into main is deployment. No version bump or bundle rebuild was performed on this branch.

After review/integration, when existing pairs are idle, add the key to the existing Codex MCP env table, preserving its other values:

```toml
[mcp_servers.pair.env]
CLAUDE_PAIR_SYNC_CAP_SECONDS = "45"
CLAUDE_PAIR_CODEX_COMPLETION = "desktop"
```

Start a fresh Codex chat/MCP. Its async handle must say `Codex completion notification armed`; no turn-scoped terminal watcher is then needed. If it says `not armed`, inspect the displayed task with `pair_poll`: work continues.

`CLAUDE_PAIR_CODEX_PIPE` can supply an explicitly identified app-tools pipe. Forwarded `CODEX_APP_TOOLS_PIPE_PATH` is also accepted. Neither is required if exactly one catalog-verified pipe belongs to this MCP's Desktop ancestor process. Paths are volatile; do not assume yesterday's pipe survives a restart.

## Identity, completion and lifetime

IDs come from FastMCP request metadata, including serialized/structured `x-codex-turn-metadata`. Aliases must agree. The destination is the verified local Desktop caller, not the pair's Codex session or window focus. User-started and Desktop agent-created chats qualify; exec/pair threads do not. The original source turn must still exist.

Caller identity/cwd, pair session and loss baseline are captured **before** the worker starts. Registration persists after task creation, so a fast completion cannot miss registration. Same-manager registrations coalesce only when workspace/session and original source remain valid. A fresh source after rewind/workspace change cannot join stale provenance.

The existing `activity.view()` merged view tracks background tasks, pending wakes, self-woken turns and quiet/publication grace. An interim result is not completion. Notices reference exact initiating/follow-up task IDs, avoiding large embedded replies or re-sending work. Lost background, unresolved visibility and six-hour observation timeouts are attention events, not claims of successful completion. Such attention notices end that notification window; the manager must inspect/resolve the underlying work. Pair clear/forget or session changes cancel stale registrations.

The dispatcher runs inside the MCP, outside the manager's model/terminal turn. No new Windows service or process launcher is added. A stopped MCP leaves a durable outbox; another enabled MCP startup can recover it and collect Codex detached-turn receipts. With all enabled MCPs stopped, delivery waits until one starts. The adapter does not keep Desktop alive or deliver while it is closed.

State lives in `pairs/completion-v1/`, separate from the registry. New tickets use record schema **4**, so older v1/v2/v3 dispatchers cannot reject them using obsolete build/path checks. The current dispatcher with verified Desktop ancestry can claim legacy v1/v2/v3 pending tickets under their kernel lock, preserve intent/ID, upgrade schema and clear obsolete bridge retry backoff; uncertain sends are still never blindly retransmitted. Unknown schemas remain untouched. Unique temporary files and bounded Windows replacement retries publish atomically. No pair/runtime/registry/activity lock is held over network I/O. Disabled mode creates no outbox, dispatcher or pipe probes. Unchanged terminal records are skipped during dispatch scans.

## Delivery and fail-closed behavior

Executable path, Desktop ancestry, creation times and pipe owner are checked. Catalog/schema validation must identify exactly one callback endpoint. Frame sizes and I/O deadlines are bounded; rejected candidates close handles. The response must acknowledge the intended destination.

Codex's local index can store an active rollout as `\\\\?\\C:\\...\\sessions\\...` while Python resolves the sessions directory without the extended prefix. The original lexical containment check incorrectly refused it as "manager is not an active local session." Resolved paths now compare equally with/without that Windows prefix (drive and UNC forms), while I/O retains the original path for long-path support. Archived directories, sibling directories, other drives/shares and junction escapes still fail containment. This mismatch was reproduced on actual active manager index rows; 55 completion tests pass, including real Windows extended-path arming/delivery in an isolated fixture.

Compatibility is checked against the live callback catalog/schema, not a fixed build list. Alvar requested removal of the allowlist on October 7 after `26.1002.7124.0` was refused solely because it was new. Regression tests accept that build and a synthetic future build while still rejecting invalid executable identity or creation times. Read-only endpoint discovery passed on actual Desktop `26.1002.7124.0`; that check sends no callback. Earlier full idle/active tests on Desktop `26.930.7945.0` / CLI `0.160.1` and Desktop `26.1002.6548.0` / CLI `0.162.0-alpha.2` preserved manager policy/model/cwd. Newer builds are attempted automatically; incompatible catalogs/protocols still fail closed. The request includes `callerSource: codex`, absent from September's prototype.

Current manager policy/cwd and source are checked before transmission, and pair generation/activity are rechecked after discovery. A new turn with unpublished/mismatched context defers instead of inheriting older permissions. Any rollback marker after the source invalidates that registration. User-turn replay is deliberately not approximated by counting task-start events; new work can arm a fresh record.

Every notice has a stable marker. Intent is persisted before callback bytes, acknowledgment afterward. A send timeout/crash is `uncertain`, not proof of failure. Reconciliation checks the intended manager's recorded tool output with backoff; absent a receipt, automatic resend is suppressed. Stable `callId` alone is not treated as receiver deduplication. Exactly-once delivery is not promised.

The adapter provides an event and result references under the manager's existing authorization. It does not guarantee the model will finish that task. Results/diagnostics survive blocked callbacks. Published-context checks cannot provide an atomic precondition that the private endpoint itself lacks; UI-only changes not yet published are not independently observable here.

## Validation and review

Final checks: all **27 maintained offline suites passed** after build-gate removal. The completion suite was rerun after the schema-3 coexistence fix: **51 completion/regression tests passed**, including legacy-registration coalescing and uncertain-send preservation. `git diff --check` passed; original main remained clean.

The offline runner includes the completion suite. It covers disabled behavior, metadata injection/schema hiding, origin/policy/profile/plan checks, coalescing, fast completion/loss/generation races, pending/background work, context publication lag, cwd changes, clear/forget/archive/rewind, concurrent dispatchers, atomic failures, malformed catalogs/frames, handle cleanup, destination acknowledgments, uncertain sends and reconciliation. Real stdio fixtures validate dispatch after the RPC returns and an owned-server restart with no model calls.

Live Windows tests used an already-idle **Desktop-created manager** and a **read-only Luna worker**, with pair/task/activity state isolated from the real pair registry. The branch MCP captured metadata over stdio and delivered the notice itself. The manager replied `IMPLEMENTATION_WAKE_OK`; its model, cwd, full-access policy and `never` approval were unchanged, and it called no tools. The worker's native shell sleep finished with `PAIR_WORK_DONE`, collected once through `pair_poll`.

A second live pass closed only the owned test MCP after its Codex child started, then started a fresh owned MCP. The worker continued, the new owner recovered the result, and the manager again replied `IMPLEMENTATION_WAKE_OK`. User app/windows and existing MCP/pair processes were not restarted or stopped. Two windows were open; explicit destination delivery passed without navigating either window. All per-window focus/ownership cases are not claimed tested.

An Astra reviewer made read-only passes. Fixes covered discovery races, pre-launch generation/loss capture, plan/cwd checks, uncertain-check backoff, rollback semantics, context publication, stale coalescing, catalog cleanup and launch-history/final-inline duplicates. No Claude subscription usage was required. Logs/evidence stay local beside this checkout (`callback-offline-*`, `callback-focused-*`, `callback-live-*`); the separate October smoke report is in this Codex task's outputs.

Claude became available during the October 7 pass. An Opus pair launched one background Opus-class Agent, returned an interim, self-woke without another send, and produced the exact final result. Its compaction retained that result. A fresh Codex manager exercised the whole chain and two callbacks, ending `SELFWAKE_COMPACTION_CALLBACK_PASS`. Live Claude-to-Codex handoff was not exercised. September's document/prototype remains historical.

### October 7 broader native checks and fixes

The restarted chat exposed all 31 clone tools. Native checks passed conversation memory, ledger calculation, fixture read/write, context/log/tool-detail inspection, both backends' compaction retention, independent fork memory, clear/reset (`NO_PRIOR_CONTEXT`), and a confirmed mid-command Codex stop/resume preserving marker/total. All operations targeted uniquely named disposable pairs. All five pairs were forgotten; test chats were archived.

Three real failures were found:

- The restart installed a new Desktop build and the original allowlist refused callbacks. Idle/active checks passed before commit `c651d08` added that build. A subsequent update to `26.1002.7124.0` led Alvar to request removing the allowlist entirely; current code validates the live protocol instead.
- Old loaded dispatchers could claim a newer dispatcher’s ticket and impose shared unsupported-build backoff. Schema-2 tickets initially isolated the v1 dispatcher. Build-gate removal now uses schema 3 to fence both v1 and v2 build-gated processes; safe legacy claims preserve uncertain-send handling without restarting old servers.
- Codex compaction incorrectly used lifetime usage as post-context size (showing 16,772 -> 66,533). It now uses the latest context total, including the new CLI's compaction shape with zero input/output and nonzero last.total_tokens. The live corrected result was **16,395 -> 4,608**, with retained `DELTA-662` / 76 and `V2_COMPACT_CALLBACK_PASS`.

The default elevated Windows sandbox also failed to start a command (`helper_unknown_error: setup refresh had errors`). This upstream/environment failure is **not fixed globally**. A documented per-test-pair `windows.sandbox=unelevated` fallback retained read-only/workspace permissions and allowed the I/O checks. No global sandbox setting, full-access pair mode or other pair was changed. Evidence/logs are in this Codex task's `outputs/codex-squared-daylife-2026-10-07/` and `work/daylife-*` artifacts.

Existing MCP processes retain their loaded snapshot. Current-chat direct lifecycle tests used the older loaded clone; updated callback/metric checks used fresh native managers. Start a fresh chat to load the current installed revision; no forced process/app restart was performed. Build-gate removal and read-only discovery evidence are in this task's `work/callback-no-build-gate-2026-10-07.log` and installation receipt.

## Files and release handoff

New: `completion.py`, `desktop_bridge.py`, `test_completion.py`, `completion_mcp_fixture.py`, and `experiments/completion_smoke.py`. Small integrations: `server.py`, `__main__.py`, offline runner and README. No dependencies, warm-runtime rewrite, unrelated shutdown-race fix, registry migration, defaults change or reinstall.

The smoke harness is offline by default. `--live` requires explicit disposable manager/turn IDs. `--restart` restarts only its own isolated MCP; it never installs or restarts the user's server. Results, worker transcripts and manager evidence were preserved locally; test pairs were forgotten and their threads archived.

Before shipping: review/integrate this branch when pairs are idle, choose a version, build/verify through the existing workflow, then enable in a fresh Codex session. Never switch active MCPs to this worktree, patch Desktop, stop other pairs or kill processes by image name.
