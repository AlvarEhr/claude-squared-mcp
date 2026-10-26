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
| Log detail | 8 KiB only for an expanded, selected row |

Only matching session watches select pairs in the default view. All-pairs mode
also selects registered/activity pairs. Filtering precedes activity and log
reads. No separate reply-detail/transcript fetch is performed. Budget/age omissions set a partial
notice, and idle becomes unknown rather than claiming all work was observed.
An old queued task without an activity reference or cached running summary may
be omitted; use pair_status/pair_poll when completeness matters.

The view puts busy pairs first, then attention/watched rows, recent completions
(newest first), and a collapsed compact Idle group. Desktop's element table has
no Progress/Meter, but supports percentage-width Boxes with background colours;
these draw context bars, with text bars only on terminal. Unknown fill has no
bar. Idle Claude fallback reads already-budgeted activity/SendResult context,
excluding known pre-compaction reports; it never uses cumulative result usage.
Completion timestamps/outcomes come from activity history and task summaries,
not log usage markers. Expanded log detail skips bookkeeping markers.

Task state has owner_pid but no manager-session identifier. Without watches,
PID alone cannot safely supply the requested session fallback, so Waiting on
offers All and auto-watch setup guidance. No extra process inspection or
transcript scan was added. Hidden idle groups request no log detail.

Model labels retain slug versions. Floating aliases use same-family IDs from
already-read reported-window metadata (matching model_key) or bounded task
SendResult.model_used, newest report first. This is last-observed resolution,
not a query of the provider's current alias mapping. Explicit versioned registry
IDs stay authoritative; a different family's old turn cannot resolve an alias.

The two-second timer asks `$.ui.panes()` about placement and visibility before
launching a Python reader. Hidden tabs and unplaced panes perform no filesystem
refresh. Close/clear cancels polling and invalidates any in-flight result.

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
