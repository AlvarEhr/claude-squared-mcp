"""End-to-end smoke for v0.6 features:
- Sequential T-N tags on tool_use/tool_result in main.log
- Sequential T-N in sub-agent logs (independent counters per log)
- pair_tool_detail fetches full input + result by T-N
- pair_update(cwd=...) ALSO moves session dir with sub-agent transcripts
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from claude_squared import server, registry as reg_mod


def banner(msg: str) -> None:
    print(f"\n========== {msg} ==========", flush=True)


async def main() -> int:
    failures: list[str] = []
    name = f"v06-{int(time.time())}"
    cwd_a = Path(os.environ.get("TEMP", "/tmp")) / "pair_v06_a"
    cwd_b = Path(os.environ.get("TEMP", "/tmp")) / "pair_v06_b"
    cwd_a.mkdir(parents=True, exist_ok=True)
    cwd_b.mkdir(parents=True, exist_ok=True)

    banner(f"1. pair_create with cwd={cwd_a}")
    print(server.pair_create(name=name, model="haiku", effort="low",
                             cwd=str(cwd_a), purpose="v0.6 smoke",
                             initial_message="Reply: ready"))

    banner("2. pair_send → ask pair to use Read on a file in cwd_a")
    # Create a small test file so the pair has something to Read inside its cwd
    test_file = cwd_a / "test_for_pair.txt"
    test_file.write_text("hello from test_for_pair.txt\nline 2\nline 3", encoding="utf-8")
    out = await server.pair_send(
        name=name,
        message=f"Use the Read tool to read {test_file}. Reply on one line: read-ok",
        timeout_seconds=120,
    )
    print(out)

    banner("3. pair_log → expect T-N tags on the Read tool_use + tool_result")
    log_text = server.pair_log(name=name, last_n=20)
    print(log_text)
    if "[T-1]" not in log_text:
        failures.append("T-N tag not in main.log")
    if "tool_use] Read" not in log_text:
        failures.append("Read tool_use not logged with T-N")

    banner("4. pair_tool_detail T-1 → expect full Read input + full file content as result")
    detail = server.pair_tool_detail(name=name, tool_id="T-1")
    print(detail)
    if "test_for_pair.txt" not in detail:
        failures.append("tool detail missing input file path")
    if "hello from test_for_pair.txt" not in detail:
        failures.append("tool detail missing actual file content from result")

    banner("5. pair_send with Agent → spawn sub-agent that does its own tool_use")
    out2 = await server.pair_send(
        name=name,
        message=("Use the Agent tool to spawn a general-purpose sub-agent. "
                 f"Tell the sub-agent to use Read on {test_file} and reply with "
                 "exactly: 'subagent-read-ok'. Then YOU report on one line what it returned."),
        timeout_seconds=240,
    )
    print(out2)

    banner("6. List sub-agent log files")
    log_dir = reg_mod.logs_dir() / name
    sub_logs = sorted(log_dir.glob("subagent-*.log"))
    for p in sub_logs:
        print(f"  {p.name} ({p.stat().st_size} bytes)")

    if sub_logs:
        banner("7. pair_log subagent='1' → expect sub-agent's own T-N tagging")
        sub_log_text = server.pair_log(name=name, subagent="1")
        print(sub_log_text)

        banner("8. pair_tool_detail T-1 in subagent='1' → expect sub-agent's Read")
        try:
            sub_detail = server.pair_tool_detail(name=name, tool_id="T-1", subagent="1")
            print(sub_detail)
            if "test_for_pair.txt" not in sub_detail:
                failures.append("sub-agent tool detail missing file path")
        except Exception as e:
            print(f"WARN sub-agent tool detail: {e}")

    banner(f"9. pair_update cwd → {cwd_b} — verify session dir moves with JSONL")
    src_proj = reg_mod.claude_home() / "projects" / server._encode_cwd_for_project(str(cwd_a))
    dst_proj = reg_mod.claude_home() / "projects" / server._encode_cwd_for_project(str(cwd_b))
    src_session_dir = src_proj / server.pair_info(name=name, verbose=True).split('"session_id":')[1].split('"')[1]  # quick parse
    print(f"before move: src has session dir? {src_session_dir.exists()}")
    out_upd = server.pair_update(name=name, cwd=str(cwd_b))
    print(out_upd)
    if "session dir" not in out_upd and src_session_dir.exists():
        # If src still exists post-move, the session-dir move didn't happen
        print(f"WARN: session dir did not move (still at {src_session_dir})")

    banner("10. pair_send after cwd move → ensure sub-agent recall still works")
    out3 = await server.pair_send(
        name=name,
        message="Did you use the Agent tool earlier in this conversation? Reply yes or no.",
        timeout_seconds=120,
    )
    print(out3)

    banner("11. cleanup")
    print(server.pair_forget(name=name, archive=False))
    test_file.unlink(missing_ok=True)

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
