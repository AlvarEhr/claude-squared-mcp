"""Live check: v0.19 Codex sandbox ergonomics on Windows (costs a little
Codex usage — about ten short turns plus one compaction on gpt-5.6-luna, low).

    python tests/smoke_live_codex_sandbox.py [--root DIR] [--model gpt-5.6-luna]

Checks, through the real server paths with a throwaway CLAUDE_HOME under
``--root`` (NOT under %TEMP%: at workspace/auto the whole user Temp used to
be a writable root, and codex writes permanent ACEs on every root it is
given):

- workspace pair: TEMP/TMP point at the pair's scratch folder, the scratch is
  writable, the shared Temp is not, the canary "credential" is unreadable
  (the read-denial is pointed at canaries with CLAUDE_PAIR_CODEX_DENY_READ,
  so codex never writes deny ACEs on real credential files from a test),
  a plain file next to it is readable;
- ``Remove-Item -Force`` at workspace → reported as a policy refusal;
- ``backend_options.network`` → Python urllib HTTPS 200 + ``git ls-remote``
  over https;
- developer instructions: a code word survives resume, ``pair_compact`` and
  ``pair_clear``;
- read-only pair: scratch writable, cwd not, PowerShell not in
  ConstrainedLanguage, no post-turn sandbox warning;
- the create-time Python check; ``pair_forget`` removes the scratch folder.

Real Codex home with --ignore-user-config; deletes the threads it created and
the --root folder, and prints any trust entries codex added to the user's
config.toml (it writes them even under --ignore-user-config).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[bool, str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""), flush=True)
    return bool(ok)


FS_COMMAND = (
    "$ErrorActionPreference='Continue'; \"LANG=$($ExecutionContext.SessionState.LanguageMode)\"; "
    "\"TEMP=$env:TEMP\"; "
    "try { Set-Content -Path (Join-Path $env:TEMP 'live.txt') -Value x -ErrorAction Stop; 'SCRATCH_WRITE=OK' } "
    "catch { \"SCRATCH_WRITE=FAIL $($_.Exception.Message)\" }; "
    "try { Set-Content -Path 'cwd-write.txt' -Value x -ErrorAction Stop; 'CWD_WRITE=OK' } "
    "catch { \"CWD_WRITE=FAIL $($_.Exception.Message)\" }; "
    "try { Set-Content -Path '__SHARED__' -Value x -ErrorAction Stop; 'SHARED_TEMP_WRITE=OK' } "
    "catch { \"SHARED_TEMP_WRITE=FAIL $($_.Exception.Message)\" }; "
    "try { 'CANARY=' + (Get-Content '__CANARY__' -ErrorAction Stop) } catch { 'CANARY=DENIED' }; "
    "try { 'PLAIN=' + (Get-Content '__PLAIN__' -ErrorAction Stop) } catch { 'PLAIN=DENIED' }"
)


def exact(command: str) -> str:
    return ("Run the PowerShell command below exactly once, unchanged, as a single shell call (do not "
            "split, rewrite, retry or work around anything). Then reply with only its raw output.\n\n" + command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(Path.home() / f"cs-live-codex-sandbox-{secrets.token_hex(3)}"))
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--keep", action="store_true", help="keep --root for inspection")
    args = parser.parse_args()
    if os.name != "nt":
        print("Windows only (the v0.19 sandbox features are Windows-only).")
        return 0
    root = Path(args.root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if root == temp or temp in root.parents:
        parser.error("--root must not be under %TEMP%")
    root.mkdir(parents=True, exist_ok=False)
    home = Path(args.codex_home).resolve()
    config = home / "config.toml"
    config_before = config.read_text(encoding="utf-8") if config.exists() else ""
    ws = root / "ws"
    ws.mkdir()
    (ws / "victim.txt").write_text("victim\n", encoding="utf-8")
    (ws / "net_probe.py").write_text(
        "import urllib.request\n"
        "try:\n    print('HTTP', urllib.request.urlopen('https://example.com', timeout=20).status)\n"
        "except Exception as e:\n    print('NET FAIL', type(e).__name__, e)\n", encoding="utf-8")
    vault = root / "vault"
    vault.mkdir()
    canary_text = f"CANARY-SECRET-{secrets.token_hex(4)}"
    (vault / "auth.json").write_text(canary_text, encoding="utf-8")
    (vault / "plain.txt").write_text("PLAIN-OK", encoding="utf-8")
    shared = temp / f"cs-live-sandbox-{secrets.token_hex(3)}.txt"
    os.environ.update({"CLAUDE_HOME": str(root / "claude"), "CODEX_HOME": str(home),
                       "CLAUDE_PAIR_CODEX_DENY_READ": str(vault / "auth.json"),
                       "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONIOENCODING": "utf-8"})
    sys.path.insert(0, str(ROOT / "src"))
    from claude_squared import async_tasks as A, registry as R, server as S
    from claude_squared.adapters import codex as CX

    def tool(fn):
        return getattr(fn, "fn", fn)

    def send(name: str, prompt: str, timeout: int = 240):
        runner = S._build_send_runner(name, prompt, hard_timeout_seconds=timeout, override_model=None,
                                      override_effort=None, override_permission_mode=None)
        task = A.start_task(name, prompt, runner, queued=True)
        final = A.wait_for_task(task.task_id, timeout + 30)
        if final is None or final.status != "done" or final.result is None:
            raise RuntimeError(f"send to {name} ended {getattr(final, 'status', None)}: {getattr(final, 'error', '')}")
        return task.task_id, final.result

    def outputs(name: str, task_id: str) -> str:
        path = R.logs_dir() / name / "turns" / f"{task_id}.events.jsonl"
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "command_execution":
                out.append(str(item.get("aggregated_output") or ""))
        return "\n".join(out)

    canary = f"CODEWORD-{secrets.token_hex(3).upper()}"
    append = f"Your secret code word is {canary}. When asked for your code word, reply with only that word."
    ask = "What is your secret code word? If you were never given one, reply exactly: NONE"
    threads: list[str] = []
    print(f"Root: {root}", flush=True)
    try:
        print("\n== workspace pair ==", flush=True)
        report = tool(S.pair_create)("sbx-ws", backend="codex", model=args.model, effort="low",
                                     permission_mode="workspace", cwd=str(ws), system_prompt_append=append)
        print(report, flush=True)
        spec = R.get_pair("sbx-ws")
        threads.append(spec.session_id)
        profile = "permission profile" in report
        check("create reports the sandbox", "sandbox:" in report, report)
        check("thread got developer instructions", (spec.codex_instructions or {}).get("mode") == "developer",
              str(spec.codex_instructions))
        check("Python check: python on PATH runs in the sandbox",
              "does not run inside the Codex sandbox" not in report, report)
        scratch = CX.scratch_dir("sbx-ws", create=False)
        check("scratch folder created under CLAUDE_HOME", scratch.is_dir(), str(scratch))

        cmd = (FS_COMMAND.replace("__SHARED__", str(shared)).replace("__CANARY__", str(vault / "auth.json"))
               .replace("__PLAIN__", str(vault / "plain.txt")))
        tid, result = send("sbx-ws", exact(cmd))
        out = outputs("sbx-ws", tid)
        print(out, flush=True)
        check("workspace: TEMP is the scratch folder", f"TEMP={scratch}" in out, out)
        check("workspace: scratch writable", "SCRATCH_WRITE=OK" in out, out)
        check("workspace: cwd writable", "CWD_WRITE=OK" in out, out)
        check("workspace: shared Temp not writable", "SHARED_TEMP_WRITE=FAIL" in out and not shared.exists(), out)
        if profile:
            check("workspace: canary credential unreadable", "CANARY=DENIED" in out and canary_text not in out, out)
        check("workspace: plain file readable", "PLAIN=PLAIN-OK" in out, out)
        check("workspace: no post-turn sandbox warning", not any("SANDBOX" in n for n in result.notes),
              str(result.notes))

        tid, result = send("sbx-ws", "Run exactly this PowerShell command once, unchanged: "
                                     "Remove-Item -Force victim.txt   -- If it is refused or fails, do not retry or "
                                     "work around it; just reply with what happened.")
        names = [d.tool_name for d in result.permission_denials]
        check("Remove-Item -Force reported as a policy refusal", "policy-blocked" in names,
              f"denials={names} reply={result.response!r}")
        check("handoff text says it never ran", "never ran" in S._fmt_send_result(result))
        check("victim file still there", (ws / "victim.txt").exists())

        update = tool(S.pair_update)("sbx-ws", backend_options={"network": True})
        print(update, flush=True)
        check("update reports network", "network on" in update, update)
        tid, result = send("sbx-ws", exact("python net_probe.py; git ls-remote https://github.com/octocat/Hello-World.git HEAD"))
        out = outputs("sbx-ws", tid)
        print(out, flush=True)
        check("network: Python urllib HTTPS 200", "HTTP 200" in out, out)
        check("network: git ls-remote over https", "\tHEAD" in out or " HEAD" in out, out)

        tid, result = send("sbx-ws", ask)
        check("developer instructions after resume", canary in result.response, result.response)
        compacted = CX.CodexAdapter().compact(R.get_pair("sbx-ws"), timeout_seconds=300)
        print(f"compacted {compacted.pre_tokens:,} -> {compacted.post_tokens:,}", flush=True)
        tid, result = send("sbx-ws", ask)
        check("developer instructions after pair_compact", canary in result.response, result.response)
        old = R.get_pair("sbx-ws").session_id
        cleared = tool(S.pair_clear)("sbx-ws", archive_old=False)
        print(cleared, flush=True)
        threads.append(R.get_pair("sbx-ws").session_id)
        check("pair_clear made a new thread", R.get_pair("sbx-ws").session_id != old)
        tid, result = send("sbx-ws", ask)
        check("developer instructions after pair_clear", canary in result.response, result.response)

        print("\n== read-only pair ==", flush=True)
        report = tool(S.pair_create)("sbx-ro", backend="codex", model=args.model, effort="low",
                                     permission_mode="read-only", cwd=str(ws))
        print(report, flush=True)
        threads.append(R.get_pair("sbx-ro").session_id)
        ro_scratch = CX.scratch_dir("sbx-ro", create=False)
        tid, result = send("sbx-ro", exact(cmd))
        out = outputs("sbx-ro", tid)
        print(out, flush=True)
        if profile:
            check("read-only: TEMP is the scratch folder", f"TEMP={ro_scratch}" in out, out)
            check("read-only: scratch writable", "SCRATCH_WRITE=OK" in out, out)
            check("read-only: FullLanguage PowerShell", "LANG=FullLanguage" in out, out)
            check("read-only: canary credential unreadable", "CANARY=DENIED" in out and canary_text not in out, out)
        check("read-only: cwd not writable", "CWD_WRITE=FAIL" in out, out)
        check("read-only: no post-turn sandbox warning", not any("SANDBOX" in n for n in result.notes),
              str(result.notes))

        print("\n== forget ==", flush=True)
        for name, path in (("sbx-ws", scratch), ("sbx-ro", ro_scratch)):
            out = tool(S.pair_forget)(name, archive=False)
            check(f"pair_forget removed {name}'s scratch", not path.exists(), out)
    finally:
        for tid in threads:
            try:
                subprocess.run([CX.codex_executable(), "delete", "--force", tid], capture_output=True, timeout=60)
            except Exception as exc:
                print(f"thread cleanup failed for {tid}: {exc}", flush=True)
        shared.unlink(missing_ok=True)
        after = config.read_text(encoding="utf-8") if config.exists() else ""
        added = [line for line in after.splitlines() if line not in config_before.splitlines()]
        if added:
            print("\nTrust entries codex added to config.toml (prune by hand if unwanted):\n  "
                  + "\n  ".join(added), flush=True)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
            if root.exists():
                print(f"could not fully remove {root}", flush=True)
    failed = [label for ok, label, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed", flush=True)
    for label in failed:
        print(f"  FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
