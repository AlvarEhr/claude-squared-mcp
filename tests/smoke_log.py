"""Verify log file is written + tail tool reports it."""

import asyncio
import sys
from pathlib import Path

from claude_squared import server


async def main() -> int:
    name = "logtest"
    print("--- pair_create (no explicit cwd; uses os.getcwd()) ---")
    print(server.pair_create(name=name, model="haiku", effort="low",
                             purpose="log smoke", initial_message="ok"))
    print()

    print("--- pair_send (warms runtime, writes to log) ---")
    out = await server.pair_send(name=name,
                                 message="Reply on one line: hello-from-log-test",
                                 timeout_seconds=120)
    print(out)
    print()

    print("--- pair_tail ---")
    print(server.pair_tail(name=name))
    print()

    log = Path.home() / ".claude" / "pairs" / "logs" / f"{name}.log"
    print(f"--- actual log file (size: {log.stat().st_size if log.exists() else 'MISSING'} bytes) ---")
    if log.exists():
        print(log.read_text(encoding="utf-8")[:3000])
    print()

    server.pair_forget(name=name, archive=False)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
