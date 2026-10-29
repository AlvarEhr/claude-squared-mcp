# Steering implementation and verification

`pair_send_async(steer=True)` attempts a same-process Claude runtime write before
creating a queued task. Fallback is explicit, including Codex and self-woken
turns. No cross-process mailbox or new backend is introduced.

## Local protocol findings — 2026-10-29

- The installed Claude executable's embedded schema documents
  `result.user_message_uuids`: client UUIDs consumed by a turn, including user
  messages folded into it between tool rounds. The list is capped at 64. The
  implementation sends UUIDs on normal inputs, requires observed correlation
  support before steering, and allows at most 63 steers per task. This is schema
  inspection, **not a live inference measurement**.
- `codex-cli 0.165.0-alpha.1.2 app-server generate-json-schema --out <scratch>`
  exposes `turn/steer` with required `threadId`, `expectedTurnId`, `input`, and
  optional `clientUserMessageId`. The response contains `turnId`. Our per-turn
  file-backed `exec` has no app-server connection to address; using that protocol
  requires a backend change. No live Codex inference was run.
- Run `python scripts/probe_claude_steer.py` from an authenticated manager
  environment before release. It uses Haiku/low, isolated `CLAUDE_HOME` and
  `CODEX_HOME`, a unique scratch cwd, and no package imports or pair registry.
  It writes a follow-up during a foreground sleep and saves raw events under
  `.test-homes/steer-probe-*`. Inspect whether the first result says
  `STEER_CANARY`, which UUIDs it consumed, and whether a second result appears.
  The CLI may write its unique transcript in its normal projects directory,
  as with the existing live harnesses. Only its own spawned PID is terminated.

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

Activity additions are `turn.steer_count` and
`turn.last_steer = {message_id, at, preview}`; completed window entries retain
both. No schema-version change and no pane edits.

## Offline coverage

`tests/test_steer.py` checks existing-task reuse, explicit fallbacks, correlation
cap, stop/error and write failure handling, multiple follow-ups, result-vs-write
races, cost/duration aggregation, and the actual send loop with synthetic events.
It sets isolated homes before importing the package and is listed in
`scripts/run_offline_tests.py`.

Verification on 2026-10-29: `python tests/test_steer.py` — 15 passed;
`python scripts/run_offline_tests.py` — 36 suites passed, only `pairs_pane_mod`
failed (17 pane tests: local CLI refuses `Text` prop `size`; pane files belong
to the manager). Final targeted `test_maintenance*.py`, `smoke_selfwoken.py`
and `smoke_v0100.py` runs also passed after the stdin-lock change. No live
Claude/Codex model runs were performed by the implementer.

Windows test setup finding: a venv redirector's `Popen.pid` differed from its
Python child's `os.getpid()` (verified with a stdlib-only subprocess probe).
PID-sensitive lifecycle tests need the system Python executable, with private
dependencies on its user-site path. Also clear inherited `GIT_CONFIG_*` when
testing the default network configuration; that test expects no existing Git
config override. These are test invocation details, not server changes.
