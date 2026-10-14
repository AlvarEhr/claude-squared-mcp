# Windows launch visibility

Investigation and validation: 2026-10-14. This change addresses concrete missing
hide options and console inheritance gaps in production launchers. It does not
attribute previously reported terminal flashes or prove they are all resolved.

## Policy and scope

`process_spawn.hidden_process_kwargs` copies launch options and any supplied
`STARTUPINFO`. On Windows, tool-capable roots request `CREATE_NEW_CONSOLE` with
`STARTF_USESHOWWINDOW` / `SW_HIDE`. Ordinary console descendants can inherit that
hidden console, including when their standard handles are pipes. Short leaves
request `CREATE_NO_WINDOW`. Unrelated creation flags, environment, stdin,
stdout, stderr and other options are retained. POSIX options pass through.

| Launcher | Policy |
| --- | --- |
| Codex exec, app-server and run/popen wrappers | Hidden inheritable console |
| Claude warm runtime, one-shot print and stream-json | Hidden inheritable console |
| Connector inventory queries | Hidden inheritable console (queries may start servers) |
| Codex version and Claude help | No console |
| Tool-free triage classifier | No console |
| All production `taskkill` calls | No console |

`CREATE_NEW_PROCESS_GROUP` is omitted on Windows: it is ignored with
`CREATE_NEW_CONSOLE`, and these cancellation paths use exact-PID `taskkill /F /T`
rather than console group signaling. POSIX session/group behavior is unchanged.
Detached Codex cancellation still retains a matching creation-time process
handle across the kill. File-backed exec receipts/recovery are unchanged.

Microsoft documents console creation/inheritance in
[Creation of a Console](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console),
the mutually exclusive flags and ignored process-group combination in
[Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags),
and startup visibility/handles in
[STARTUPINFOW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow).

This policy cannot suppress a descendant that explicitly allocates/shows a new
console, opts into a separate terminal/pseudoconsole, or starts a GUI program.
It adds no sandbox/configuration changes, job breakaway, or context clearing.

## Verification and observation limits

The new offline policy tests cover option copying, redirected handle preservation,
unrelated flags, POSIX pass-through, retry behavior, app-server pipes and query
leaf classification. Existing focused regressions passed: `test_codex_detach.py`
39 tests, and `test_codex_exe.py` 16 tests. The manager's full isolated runner
passed 28 of 29 suites; the maintenance suite hit a transient Windows
PermissionError reading its own temporary async-task JSON. A focused rerun of
that entire suite passed all 53 tests, giving coverage of all 29 suites. The
original failure is retained as a test limitation, not described as a clean
single full run. The manual empirical probe is skipped by the automatic runner.

A first desktop-isolation attempt was invalid: processes on an unshown desktop
had console HWNDs that were unqueryable from that desktop. A terminal flash was
reported near these attempted negative controls. The controls were stopped;
that timing suggests a test-related flash, but does not identify the user's
earlier flashes. Do not treat an unshown thread desktop as sufficient containment
for delegated console hosting. No visible negative controls remain in the fixture.

A subsequent bounded positive-only probe used a windowless supervisor on the
Default desktop, with an independent passive window-event trace armed before
launch and through cleanup. Visibility APIs were first validated against an
existing visible top-level window without collecting its content or manipulating
it. The synthetic console HWND was valid/queryable, `ConsoleWindowClass`, on
Default, and lacked both `IsWindowVisible` and `WS_VISIBLE`. The independent
trace recorded hidden creation and no show event for that window.

The extension probe then verified:

- A root and two ordinary piped console descendants shared the same hidden
  classic console; input, stdout and stderr canaries reached all generations.
- Five short no-console leaves reported no console HWND.
- Exact-PID tree cancellation killed the synthetic root and both descendants;
  creation-time identity checks verified their exit.
- A file-backed child survived termination of only its synthetic supervisor,
  retained its hidden console, and launched another ordinary child sharing it
  after supervisor death. Both completed; output/error files remained readable.

Two independent passive traces agreed on hidden creation for all three extension
consoles, with no show/hide/foreground callbacks in the bounded test interval.
This validates the synthetic hidden-launch behavior on the tested Windows host.
It is not a real model/tool turn, a visible A/B test, or proof that arbitrary CLI
descendants obey console inheritance. A separate initial passive observation of
the user's desktop saw no flash and did not attribute the original report.

## Running tests safely

Routine policy regressions:

```powershell
python -B tests/fixtures/hidden_desktop.py -- python -B tests/test_process_spawn.py
```

The stdlib desktop helper requests a hidden root and redirects output. It must
not be used as authorization to run intentionally visible console controls:
Windows Terminal delegation may display on another desktop. It never switches
the user's desktop or manipulates their windows.

The optional `windows_console_probe.py` supports `default-single` followed by
`default-extension`, from `pythonw.exe` on Default, using only hidden roots and
no-console leaves. Run manually only with independent passive event observation
already armed; abort on an unqueryable/nonclassic HWND, unexpected visibility,
or inconclusive observation. Never add `SW_SHOW`/plain-console negative controls
without independently verified containment. Cleanup targets only synthetic PIDs
the harness started, retaining process handles/creation-time guards; no image-name
kill and no speculative console-host termination.

All package imports in the harness use temporary `CLAUDE_HOME` and `CODEX_HOME`
established first. No model calls, authenticated CLI sessions or real pair state
are involved.
