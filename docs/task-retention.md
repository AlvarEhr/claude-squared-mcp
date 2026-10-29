# Async task result retention

`server.run()` starts one daemon housekeeping worker per home. It attempts a
pass at startup and then waits a day. A cross-process FileLock and persisted
`async/.retention` attempt timestamp enforce at most one pass per 24 hours
across MCP servers and restarts. The attempt is stamped before work so repeated
failures do not create a cleanup storm. Startup/cleanup failures are logged and
do not prevent the server from serving tools.

Only `.json` task results whose mtime **and** `finished_at` are older than
14 days are eligible, with status `done`, `failed`, or `stopped`. Running,
queued, explicit/unresolved detached state, local pending results and live or
unverifiable child processes are kept. Missing dates, malformed/oversized task
data, and changing files are kept. Per-task reads are capped at 16 MiB.

All activity JSON (including old owners) is checked for references before any
deletion; local in-memory writer snapshots protect references not flushed yet.
Unreadable or unknown-schema activity aborts the pass rather than guessing.
Activity reads are capped at 2 MiB and transient OS read failures retry three
times. Referenced tasks remain regardless of age.

Deletion acquires the existing per-pair OS lock nonblocking, re-reads task age
and state, and rechecks that pair's activity references. Busy pair locks are
skipped. The order is housekeeping lock → pair OS lock → short memory snapshot
locks; no control lock or model/runtime action is taken. Task identities are
never reused. Only task JSON is removed; transcripts, main logs, Codex event
files, cancellation markers and locks are left alone. Counts are logged after
each attempted pass. A pruned task ID no longer resolves for polling, while its
conversation remains in the backend transcript/logs.

`tests/test_task_retention.py` uses fresh worktree homes, no live pairs. It
covers eligibility/protected states, activity references appearing during the
pass, unreadable/future-schema activity, local unflushed references, locked
pairs, changed mtimes, the persisted daily gate and server/worker startup.
