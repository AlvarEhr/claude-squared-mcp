"""End-to-end smoke test — bypasses the MCP transport, calls tool functions directly.

Run: python tests/smoke.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback

# Note: we DON'T sandbox via CLAUDE_HOME because the claude CLI itself writes session
# JSONLs to the real ~/.claude/projects/ regardless. We use a uniquely-named pair so
# the real registry gets one extra entry that pair_forget cleans up.

from claude_squared import server  # noqa: E402
from claude_squared import registry as reg_mod  # noqa: E402


def banner(msg: str) -> None:
    print(f"\n========== {msg} ==========", flush=True)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"


def main() -> int:
    failures: list[str] = []
    name = f"smoke-{int(time.time())}"  # unique to avoid stomping prior test

    banner(f"1. pair_create (name={name})")
    result, err = _safe(
        server.pair_create,
        name=name,
        purpose="smoke test",
        model="haiku",
        effort="low",
        permission_mode="auto",
        initial_message="Reply with exactly: smoke-init-ok",
    )
    if err:
        print(f"ERROR: {err}", flush=True)
        failures.append(f"pair_create: {err.splitlines()[0]}")
        return 1
    print(f"  session_id={result['session_id']}")
    print(f"  initial_response={result.get('initial_response')!r}")
    if "smoke-init-ok" not in (result.get("initial_response") or ""):
        failures.append(f"unexpected initial_response: {result.get('initial_response')!r}")
    session_id = result["session_id"]

    banner("2. pair_list")
    listing = server.pair_list()
    print(listing)
    if not any(p["name"] == name for p in listing["pairs"]):
        failures.append("pair_list did not include new pair")

    banner("3. pair_info")
    info = server.pair_info(name)
    print(info)
    if info["session_id"] != session_id:
        failures.append(f"pair_info session_id mismatch: {info['session_id']} vs {session_id}")
    if not info["transcript_exists"]:
        failures.append(f"transcript not found at {info.get('transcript_path')}")

    banner("4. pair_send (memory + context tracking)")
    try:
        resp = server.pair_send(
            name=name,
            message="Remember: secret-marker=BANANA-42. Then reply with just: ack",
            timeout_seconds=120,
        )
        print({k: v for k, v in resp.items() if k != "response"}, "\nresponse:", resp["response"])
        assert resp.get("context"), "no context block in response"
        ctx = resp["context"]
        print(f"   context: {ctx['tokens_used']}/{ctx['tokens_max']} = {ctx['percent']:.1f}%")
        assert ctx["tokens_max"] > 0, "tokens_max should be > 0"
    except Exception as e:
        failures.append(f"pair_send: {e}")

    banner("5. pair_send (recall test - verifies context persistence)")
    try:
        resp2 = server.pair_send(
            name=name,
            message="What was the secret-marker I told you? Reply with just the value.",
            timeout_seconds=120,
        )
        print("response:", resp2["response"])
        if "BANANA-42" not in resp2["response"]:
            failures.append(f"recall failed: response was {resp2['response']!r}")
    except Exception as e:
        failures.append(f"pair_send recall: {e}")

    banner("6. pair_transcript")
    try:
        t = server.pair_transcript(name, last_n=5)
        print(f"  turns: {len(t['turns'])}")
        for turn in t["turns"]:
            print(f"   [{turn['role']}] {turn['content'][:80]}")
        if len(t["turns"]) < 2:
            failures.append(f"expected ≥2 turns, got {len(t['turns'])}")
    except Exception as e:
        failures.append(f"pair_transcript: {e}")

    banner("7. pair_update")
    try:
        updated = server.pair_update(name=name, purpose="updated-purpose-marker")
        if updated["purpose"] != "updated-purpose-marker":
            failures.append(f"pair_update purpose mismatch")
        print("  purpose updated ok")
    except Exception as e:
        failures.append(f"pair_update: {e}")

    banner("8. pair_send_async + pair_poll")
    try:
        task = server.pair_send_async(
            name=name,
            message="Reply with exactly: async-ok",
            timeout_seconds=120,
        )
        task_id = task["task_id"]
        print(f"  task_id: {task_id}, status: {task['status']}")
        for i in range(60):
            poll = server.pair_poll(task_id)
            if poll["status"] != "running":
                break
            time.sleep(2)
        print(f"  final status: {poll['status']}")
        if poll["status"] != "done":
            failures.append(f"async send did not complete: {poll}")
        elif "async-ok" not in (poll.get("result") or {}).get("response", ""):
            failures.append(f"async response unexpected: {poll.get('result', {}).get('response')!r}")
    except Exception as e:
        failures.append(f"pair_send_async: {e}")

    banner("9. pair_clear")
    try:
        before_sid = server.pair_info(name)["session_id"]
        cleared = server.pair_clear(name)
        print(cleared)
        after_sid = server.pair_info(name)["session_id"]
        if before_sid == after_sid:
            failures.append("pair_clear didn't rotate session_id")
        # Recall test should now FAIL since context was cleared
        recall = server.pair_send(
            name=name,
            message="What was the secret marker I told you earlier? Reply 'BANANA-42' or 'no-marker' if you don't have it.",
            timeout_seconds=60,
        )
        print("  post-clear recall:", recall["response"])
        if "BANANA-42" in recall["response"]:
            failures.append("pair_clear did NOT actually clear context (banana marker survived)")
    except Exception as e:
        failures.append(f"pair_clear: {e}")

    banner("10. pair_forget")
    try:
        forget = server.pair_forget(name=name, archive=True)
        print(forget)
        listing_after = server.pair_list()
        if any(p["name"] == name for p in listing_after["pairs"]):
            failures.append("pair_forget did not remove from registry")
    except Exception as e:
        failures.append(f"pair_forget: {e}")

    banner("RESULTS")
    if failures:
        print(f"FAIL ({len(failures)} issue{'s' if len(failures) > 1 else ''}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
