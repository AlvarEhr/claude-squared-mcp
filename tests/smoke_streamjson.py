"""Second smoke test — exercises stream-json-based tools (compact, context, invoke,
adopt, agents). Slower than smoke.py because each command spawns a stream-json subprocess.
"""

from __future__ import annotations

import sys
import time
import traceback

from claude_squared import server  # noqa: E402


def banner(msg: str) -> None:
    print(f"\n========== {msg} ==========", flush=True)


def main() -> int:
    failures: list[str] = []
    name = f"sj-{int(time.time())}"

    banner(f"setup: pair_create (name={name})")
    try:
        result = server.pair_create(
            name=name,
            purpose="stream-json smoke",
            model="haiku",
            effort="low",
            permission_mode="auto",
            initial_message="Reply with: ok",
        )
        print(f"  session_id={result['session_id']}")
    except Exception as e:
        print(f"setup failed: {e}\n{traceback.format_exc(limit=4)}")
        return 1

    # Add some turns to give /context and /compact something to work with
    banner("warmup: a couple of pair_send to build context")
    try:
        server.pair_send(name=name, message="Remember tokens: ALPHA, BRAVO, CHARLIE.", timeout_seconds=120)
        server.pair_send(name=name, message="Also remember: cities = Oslo, Bergen, Trondheim.", timeout_seconds=120)
        print("  warmup ok")
    except Exception as e:
        failures.append(f"warmup: {e}")

    banner("1. pair_context")
    try:
        ctx = server.pair_context(name=name, timeout_seconds=60)
        print(f"  model: {ctx['model']}")
        print(f"  tokens: {ctx['tokens_used']}/{ctx['tokens_max']} ({ctx['percent']:.1f}%)")
        print(f"  raw_markdown[:200]: {ctx['raw_markdown'][:200]!r}")
        if ctx["tokens_max"] <= 0:
            failures.append(f"pair_context tokens_max invalid: {ctx['tokens_max']}")
        if "Tokens" not in ctx["raw_markdown"]:
            failures.append("pair_context raw_markdown missing 'Tokens' header")
    except Exception as e:
        failures.append(f"pair_context: {e}\n{traceback.format_exc(limit=3)}")

    banner("2. pair_actions (with name -> triggers slash_commands probe)")
    try:
        actions = server.pair_actions(name=name)
        print(f"  curated actions: {len(actions['actions'])}")
        skills = actions.get("pair_skills", [])
        print(f"  pair_skills: {len(skills)} -> {skills[:5]}...")
        agents = actions.get("pair_agents", [])
        print(f"  pair_agents: {agents[:5]}")
        if not skills:
            failures.append("pair_actions returned empty pair_skills")
    except Exception as e:
        failures.append(f"pair_actions: {e}\n{traceback.format_exc(limit=3)}")

    banner("3. pair_compact (with custom steering)")
    try:
        before = server.pair_context(name=name, timeout_seconds=60)
        compact = server.pair_compact(
            name=name,
            steering_prompt="Capture the tokens and city names verbatim. Skip everything else.",
            timeout_seconds=300,
        )
        print(f"  pre_tokens={compact['pre_tokens']}, post_tokens={compact['post_tokens']}, "
              f"trigger={compact['trigger']}, duration_ms={compact['duration_ms']}")
        if compact["post_tokens"] >= compact["pre_tokens"]:
            failures.append(f"compaction did not reduce tokens: {compact}")
        # Verify steered info survived
        recall = server.pair_send(
            name=name,
            message="What were the three tokens and three cities I told you? List them.",
            timeout_seconds=120,
        )
        print(f"  post-compact recall: {recall['response']!r}")
        if "ALPHA" not in recall["response"] or "Oslo" not in recall["response"]:
            failures.append(f"compact lost steered content: {recall['response']!r}")
    except Exception as e:
        failures.append(f"pair_compact: {e}\n{traceback.format_exc(limit=3)}")

    banner("4. pair_invoke (skill via stream-json)")
    try:
        # Try invoking 'context' skill — should produce some output
        # Some claude installs may not have a skill that's safe in this cwd; try /init which is universal
        invoked = server.pair_invoke(name=name, skill_name="context", timeout_seconds=60)
        # Either /context-as-skill works (rare) or we get a response indicating the skill ran
        print(f"  response[:200]: {invoked.get('response', '')[:200]!r}")
        # We accept any non-empty response as success
        if not invoked.get("response"):
            failures.append("pair_invoke produced no response")
    except Exception as e:
        # Don't hard-fail — skills are env-dependent
        print(f"  WARN pair_invoke: {e}")

    banner("5. pair_agent_define + pair_agent_list")
    try:
        agent_name = f"smoke-rev-{int(time.time())}"
        define = server.pair_agent_define(
            name=agent_name,
            description="Smoke test agent for pair MCP",
            prompt="You are a smoke-test agent. Reply only 'agent-ok'.",
            tools=["Read", "Bash"],
            model="haiku",
        )
        print(f"  defined at: {define['path']}")
        listing = server.pair_agent_list()
        names = [a.get("name") for a in listing["agents"]]
        if agent_name not in names:
            failures.append(f"agent_define did not produce listable agent: {names}")
        # Cleanup
        from claude_squared import agents as agents_mod
        agents_mod.delete_agent(agent_name)
        print(f"  cleaned up agent {agent_name}")
    except Exception as e:
        failures.append(f"pair_agent_*: {e}\n{traceback.format_exc(limit=3)}")

    banner("6. pair_adopt (register an existing session)")
    try:
        # We have a session from `name` above. Forget it without archive, then re-adopt by UUID.
        info = server.pair_info(name)
        sid = info["session_id"]
        cwd = info.get("cwd")
        server.pair_forget(name=name, archive=False)

        adopted_name = f"adopted-{int(time.time())}"
        adopted = server.pair_adopt(
            name=adopted_name,
            session_id=sid,
            purpose="adopted from smoke test",
            model="haiku",
            cwd=cwd,
        )
        print(f"  adopted as {adopted_name}, session_id={adopted['session_id']}")
        recall = server.pair_send(
            name=adopted_name,
            message="One word: do you remember Oslo? Reply 'yes' or 'no'.",
            timeout_seconds=60,
        )
        print(f"  adopted recall: {recall['response']!r}")
        if "yes" not in recall["response"].lower():
            failures.append(f"adopt didn't preserve session context: {recall['response']!r}")
        # Cleanup adopted entry
        server.pair_forget(name=adopted_name, archive=True)
    except Exception as e:
        failures.append(f"pair_adopt: {e}\n{traceback.format_exc(limit=3)}")

    banner("RESULTS")
    if failures:
        print(f"FAIL ({len(failures)} issue{'s' if len(failures) > 1 else ''}):")
        for f in failures:
            print(f"  - {f.splitlines()[0]}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
