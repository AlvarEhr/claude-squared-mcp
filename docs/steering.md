# Steering implementation and verification

`pair_send_async(steer=True)` attempts a same-process Claude runtime write before
creating a queued task. Fallback is explicit, including Codex and self-woken
turns. No cross-process mailbox or new backend is introduced.

## Local protocol findings — 2026-10-29

- The installed Claude executable's embedded schema documents
  `result.user_message_uuids`: client UUIDs consumed by a turn, including user
  messages folded into it between tool rounds. The list is capped at 64. The
  implementation sends UUIDs on normal inputs, requires observed correlation
  support before steering, and allows at most 63 steers per task. The manager's
  live probe on CLI 2.1.296 returned `STEER_CANARY` in one result containing both
  input UUIDs (reported 2026-10-29), confirming consumption during the turn.
  On that CLI the first turn after each spawn/respawn cannot be steered: support
  is learned from its first result. Follow-ups before that queue normally.
- `codex-cli 0.165.0-alpha.1.2 app-server generate-json-schema --out <scratch>`
  exposes `turn/steer` with required `threadId`, `expectedTurnId`, `input`, and
  optional `clientUserMessageId`. The response contains `turnId`. Our per-turn
  file-backed `exec` has no app-server connection to address; using that protocol
  requires a backend change. No live Codex inference was run.
- `python scripts/probe_claude_steer.py` reproduces the successful manager
  probe. It uses Haiku/low, isolated `CLAUDE_HOME` and
  `CODEX_HOME`, a unique scratch cwd, and no package imports or pair registry.
  It writes a follow-up during a foreground sleep and saves raw events under
  `.test-homes/steer-probe-*`. Inspect whether the first result says
  `STEER_CANARY`, which UUIDs it consumed, and whether a second result appears.
  The CLI may write its unique transcript in its normal projects directory,
  as with the existing live harnesses. Only its own spawned PID is terminated.
- `python scripts/probe_claude_steer.py --interrupt` writes the same user/steer
  frames followed by `control_request: interrupt`. It records the interrupt
  result's consumed UUIDs and leaves stdin open for up to 40 seconds after the
  first result to observe a separate steered turn. Its summary lists result
  subtypes, UUIDs, and canary matches. Exit 0 means evidence was captured, not
  that queued input was cancelled. This mode has **not been run**; the manager
  will use it to decide whether the conservative hard stop can be avoided.

## Bookkeeping and locks

`_input_lock` serializes complete stdin frames (send, steer, interrupt). It is
never held by the stdout reader. A steer reserves its UUID under `_scope_lock`,
then releases that lock before pipe I/O. The original send marks `input_sent`
only after its write, preventing a steer from overtaking it. Steering never
takes the turn-long pair/send locks or the promotion lock. Existing lock order
is unchanged; new order is input → scope → activity leaf.

The reader removes only UUIDs named by result events. An intermediate result
with pending steers does not close activity, snapshot the scope or enter the
send queue. Once all messages have results, the original task returns the final
reply, aggregated cost/duration/permission denials and the entire log range.
No second running task or activity window exists. Interrupted/failed turns
with unconsumed input retire the runtime before releasing the pair lock,
preventing queued CLI input from escaping into another send. A partial stdin
write is ambiguous: report it, stop the runtime, and never retry automatically.
The original send's hard timeout still bounds this task; steering does not
restart or extend it. The synchronous send's wait limit is unchanged too.

The reader publishes a task-scoped discard decision before the worker stops
the runtime. `send_interrupt` waits within its existing deadline for the stop's
outcome, returning a hard-stop description instead of a soft-stop ACK; if the
worker has not finished, it reports a hard stop in progress. `pair_stop` also
checks the selected task's report if the worker finished before inspection,
including the recorded count of lost background tasks. No second kill is
requested while the worker's discard is in progress. A retired runtime detected
before the steer write reports "turn ended before the follow-up was delivered;
send it normally"; a retirement after the write remains explicitly uncertain.

Activity additions are `turn.steer_count` and
`turn.last_steer = {message_id, at, preview}`; completed window entries retain
both. No schema-version change and no pane edits.

## Offline coverage

`tests/test_steer.py` checks existing-task reuse, explicit fallbacks, correlation
cap, stop/error and write failure handling, multiple follow-ups, result-vs-write
races, cost/duration aggregation, and the actual send loop with synthetic events.
It sets isolated homes before importing the package and is listed in
`scripts/run_offline_tests.py`.

Review verification on 2026-10-29 (isolated homes, system Python):

- `python tests/test_steer.py`: 22 passed.
- `python tests/smoke_selfwoken.py` and `python tests/smoke_v0100.py`: passed.
- `python -m unittest discover -s tests -p '<pattern>' -v`: passed for
  `test_maintenance*.py` (53), `test_activity.py` (49), `test_v016.py` (67),
  and `test_auto_watch.py` (44). Maintenance's first run hit a transient Windows
  PermissionError reading fixture task JSON; a fresh isolated rerun passed.
- `python scripts/probe_claude_steer.py --help`: passed (no model call).

The full offline runner was not repeated for this review. No live Claude/Codex
model runs were performed by the implementer; the success-path probe was run
by the manager, and the interrupt probe remains pending.

Windows test setup finding: a venv redirector's `Popen.pid` differed from its
Python child's `os.getpid()` (verified with a stdlib-only subprocess probe).
PID-sensitive lifecycle tests need the system Python executable, with private
dependencies on its user-site path. Also clear inherited `GIT_CONFIG_*` when
testing the default network configuration; that test expects no existing Git
config override. These are test invocation details, not server changes.
