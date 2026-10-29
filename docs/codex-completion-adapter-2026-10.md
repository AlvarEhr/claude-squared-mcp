# Codex Desktop completion adapter — implementation and work record

Status, 2026-10-29: integrated into the v0.22 worktree release line from callback
branch `a50b68e` (v0.20 base), preserving steering, task retention and Claude's
auto-watch hook. The unified `pair` registration defaults to per-caller detection:
only verified local Codex Desktop managers with full access and approval policy
`never` arm callbacks. `CLAUDE_PAIR_CODEX_COMPLETION=off` disables callbacks;
`=desktop` retains explicit opt-in and refusal diagnostics. Other auto-detected
callers retain the existing hook/wait.py guidance. No installed registration or
live process was changed by integration. Earlier clone deployment evidence is
recorded below; its pinned snapshot remains in the local `installation.json`
until the operator retires it. Live protocol/identity checks, normalized Windows
paths and bounded inspection of large rollouts are retained.

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

A successful `pair_send_async(steer=True)` also arms no new notification: it
retains the running task and its existing completion tracking. A steer that
falls back to a queued send prepares and arms that new task normally. The
successful-steer reply does not promise a new callback to the steering caller.

## Accepted boundary

Alvar approved the first release for managers **already using full access and approval policy `never`**. Child pairs can still be read-only. The adapter never changes a manager's or pair's permissions to enable delivery, and sends no model/effort/cwd/permission override.

Read-only/workspace/plan managers, custom permission profiles, remote callers, CLI/pair callers, missing/conflicting metadata and unverifiable contexts are refused. Results remain available through `pair_poll`.

The tested Desktop records full access as `sandbox_policy.type = danger-full-access` and, on new chats, `permission_profile = {type: disabled}` plus the built-in `active_permission_profile = {id: :danger-full-access}`. Only those exact built-in shapes or legacy absent fields are allowed. Unknown/custom profiles do not become full access. Default collaboration mode is required when that field exists.

October's prototype test showed a CLI-created read-only thread resuming with the app's full-access setting. The private route exposes no sandbox override or atomic expected-policy precondition. Restrictive manager modes remain unsupported: checking after generation would be too late.

## Unified registration and detection

With `CLAUDE_PAIR_CODEX_COMPLETION` unset (or `auto`), `completion.prepare`
checks request metadata, Desktop ancestry, active manager identity and full-access
policy before registration. A refused/unverified caller creates no callback
ticket and retains ordinary hook/wait.py hints. The auto-mode dispatcher starts
after a verified arm, or recovers existing tickets at startup only under verified
Desktop ancestry; Claude/CLI servers do not dispatch the shared outbox.

`off` disables the adapter without pipe probes, outbox creation or a dispatcher.
`desktop` explicitly enables it while retaining every identity/policy gate and
displaying refusal diagnostics. Empty/unknown values remain disabled.

### Retiring codex_squared

After deploying the unified release when safe, keep the existing `pair` Codex
entry and its other settings. Put `CLAUDE_PAIR_CODEX_COMPLETION=desktop` in that
entry's env table if explicit opt-in is wanted (`off` to disable; unset uses
auto-detection). Remove the separate `codex_squared` entry and start fresh Codex
chats. Existing chats retain already-loaded servers. This migration does not
copy pair state or change permissions; no config file is edited automatically.

### Earlier Codex-only clone

Alvar requested a parallel installation on 2026-10-07. `codex_squared` runs a pinned exported snapshot under `%USERPROFILE%/.codex/mcp/codex-squared/releases/`, using existing Python 3.11/dependencies. The local `installation.json` records its current commit and path; earlier snapshots are retained unchanged. This is not an editable checkout or a replacement package installation. Local adjustments are the display name and self-recursive alias guard; each release's manifest records original/installed SHA-256 values.

The new Codex config entry supplies that snapshot's `src` through `PYTHONPATH` and `CLAUDE_PAIR_CODEX_COMPLETION=desktop`. The old `pair` entry and Claude configuration were not changed. Pair/task/activity state remains shared; no registry was copied or edited by hand. Child Codex execs still ignore user config, and the cloned connector selector refuses `codex_squared` itself.

In a fresh chat, use the `mcp__codex_squared__pair_*` tools, not the original `mcp__pair__*` namespace, for wake-up support. Existing chats retain the servers they already loaded. Codex's local app, CLI and IDE share this user-level Codex config; this registration was not added to Claude.

Verification: the isolated initialization/list smoke found the correct display name and all 31 tools; the clone manifest and original `pair` entry were verified. A fresh native Codex chat then called `codex_squared` create/send_async, ended its turn with `CODEX_SQUARED_WAITING`, woke from one completion notice, polled the finished result once and forgot only its disposable worker. Its final reply was `CLONE_WAKE_OK`; model, workspace and permissions stayed unchanged. The test chat was archived and evidence kept in this task's local outputs. No existing pair, MCP or app process was restarted or stopped.

### Loading the unified installation

Do not merge or switch the installed editable checkout while Alvar's pairs are active. Integration into main is deployment. No version bump or bundle rebuild was performed on this branch.

After review/integration, when existing pairs are idle, use the existing Codex
MCP entry. The env key below is optional; preserve any other values:

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

State lives in `pairs/completion-v1/`, separate from the registry. New tickets use record schema **6**, so older v1/v2/v3/v4/v5 dispatchers cannot reject them using obsolete build/path/size checks. The current dispatcher with verified Desktop ancestry can claim legacy v1/v2/v3/v4/v5 pending tickets under their kernel lock, preserve intent/ID, upgrade schema and clear obsolete bridge retry backoff; uncertain sends are still never blindly retransmitted. Unknown schemas remain untouched. Unique temporary files and bounded Windows replacement retries publish atomically. No pair/runtime/registry/activity lock is held over network I/O. Disabled mode creates no outbox, dispatcher or pipe probes. Unchanged terminal records are skipped during dispatch scans.

## Delivery and fail-closed behavior

Executable path, Desktop ancestry, creation times and pipe owner are checked. Catalog/schema validation must identify exactly one callback endpoint. Frame sizes and I/O deadlines are bounded; rejected candidates close handles. The response must acknowledge the intended destination.

Codex's local index can store an active rollout as `\\\\?\\C:\\...\\sessions\\...` while Python resolves the sessions directory without the extended prefix. The original lexical containment check incorrectly refused it as "manager is not an active local session." Resolved paths now compare equally with/without that Windows prefix (drive and UNC forms), while I/O retains the original path for long-path support. Archived directories, sibling directories, other drives/shares and junction escapes still fail containment. This mismatch was reproduced on actual active manager index rows; 55 completion tests pass, including real Windows extended-path arming/delivery in an isolated fixture.

Compatibility is checked against the live callback catalog/schema, not a fixed build list. Alvar requested removal of the allowlist on October 7 after `26.1002.7124.0` was refused solely because it was new. Regression tests accept that build and a synthetic future build while still rejecting invalid executable identity or creation times. Read-only endpoint discovery passed on actual Desktop `26.1002.7124.0`; that check sends no callback. Earlier full idle/active tests on Desktop `26.930.7945.0` / CLI `0.160.1` and Desktop `26.1002.6548.0` / CLI `0.162.0-alpha.2` preserved manager policy/model/cwd. Newer builds are attempted automatically; incompatible catalogs/protocols still fail closed. The request includes `callerSource: codex`, absent from September's prototype.

Current manager policy/cwd and source are checked before transmission, and pair generation/activity are rechecked after discovery. A new turn with unpublished/mismatched context defers instead of inheriting older permissions. Any rollback marker after the source invalidates that registration. User-turn replay is deliberately not approximated by counting task-start events; new work can arm a fresh record.

The manager reader has no total-rollout size ceiling. It streams a fixed EOF snapshot, inspecting identity, turn/context and rollback records and skipping irrelevant canonical messages/tool outputs in bounded chunks. Receipt checks search their marker while draining tool output; unrelated giant outputs never reach JSON decoding. Required control/receipt records still have an 8 MiB individual parsing bound, and unknown oversized record shapes fail closed. Incomplete final records are ignored; a missing latest context still defers. No persistent parser cache or source/policy relaxation was introduced.

Every notice has a stable marker. Intent is persisted before callback bytes, acknowledgment afterward. A send timeout/crash is `uncertain`, not proof of failure. Reconciliation checks the intended manager's recorded tool output with backoff; absent a receipt, automatic resend is suppressed. Stable `callId` alone is not treated as receiver deduplication. Exactly-once delivery is not promised.

The adapter provides an event and result references under the manager's existing authorization. It does not guarantee the model will finish that task. Results/diagnostics survive blocked callbacks. Published-context checks cannot provide an atomic precondition that the private endpoint itself lacks; UI-only changes not yet published are not independently observable here.

## Validation and review

Unified integration checks, 2026-10-29: `python scripts/run_offline_tests.py`
passed **38/38 suites**, with `CLAUDE_SQUARED_TEST_CLI` pointing at the Desktop
app's bundled Claude 2.1.309. This includes **70 completion tests** and **25 pane
tests**. New coverage verifies unset-env non-Codex calls retain identical
hook/wait.py hints, verified Desktop calls arm, rejected CLI/restricted callers
do not arm, startup recovery stays under Desktop ancestry, successful steers do
not re-arm, and queued steer fallbacks do arm. The existing isolated stdio and
simulated transport fixtures passed. No live model runs or Desktop callbacks
were performed during integration; configs, pane sources and main were untouched.

Earlier callback-branch checks passed **33 maintained offline suites** and
**64 completion/regression tests** after the v0.20 refresh. The live manager's
extended-path rollout passed caller identity and full-access eligibility.
After Alvar restarted on October 13, the actual clone armed a read-only Luna task, the manager ended its turn, and the completion callback woke it automatically. The command slept 25 seconds and returned `RESTART_WAKE_OK_B72` with exit 0; the notification was acknowledged as delivered, its result was collected once, and the disposable pair was forgotten/thread archived. No terminal watcher or polling loop was used. `git diff --check` passed; original main remained clean.

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

### October 13 base refresh and sandbox recheck

The original MCP advanced to v0.17.0 and v0.18.0 while the clone remained on v0.16.2. This branch merged the released v0.18.0 commit, preserving callback entry points and compaction metrics. Only the offline-runner additions conflicted: the resolution retains completion, pair-options and billing suites. The clone now exposes 32 tools, including the new billing tool. Installing it does not switch any pair to API billing; that still requires Alvar's explicit request. Upstream changes include Claude tool/options controls, context alerts, per-turn cost accounting and shutdown fix A. They did not modify the Codex adapter or repair its sandbox.

The two reported builder failures were traced to the Windows sandbox setup log at the exact tool-call times: `helper_unknown_error: setup refresh had errors` hid an ACL refresh failing to open the shared `node_repl.exe` for a root-only permissions update, with Windows sharing violation **32**. This was an upstream runtime/setup problem, not a pair-level permission denial. The manager's own full-access shell success does not prove the elevated sandbox is healthy.

On October 13, CLI **0.162.0-alpha.17.2** passed both disposable live checks with the existing elevated implementation: a read-only shell printed `ELEVATED_SHELL_OK_Q6P2`; a separate workspace pair read a fixture containing 17, wrote the result of multiplying it by three, and returned 51. Both command exit codes were 0. No fallback, sandbox setup repair, global configuration change, permissions escalation or existing-pair mutation was used. The disposable pairs were forgotten after collecting their results. This demonstrates current shell/I/O recovery, not an assertion that an intermittent sharing violation can never recur.

If it recurs, inspect the current setup error and dated sandbox log before changing policy. Do not terminate a shared runtime or silently fall back to unelevated mode. OpenAI's [Windows sandbox guide](https://learn.chatgpt.com/docs/windows/windows-sandbox) documents that fallback's weaker network isolation and lack of denied-read support; it requires a separate user decision. No automatic fallback was implemented. Local evidence: `work/callback-path-fix-2026-10-13.log`, `work/callback-base018-2026-10-13.log`, the two disposable task results, and the installation receipt.

### October 14 large manager rollouts

Two sends were refused because the active manager's transcript had exceeded the old 128 MiB inspection ceiling (about 140 MiB when inspected). Pair work continued, but those sends created no notification ticket. The bounded streaming reader replaces that ceiling; it preserves identity, latest policy/context, source-turn, rollback and receipt checks. Schema 5 isolates new registrations from still-loaded size-limited dispatchers. Older terminal blocked records are not automatically reopened.

A read-only check against the actual active manager's 147,055,062-byte rollout passed eligibility. Selecting its current turn and validating it took 0.751 seconds with about 280 KiB peak traced Python inspection allocations; this excludes imported modules and the OS file cache. No callback was sent to that manager, and its pairs were not stopped or changed. The regression suite arms and delivers through the simulated transport with a 129 MiB single tool-output record, checks that the giant payload never reaches JSON decoding, preserves policy/rollback checks after large irrelevant events, rejects oversized control records, and covers partial EOF, Unicode, receipt chunk boundaries and concurrent appends.

Already-running MCPs retain old code. Loading the new snapshot requires a fresh MCP/chat when safe; installation does not restart an active manager or its pairs. Results from the two unarmed sends must be collected with `pair_poll`, not re-sent. Local evidence: `work/callback-large-rollout-focused-2026-10-14.log` and `work/callback-large-rollout-offline-2026-10-14.log`.

### October 28 v0.20 refresh

Merged released main `05ddc87` into the separate callback branch. Includes hidden Windows spawns, upstream compaction accounting, private scratch TEMP and permission profiles, persistent Codex developer instructions, compressed `.jsonl.zst` recovery, the auto-escalation repair, and Claude's opt-in auto-watch hook. Hook settings were not edited. Existing upstream credential-denial ACL behavior and auto-mode limitations are documented in README/CHANGELOG and apply to the clone too; billing was not changed.

Conflict resolution uses upstream's newer compaction and compressed-rollout logic. Claude tool-specific hook hints and Codex Context metadata are both preserved. Codex replies prioritize their callback status, including disabled/refused status, and never claim the Claude hook is armed. Compressed manager inspection defers and asks the user to resume that manager; the dispatcher does not start a model or decompress a manager itself.

Desktop 26.1023.6123.0 moved the callback endpoint from `codex-browser-use-*` to `codex-app-tools-*`. Discovery accepts both spellings while retaining owner, ancestry and schema validation. Notification schema 6 fences older dispatchers with obsolete discovery. A real isolated stdio MCP captured this manager's metadata, ran a read-only Luna worker, sent a completion notice through the new pipe and collected `PAIR_WORK_DONE`; the notice reached this chat. This was an active-manager test; the earlier idle-wake evidence remains recorded above.

Validation: 33 offline suites; final 64 completion tests (including hook precedence, compressed-manager handling and pipe names); live sandbox harness **51/52**. Scratch/read-only/workspace/auto boundaries, canary credential denial, forks, worktree writes, Python execution and instruction retention through compaction/clear passed. Python urllib HTTPS failed `CERTIFICATE_VERIFY_FAILED` (unable to get local issuer certificate); HTTPS Git passed. This certificate trust issue is not fixed, and TLS verification was not bypassed. Live harness ACLs targeted a synthetic credential canary, not real credential files. Disposable workers were forgotten; original user pairs were not stopped. First live attempt stopped at a CP1252 print error; rerun used UTF-8.

Local evidence: `work/base020-offline-2026-10-28.log`, `work/base020-completion-final-2026-10-28.log`, `work/base020-live-sandbox-2026-10-28b.log`, `work/callback020-live-20261028/summary.json`. Installation remains a new immutable clone snapshot; active MCP processes retain their loaded version until a safe reload.

## Files and release handoff

New: `completion.py`, `desktop_bridge.py`, `test_completion.py`, `completion_mcp_fixture.py`, and `experiments/completion_smoke.py`. Small integrations: `server.py`, `__main__.py`, offline runner and README. Integration changes the completion default to auto-detection; no dependencies, warm-runtime rewrite, unrelated shutdown-race fix, registry migration or reinstall.

The smoke harness is offline by default. `--live` requires explicit disposable manager/turn IDs. `--restart` restarts only its own isolated MCP; it never installs or restarts the user's server. Results, worker transcripts and manager evidence were preserved locally; test pairs were forgotten and their threads archived.

Before shipping: review/integrate this branch when pairs are idle, choose a version, build/verify through the existing workflow, then enable in a fresh Codex session. Never switch active MCPs to this worktree, patch Desktop, stop other pairs or kill processes by image name.
