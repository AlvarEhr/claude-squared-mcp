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
error results with exit 0. Login guidance requires a top-level `is_error` result
starting with an auth phrase, or `terminal_reason: api_error` with zero reported
token usage and an auth phrase; structured `loggedIn: false` also qualifies.
Plain diagnostics (including `[MCP github] Failed to authenticate`) remain their
own messages. Successful result envelopes are never classified, even if the
process exited nonzero. Assistant/tool events quoting errors are excluded.
Generic result text comes before the exit code;
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
plain and structured auth diagnostics, MCP/successful-result negative controls,
zero-usage evidence, and API billing.
No live model probe was run by the implementer.
