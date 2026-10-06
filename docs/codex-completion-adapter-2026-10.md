# Codex Desktop completion adapter — implementation and work record

Status, 2026-10-06: implemented and tested on `codex/desktop-callback-2026-10-06`, based on v0.16.2 / `8644cd8`. Disabled by default. Not merged, installed, enabled in Alvar's config, or released. This document separates the Codex agent's changes from Claude's earlier work.

## What changed

The adapter wakes the **calling Codex Desktop manager chat** when a pair settles, through the existing Desktop tool-output route. Either pair backend is supported. This is separate from Claude pairs waking themselves after background work.

`completion.py` owns caller capture, pending registrations, the dispatcher and delivery status. `desktop_bridge.py` owns the private, build-gated Windows pipe transport. Integration in `server.py` adds injected Context metadata to shared async entry points. Context is not an argument exposed to the model.

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

State lives in `pairs/completion-v1/`, separate from the registry. Unique temporary files and bounded Windows replacement retries publish atomically. Kernel-backed per-record file locks serialize senders. No pair/runtime/registry/activity lock is held over network I/O. Disabled mode creates no outbox, dispatcher or pipe probes. Unchanged terminal records are skipped during dispatch scans.

## Delivery and fail-closed behavior

Executable path, Desktop ancestry, creation times and pipe owner are checked. Catalog/schema validation must identify exactly one callback endpoint. Frame sizes and I/O deadlines are bounded; rejected candidates close handles. The response must acknowledge the intended destination.

Compatibility is gated by `SUPPORTED_BUILDS` in `desktop_bridge.py`. The measured snapshot is Desktop `26.930.7945.0`, CLI `0.160.1`, FastMCP `3.2.4`, on 2026-10-06. Unknown Desktop builds cannot silently use this private protocol: they need a compatibility test and supported-build update. Today's request includes `callerSource: codex`, which September's prototype omitted; the old request was rejected before delivery.

Current manager policy/cwd and source are checked before transmission, and pair generation/activity are rechecked after discovery. A new turn with unpublished/mismatched context defers instead of inheriting older permissions. Any rollback marker after the source invalidates that registration. User-turn replay is deliberately not approximated by counting task-start events; new work can arm a fresh record.

Every notice has a stable marker. Intent is persisted before callback bytes, acknowledgment afterward. A send timeout/crash is `uncertain`, not proof of failure. Reconciliation checks the intended manager's recorded tool output with backoff; absent a receipt, automatic resend is suppressed. Stable `callId` alone is not treated as receiver deduplication. Exactly-once delivery is not promised.

The adapter provides an event and result references under the manager's existing authorization. It does not guarantee the model will finish that task. Results/diagnostics survive blocked callbacks. Published-context checks cannot provide an atomic precondition that the private endpoint itself lacks; UI-only changes not yet published are not independently observable here.

## Validation and review

Final checks: all **27 maintained offline suites passed**, including **44 completion tests**. `git diff --check` passed. The installed main checkout remained clean at the base commit throughout testing.

The offline runner includes the completion suite. It covers disabled behavior, metadata injection/schema hiding, origin/policy/profile/plan checks, coalescing, fast completion/loss/generation races, pending/background work, context publication lag, cwd changes, clear/forget/archive/rewind, concurrent dispatchers, atomic failures, malformed catalogs/frames, handle cleanup, destination acknowledgments, uncertain sends and reconciliation. Real stdio fixtures validate dispatch after the RPC returns and an owned-server restart with no model calls.

Live Windows tests used an already-idle **Desktop-created manager** and a **read-only Luna worker**, with pair/task/activity state isolated from the real pair registry. The branch MCP captured metadata over stdio and delivered the notice itself. The manager replied `IMPLEMENTATION_WAKE_OK`; its model, cwd, full-access policy and `never` approval were unchanged, and it called no tools. The worker's native shell sleep finished with `PAIR_WORK_DONE`, collected once through `pair_poll`.

A second live pass closed only the owned test MCP after its Codex child started, then started a fresh owned MCP. The worker continued, the new owner recovered the result, and the manager again replied `IMPLEMENTATION_WAKE_OK`. User app/windows and existing MCP/pair processes were not restarted or stopped. Two windows were open; explicit destination delivery passed without navigating either window. All per-window focus/ownership cases are not claimed tested.

An Astra reviewer made read-only passes. Fixes covered discovery races, pre-launch generation/loss capture, plan/cwd checks, uncertain-check backoff, rollback semantics, context publication, stale coalescing, catalog cleanup and launch-history/final-inline duplicates. No Claude subscription usage was required. Logs/evidence stay local beside this checkout (`callback-offline-*`, `callback-focused-*`, `callback-live-*`); the separate October smoke report is in this Codex task's outputs.

No live Claude background/compaction/handoff round ran because Claude usage was unavailable. Existing offline lifecycle suites and mocked entry points cover those paths; add a live Claude pass before a broad release. The September document/prototype remains historical and unchanged.

## Files and release handoff

New: `completion.py`, `desktop_bridge.py`, `test_completion.py`, `completion_mcp_fixture.py`, and `experiments/completion_smoke.py`. Small integrations: `server.py`, `__main__.py`, offline runner and README. No dependencies, warm-runtime rewrite, unrelated shutdown-race fix, registry migration, defaults change or reinstall.

The smoke harness is offline by default. `--live` requires explicit disposable manager/turn IDs. `--restart` restarts only its own isolated MCP; it never installs or restarts the user's server. Results, worker transcripts and manager evidence were preserved locally; test pairs were forgotten and their threads archived.

Before shipping: review/integrate this branch when pairs are idle, choose a version, build/verify through the existing workflow, then enable in a fresh Codex session. Never switch active MCPs to this worktree, patch Desktop, stop other pairs or kill processes by image name.
