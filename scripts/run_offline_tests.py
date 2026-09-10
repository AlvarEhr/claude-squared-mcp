"""Run the maintained offline tests in isolated Python processes.

Run with ``python scripts/run_offline_tests.py`` from any working directory.
The explicit allow-list excludes smoke scripts that invoke live models.
"""

from __future__ import annotations

import os
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
    commands.append(("connectors", [
        "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_connectors.py", "-v",
    ]))
    failures = []
    for name, arguments in commands:
        print(f"\n=== {name} ===", flush=True)
        # Set isolation in the child environment BEFORE it imports the package:
        # server/async_tasks imports can otherwise touch real user state.
        with tempfile.TemporaryDirectory(prefix=f"cs-offline-{name}-", ignore_cleanup_errors=True) as tmp:
            env = os.environ.copy()
            env.update({
                "CLAUDE_HOME": str(Path(tmp) / "claude"),
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

    print(f"\nOffline suites: {len(commands) - len(failures)} passed, {len(failures)} failed", flush=True)
    if failures:
        print("Failed: " + ", ".join(failures), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
