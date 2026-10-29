# Pairs pane and restart recovery: maintainer notes

## Validation evidence

- **2026-10-26, main-session live probe:** Desktop's Claude Code CLI **2.1.308**
  kept the session ID on `--resume`; a SessionStart `asyncRewake` hook woke the
  idle stream-json manager. This was reported by the main session after running
  `tests/smoke_live_sessionstart.py`, not run by the implementer. No inference is
  needed for plugin validation/testing. The maintainer then confirmed the pane
  loads and works in Desktop; screenshots of it motivated the layout round below.
- Quench found an approximately 80 MiB snapshot read from ten completed task
  files. The reader now caps reads before opening oversized files and filters
  old history by mtime. The regression uses ten 8 MiB historical task files plus
  a recent oversized file, and checks that none is opened.
- Quench reproduced duplicate recovery wakes by holding a Windows watch file
  open. A regression now holds a real handle without delete sharing, completes
  the watch, and verifies both SessionStart and pair_poll recovery are silent.
- **2026-10-29, manager observation:** a Desktop restart at 14:14 removed the old
  auto-watch hook; a later send armed a new one. This is one observed restart,
  not proof every host kills every hook. Newly published records carry the
  native Claude ancestor's PID/birth time when discoverable. Normal/recovered
  wait sleeps check it at least every 250 ms; death/reuse exits quietly, keeps
  the record, and releases the kernel owner lock. Recovery can recognize such
  a live-hook/dead-host record and allow the old owner time to release it.
  Legacy records and unidentified ancestry retain PID-only behavior. Tests use
  only their own fixture host/hook PIDs, never image-name process control.
- **2026-10-29, manager live probe:** `--immediate` passed with Desktop's CLI
  **2.1.308**, waking in **5.39 seconds** with no user message after resume.
  The probe uses a completed-work fixture before
  resume and writes zero user messages after resume. It records launch/input
  count and requires WOKEN within `--wake-seconds` (30 by default). This result
  was reported by the manager, not run by the implementer. In the Desktop
  real-MCP check, verify `host_pid` is the session CLI, not the Desktop app.

## Snapshot contract

`read_pairs.py` accepts the home in argv and `{session, all, expanded, cache}`
on stdin. It writes a snapshot and a compact task-summary cache to stdout. The
mod keeps the cache in memory; the reader never writes pair state. Cache keys
include string-encoded nanosecond mtimes (JavaScript numbers lose precision).

The reader reuses the installed wait.py's merged view/PID checks, but supplies
a bounded task set and bounded JSON reads. Limits live beside the reader:

| Limit | Value |
| --- | --- |
| Recent task window | 2 hours (also the recent-completion group) |
| Supplemental task scan | 150 ms elapsed budget; POSIX additionally caps at 50,000 entries |
| Candidate tasks | 128, with activity-referenced / previously running IDs eligible regardless of age |
| Task file | 64 KiB; size checked before opening |
| Other JSON file | 128 KiB; watch/receipt 8 KiB; registry 2 MiB |
| Combined JSON I/O | 4 MiB and 128 file reads per refresh |
| Detailed pairs | 64; registry-only/omitted-detail rows remain visible beyond that |
| Priority metadata | Stat-only activity scan: 2,048 total entries/directories, at most 16 entries per pair |
| Watch discovery | Newest 24 records; overflow is reported |
| Initial task reads | At most 48 uncached task files, reserving 32 remaining file slots for activity; references share the global limits |
| Cross-refresh task cache | 256 KiB of metadata; no prompt/result bodies |
| Log detail | 8 KiB only for an expanded, selected row; initial display 15 lines |
| Reply detail | Latest selected task's full response within its 64 KiB file cap; initial display 12 lines |
| Combined detail output | 1 MiB per snapshot |
| Transcript | Explicit open/page only; 64 KiB per page, user/assistant text; no restore/decompression |

Only matching session watches select pairs in the default view. All-pairs mode
lists registry pairs, plus non-registry pairs with busy/recent completion evidence;
old forgotten activity directories do not create idle or budget-skipped rows.
A stat-only activity pass identifies likely live owner files; their turn,
background and wake records are read before async discovery and supply primary
busy evidence and task references. The async scan supplements this, using cached
Windows DirEntry mtimes (POSIX pays for stat) to skip old payloads before opening.
The 10,000-old-file regression finds a late-alphabet running task both with and
without activity references, keeps all other rows idle, and opens no old JSON.
Another regression times out the supplement and still finds the referenced turn.
Old idle rows come directly from the registry and keep idle
status when their per-pair scan is complete, even if unrelated task history hit
a limit. Missing/incomplete discovery remains unknown; skipped detail alone
does not change known status. The 60-pair
regression puts the busy pair alphabetically last and verifies full detail with
no idle activity reads. A separate exhausted-budget fixture retains all 70 names.
Filtering precedes activity JSON and log reads. Expanded rows read latest finished/current task payloads within the same
byte/file budget. Activity-referenced previous completed tasks remain eligible
even beyond the two-hour scan window. Old-history omissions alone do not alter
idle status. Any exhausted scan/read/detail budget produces a notice; rows that
cannot be read stay compact and explicitly marked, including idle candidates
whose task discovery was incomplete. Other notes are gated on known affected
working/recent rows.
An old queued task without an activity reference or cached running summary may
be omitted; use pair_status/pair_poll when completeness matters.

The view puts busy pairs first, then attention/watched rows, recent completions
(newest first), and a collapsed compact Idle group. Desktop's element table has
no Progress/Meter, but supports percentage-width Boxes with background colours;
these draw height-1 context bars, with text bars only on terminal. The header
places percent used left and k/M counts (at most one decimal) right. No ring or
rounded-corner prop is available, so a filling-circle glyph beside the name
also shows level. Both use blue below 80% of the shared alert_at, yellow from
80%, and red at/above it; no independent compaction threshold is guessed.
The widget has no category breakdown or footer. Compact Idle rows keep only the
indicator/counts; unknown fill has muted text and no bar. Idle Claude fallback
reads already-budgeted activity/SendResult context,
excluding known pre-compaction reports; it never uses cumulative result usage.
Completion timestamps/outcomes come from activity history and task summaries,
not log usage markers. Failed task supervision prefixes distinguish orphaned
and crashed from ordinary failed/stopped outcomes. These show attention status
and No reply labels; error strings are never substituted for model replies.
Successful replies quoting such prefixes remain replies.

The 2026-10-29 screenshot's one-hour discrepancy came from naive UTC task times
being passed straight to JavaScript Date (which treated them as local), while
the header had an aware timestamp. Task reply times now normalize to aware UTC;
the shared UI formatter also recognizes naive UTC and uses the real local zone
per timestamp. Mod tests exercise Europe's 2026-10-25 DST transition explicitly.

Expanded log detail skips bookkeeping markers and reads the physical last
8 KiB, never sorting HH:MM:SS labels. A warm-cache regression appends lower clock
times from a newer day and checks their physical order. The reader already used
physical tails before this report; the UI now discards cached detail when a
requested refresh returns none, or the row's outcome/status/completion changes.

Task state has owner_pid but no manager-session identifier. Without watches,
PID alone cannot safely supply the requested session fallback, so Waiting on
offers All and auto-watch setup guidance. No extra process inspection or
transcript scan was added. Hidden idle groups request no log detail.

Model labels retain slug versions. Floating aliases use same-family IDs from
already-read reported-window metadata (matching model_key) or bounded task
SendResult.model_used, newest report first. This is last-observed resolution,
not a query of the provider's current alias mapping. Explicit versioned registry
IDs stay authoritative; a different family's old turn cannot resolve an alias.

The five-second timer asks `$.ui.panes()` about placement and visibility before
launching a Python reader. Clicks update cached view/expansion state, then enqueue
a background refresh; More controls only reveal cached lines. One reader at a
time covers both snapshots and explicit transcript pages. Per-scope snapshots
preserve the last view when toggling; expanded detail is retained separately from
the task-summary cache. Hidden tabs and unplaced panes perform no filesystem
refresh. Close/clear cancels pending timers and invalidates in-flight results.

`ProcessSpawnRequest.input` in the copied 2.1.308 API is a single string followed
by closed stdin. There is no stdin writer, so a long-lived request/response
reader cannot be implemented with that API. One-shot `process.run` remains;
refresh is never awaited by an input handler. A deferred-reader mod regression
holds the process result indefinitely while switching views and expanding/
collapsing rows, and asserts no overlapping launches.

2026-10-26 local timing (`.venv/Scripts/python-direct.exe -B
scripts/benchmark_pairs_pane.py`, isolated 45-pair fixture, seven subprocess
samples): median 153.03 ms before, 126.83 ms after; warm I/O unchanged at 11,285
bytes / 46 files. Treat the small per-call difference as measurement noise.
The interval cuts scheduled launches from 30 to 12/minute (60%); clicks do not
await those reads. These are offline reader timings, not Desktop frame timings.

Transcript lookup is explicit only: Claude's home/projects/cwd/session path,
or Codex's indexed thread path from an immutable SQLite connection
(`mode=ro&immutable=1`). A checkpointed WAL-database regression asserts no files
appear in the source directory. Immutable mode reads checkpointed state only;
a new path still only in the live WAL may be unavailable until checkpointed.
No recursive
rollout search or model call. Missing/compressed files and oversized JSONL
records have visible explanations. Pages cover user/assistant text; tool and
metadata records are skipped. Log timestamps are already local (runtime.py's
datetime.now); ISO reply/transcript times are formatted locally in the mod.

Recovery and pane skip watch JSON older than the seven-day hook timeout by
mtime before opening it; recovery best-effort removes those expired records
and their receipts, while the pane never writes. Recovery retries registry
reads three times (also under the gate), retries publication PermissionError,
queues a result even for worker BaseException, and names cancelled unfinished
waits in the first wake. The offline runner now includes plugin validate/test
when the CLI is available, otherwise reports a clean skip.
Recovery scans this session's records before reading the registry; no recoverable
records or a missing registry is silent. Corrupt registries only warn when work
needs recovery. Host birth-time comparison uses wait.py's one-sided
`_AV_START_TOLERANCE_S` rule, tolerating small/negative Linux btime shifts rather
than exiting a live hook on a wall-clock adjustment.

## Watch retirement

Watch ownership is serialized by the existing `(session, pair)` gate and owner
locks. On an outcome, the hook atomically publishes and flushes a separate
`<watch-key>.done` receipt **before** trying to unlink the watch JSON. The
receipt identifies `hook_pid` and `started_at`, so it does not suppress a later
watch of the same pair. Windows readers holding the JSON may block replacement
and deletion of that file; they do not block this independent receipt.

Recovery and pane readers exclude matching terminal receipts even if the JSON
remains. Successful cleanup removes both files; failed cleanup retains the
receipt. Interrupted/cancelled waits do not retire their records. If completion
cannot be persisted at all, the hook reports that failure instead of promising
duplicate suppression. This is not a delivery-acknowledgement protocol: a host
can still die between retirement and receipt of the wake.
