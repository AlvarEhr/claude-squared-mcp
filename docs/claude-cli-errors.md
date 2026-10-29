# Claude CLI failure messages

On 2026-10-29 the manager verified a failed `pair_clear` was an expired Claude
CLI OAuth login. The CLI exited 1 with zero usage and this result:

```json
{"is_error":true,"terminal_reason":"api_error","result":"Failed to authenticate: OAuth session expired and could not be refreshed"}
```

`claude auth status` reported `loggedIn: false`. This was a login problem, but
the old exception displayed only the beginning of the large JSON envelope.

The Claude adapter now extracts the last top-level CLI result before formatting
an error. Print and stream-json paths share the formatter, including explicit
error results with exit 0. It never diagnoses auth from assistant/tool events
that happen to quote an error. Generic result text comes before the exit code;
raw JSON envelopes are not used as the error headline.

Plan-billed authentication failures say the CLI is not logged in, include its
message, and direct the **user** to `claude auth login` in a terminal. No auth
status command, credential read, login attempt, or retry is added. API-billed
pairs keep v0.18's rejected-key, workspace-scope and credit guidance. Warm sends
carry the same login explanation in their response/error signal; runtime pipe
and exit errors share the formatter while retaining the supervisor prefix.

`tests/test_claude_errors.py` uses isolated homes and mocked CLI subprocesses.
It covers the manager's exact envelope (with and without `type: result`), long
stream preambles, lifecycle calls including clear/hard stop, warm results,
plain and structured auth diagnostics, quote-negative controls, and API billing.
No live model probe was run by the implementer.
