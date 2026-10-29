"""Run the maintained offline tests in isolated Python processes.

Run with ``python scripts/run_offline_tests.py`` from any working directory.
The explicit allow-list excludes smoke scripts that invoke live models.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SUITES = (
    "listparse", "crossproc_lock", "selfwoken", "v08", "v081", "v094",
    "v095", "v096", "v097", "v098", "v099", "v0910", "v0911",
    "v0100", "v0110", "v0120", "codex",
)


def main() -> int:
    if len(sys.argv) != 1:
        print("Usage: python scripts/run_offline_tests.py (offline only)", file=sys.stderr)
        return 2

    commands = [
        (f"smoke_{suite}", [str(ROOT / "tests" / f"smoke_{suite}.py")])
        for suite in SMOKE_SUITES
    ]
    commands.append(("maintenance", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_maintenance*.py", "-v",
    ]))
    commands.append(("handoff", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_handoff.py", "-v",
    ]))
    commands.append(("codex_detach", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_codex_detach.py", "-v",
    ]))
    commands.append(("codex_activity", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_codex_activity.py", "-v",
    ]))
    commands.append(("connectors", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_connectors.py", "-v",
    ]))
    commands.append(("activity", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_activity.py", "-v",
    ]))
    commands.append(("v016", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_v016.py", "-v",
    ]))
    commands.append(("codex_exe", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_codex_exe.py", "-v",
    ]))
    commands.append(("build_script", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_build_script.py", "-v",
    ]))
    commands.append(("pair_options", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_pair_options.py", "-v",
    ]))
    commands.append(("billing", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_billing.py", "-v",
    ]))
    commands.append(("process_spawn", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_process_spawn.py", "-v",
    ]))
    commands.append(("codex_sandbox", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_codex_sandbox.py", "-v",
    ]))
    commands.append(("codex_zst", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_codex_zst.py", "-v",
    ]))
    commands.append(("auto_watch", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_auto_watch.py", "-v",
    ]))
    commands.append(("pairs_pane", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_pairs_pane.py", "-v",
    ]))
    commands.append(("claude_errors", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_claude_errors.py", "-v",
    ]))
    commands.append(("task_retention", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_task_retention.py", "-v",
    ]))
    scratch = ROOT / ".test-homes"
    scratch.mkdir(exist_ok=True)
    failures = []
    for name, arguments in commands:
        print(f"\n=== {name} ===", flush=True)
        # Set isolation in the child environment BEFORE it imports the package:
        # server/async_tasks imports can otherwise touch real user state.
        with tempfile.TemporaryDirectory(prefix=f"cs-offline-{name}-", dir=scratch,
                                         ignore_cleanup_errors=True) as tmp:
            env = os.environ.copy()
            env.update({
                "CLAUDE_HOME": str(Path(tmp) / "claude"),
                "CODEX_HOME": str(Path(tmp) / "codex"),
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                # Contain scratch directories created by the older smoke scripts.
                "TMP": tmp, "TEMP": tmp, "TMPDIR": tmp,
            })
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-u", *arguments],
                    cwd=ROOT, env=env, check=False,
                )
            except OSError as exc:
                print(f"FAIL {name}: could not start Python: {exc}", file=sys.stderr)
                failures.append(name)
                continue
            if result.returncode:
                failures.append(name)
                print(f"FAIL {name}: exit {result.returncode}", flush=True)

    suites = len(commands)
    # The pane draws props only newer engines accept; point this at the
    # desktop app's bundled CLI when the PATH one lags behind it.
    cli = os.environ.get("CLAUDE_SQUARED_TEST_CLI") or shutil.which("claude")
    if cli:
        suites += 1
        print("\n=== pairs_pane_mod (local CLI; no model calls) ===", flush=True)
        with tempfile.TemporaryDirectory(prefix="cs-offline-mod-", dir=scratch, ignore_cleanup_errors=True) as tmp:
            env = {**os.environ, "CLAUDE_CONFIG_DIR": tmp, "CLAUDE_HOME": tmp,
                   "CODEX_HOME": str(Path(tmp) / "codex"), "TEMP": tmp, "TMP": tmp, "TMPDIR": tmp}
            try:
                for action in ("validate", "test"):
                    result = subprocess.run([cli, "plugin", action, str(ROOT / "mods" / "pairs-pane")],
                                            cwd=ROOT, env=env, timeout=120, check=False)
                    if result.returncode:
                        failures.append("pairs_pane_mod")
                        break
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append("pairs_pane_mod")
                print(f"FAIL pairs_pane_mod: {exc}", file=sys.stderr)
    else:
        print("SKIP pairs_pane_mod: claude is not on PATH (no model calls required).", flush=True)
    print(f"\nOffline suites: {suites - len(failures)} passed, {len(failures)} failed", flush=True)
    if failures:
        print("Failed: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
