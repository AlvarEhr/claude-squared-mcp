"""Smoke test for the persistent runtime path.

Measures wall-clock for a sequence of sends. First send pays the resume cost; subsequent
sends should be ~2-3s faster because the subprocess stays alive.
"""

from __future__ import annotations

import sys
import time
import traceback

from claude_squared import server, runtime as runtime_mod


def t(label: str, fn, *args, **kw):
    t0 = time.monotonic()
    try:
        out = fn(*args, **kw)
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"[{elapsed:5.2f}s] {label}: ERROR {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        return None, elapsed
    elapsed = time.monotonic() - t0
    short = (str(out) or "")[:100].replace("\n", " | ")
    print(f"[{elapsed:5.2f}s] {label}: {short}")
    return out, elapsed


def main() -> int:
    name = f"rt-{int(time.time())}"
    print(f"=== smoke runtime test (pair={name}) ===\n")

    t("pair_create", server.pair_create, name=name, model="haiku", effort="low",
      cwd="C:\\tmp", purpose="runtime perf test", initial_message="ok")

    print()
    print("--- 4 simple sends (first pays resume cost, rest reuse runtime) ---")
    times = []
    for i in range(4):
        _, dt = t(f"pair_send #{i+1}", server.pair_send, name=name,
                  message=f"Reply: {chr(ord('a') + i)}", timeout_seconds=120)
        times.append(dt)

    print()
    t("pair_runtimes", server.pair_runtimes)

    print()
    print("--- toggle persistent + verify ---")
    t("pair_persist on", server.pair_persist, name=name, on=True)
    t("pair_runtimes", server.pair_runtimes)

    print()
    print("--- explicit eviction via pair_update model change ---")
    # This evicts the runtime because model is in the eviction-trigger set
    # (we update back to same model, but the eviction fires on any update of that field)
    t("pair_update (force evict via model)", server.pair_update, name=name, model="haiku")
    t("pair_runtimes (should be empty)", server.pair_runtimes)

    print()
    print("--- one more send to confirm re-spawn works ---")
    _, dt5 = t("pair_send #5 (cold again)", server.pair_send, name=name,
               message="Reply: e", timeout_seconds=120)
    times.append(dt5)

    print()
    print("--- cleanup ---")
    t("pair_forget", server.pair_forget, name=name, archive=False)

    print()
    print("=== summary ===")
    print(f"  send #1 (cold, first runtime spawn):  {times[0]:5.2f}s")
    for i in range(1, 4):
        print(f"  send #{i+1} (warm runtime):              {times[i]:5.2f}s")
    print(f"  send #5 (cold again after eviction):  {times[4]:5.2f}s")
    cold_avg = (times[0] + times[4]) / 2
    warm_avg = sum(times[1:4]) / 3
    print(f"  cold avg = {cold_avg:.2f}s, warm avg = {warm_avg:.2f}s, "
          f"speedup = {(cold_avg - warm_avg):.2f}s ({(1 - warm_avg/cold_avg)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
