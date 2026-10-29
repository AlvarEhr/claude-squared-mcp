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
| Task-directory entries examined | 2,048 per refresh |
| Candidate tasks | 128, with activity-referenced / previously running IDs eligible regardless of age |
| Task file | 64 KiB; size checked before opening |
| Other JSON file | 128 KiB; watch/receipt 8 KiB; registry 2 MiB |
| Combined JSON I/O | 4 MiB and 128 file reads per refresh |
| Selected pairs | 64; at most 16 activity-directory entries per pair |
| Cross-refresh task cache | 256 KiB of metadata; no prompt/result bodies |
| Log detail | 8 KiB only for an expanded, selected row; initial display 15 lines |
| Reply detail | Latest selected task's full response within its 64 KiB file cap; initial display 12 lines |
| Combined detail output | 1 MiB per snapshot |
| Transcript | Explicit open/page only; 64 KiB per page, user/assistant text; no restore/decompression |

Only matching session watches select pairs in the default view. All-pairs mode
also selects registered/activity pairs. Filtering precedes activity and log
reads. Expanded rows read latest finished/current task payloads within the same
byte/file budget. Activity-referenced previous completed tasks remain eligible
even beyond the two-hour scan window. Old-history omissions alone do not alter
idle status. A dim footnote is gated on known affected working/recent rows.
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
or Codex's indexed thread path from a read-only SQLite connection. No recursive
rollout search or model call. Missing/compressed files and oversized JSONL
records have visible explanations. Pages cover user/assistant text; tool and
metadata records are skipped. Log timestamps are already local (runtime.py's
datetime.now); ISO reply/transcript times are formatted locally in the mod.

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
