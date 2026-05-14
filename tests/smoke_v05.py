"""End-to-end smoke for v0.5 features:
- Per-pair log folder
- Log line refs in pair_send footer
- pair_log slice fetch
- Sub-agent extraction (one-shot post-completion)
- pair_update(cwd=...) JSONL move
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from claude_squared import server, registry as reg_mod


def banner(msg: str) -> None:
    print(f"\n========== {msg} ==========", flush=True)


async def main() -> int:
    failures: list[str] = []
    name = f"v05-{int(time.time())}"

    # Pre-create test cwds
    cwd_a = Path(os.environ.get("TEMP", "/tmp")) / "pair_v05_a"
    cwd_b = Path(os.environ.get("TEMP", "/tmp")) / "pair_v05_b"
    cwd_a.mkdir(parents=True, exist_ok=True)
    cwd_b.mkdir(parents=True, exist_ok=True)

    banner(f"1. pair_create in {cwd_a}")
    r = server.pair_create(
        name=name, model="haiku", effort="low", cwd=str(cwd_a),
        purpose="v0.5 smoke", initial_message="Reply: hi-from-a",
    )
    print(r)

    banner("2. pair_send → expect log path + line refs in footer")
    out = await server.pair_send(name=name,
                                  message="Reply on one line: turn1",
                                  timeout_seconds=120)
    print(out)
    if "main.log:" not in out:
        failures.append("pair_send footer missing log line refs")

    banner("3. pair_tail → folder layout")
    print(server.pair_tail(name=name))
    log_dir = reg_mod.logs_dir() / name
    if not (log_dir / "main.log").exists():
        failures.append(f"main.log missing at {log_dir / 'main.log'}")

    banner("4. pair_log default (last_n=50) on main.log")
    print(server.pair_log(name=name, last_n=20))

    banner("5. pair_send a second turn so we have multiple turns logged")
    out2 = await server.pair_send(name=name,
                                   message="Reply on one line: turn2",
                                   timeout_seconds=120)
    print(out2)

    banner("6. pair_log with explicit start/end (use the line refs from a footer)")
    # Parse the footer's "main.log:X-Y" — bit hacky but verifies the reference works
    import re
    m = re.search(r"main\.log:(\d+)-(\d+)", out2)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        print(f"using lines {s}-{e}:")
        print(server.pair_log(name=name, start=s, end=e))
    else:
        failures.append("could not parse line refs from second send footer")

    banner("7. pair_send with Agent recursion → expect sub-agent log to be created post-completion")
    out3 = await server.pair_send(
        name=name,
        message=("Use the Agent tool to spawn a general-purpose sub-agent. "
                 "Have the sub-agent reply with exactly: 'subagent-saw-this'. "
                 "Then YOU report on one line what it returned."),
        timeout_seconds=240,
    )
    print(out3)
    sub_logs = sorted(log_dir.glob("subagent-*.log"))
    print(f"\nsub-agent log files in {log_dir}:")
    for p in sub_logs:
        print(f"  {p.name} ({p.stat().st_size} bytes)")
    if not sub_logs:
        failures.append("no sub-agent log created after Agent invocation")
    else:
        print("\n--- first sub-agent log content (first 1500 chars) ---")
        print(sub_logs[0].read_text(encoding="utf-8")[:1500])

    banner("8. pair_log subagent='1'")
    try:
        print(server.pair_log(name=name, subagent="1", last_n=20))
    except Exception as e:
        failures.append(f"pair_log subagent fetch failed: {e}")

    banner(f"9. pair_update cwd from {cwd_a} → {cwd_b}")
    src_jsonl_loc = reg_mod.claude_home() / "projects" / server._encode_cwd_for_project(str(cwd_a))
    dst_jsonl_loc = reg_mod.claude_home() / "projects" / server._encode_cwd_for_project(str(cwd_b))
    info_before = server.pair_info(name=name, verbose=True)
    print(f"before move:")
    print(f"  src dir contents: {[p.name for p in src_jsonl_loc.glob('*.jsonl')] if src_jsonl_loc.exists() else 'MISSING'}")
    print(f"  dst dir contents: {[p.name for p in dst_jsonl_loc.glob('*.jsonl')] if dst_jsonl_loc.exists() else 'MISSING'}")
    upd = server.pair_update(name=name, cwd=str(cwd_b))
    print(upd)
    print(f"after move:")
    print(f"  src dir contents: {[p.name for p in src_jsonl_loc.glob('*.jsonl')] if src_jsonl_loc.exists() else 'MISSING'}")
    print(f"  dst dir contents: {[p.name for p in dst_jsonl_loc.glob('*.jsonl')] if dst_jsonl_loc.exists() else 'MISSING'}")

    banner("10. pair_send after cwd move → must succeed (recall what you said before)")
    out4 = await server.pair_send(name=name,
                                   message="What were the two replies you gave earlier? Reply: turn1, turn2",
                                   timeout_seconds=120)
    print(out4)
    if "turn1" not in out4 and "turn2" not in out4:
        failures.append(f"recall after cwd move failed: response was {out4!r}")

    banner("11. pair_update cwd=same — should be no-op")
    print(server.pair_update(name=name, cwd=str(cwd_b)))

    banner("12. pair_forget cleanup")
    print(server.pair_forget(name=name, archive=False))

    banner("RESULTS")
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
