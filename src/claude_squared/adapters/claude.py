"""ClaudeAdapter: wraps the `claude` CLI for create / send / compact / context / invoke_skill."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from claude_squared.adapters.base import PairAdapter
from claude_squared.cli_paths import encode_cwd_for_project as _encode_cwd_for_project
from claude_squared.errors import CLIError, CommandTimeout, SessionMissing
from claude_squared.models import (
    CompactResult,
    ContextReport,
    ContextStatus,
    CreateResult,
    PairSpec,
    PermissionDenial,
    SendResult,
)
from claude_squared.registry import claude_home, profiles_dir


WARNING_THRESHOLD = 0.60
STRONG_WARNING_THRESHOLD = 0.85
# _encode_cwd_for_project: kept as a local alias for backward compat within this
# module (multiple call sites). Source of truth: cli_paths.encode_cwd_for_project.


def _claude_executable() -> str:
    """Locate the claude CLI."""
    env = os.environ.get("CLAUDE_PAIR_CLI_PATH")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "claude"


class ClaudeAdapter(PairAdapter):
    backend_name = "claude"

    # ---- public surface ------------------------------------------------

    def create(self, spec: PairSpec, initial_message: str | None = None) -> CreateResult:
        """Create a new session with pinned config; optionally send the first message.

        Pinned at create (verified to persist): --strict-mcp-config / --mcp-config,
        --append-system-prompt, --allowed-tools, --add-dir (cwd).
        """
        if not spec.session_id:
            spec.session_id = str(uuid.uuid4())

        args = self._common_create_args(spec)
        # Use --print + initial message (or a no-op probe) to actually create the session on disk.
        prompt = initial_message or "Reply with exactly: pair-ready"
        args += ["--print", "--output-format", "json",
                 "--session-id", spec.session_id,
                 "--model", spec.model,
                 "--permission-mode", spec.permission_mode,
                 "-p", prompt]

        result_json = self._run_print(args, timeout_seconds=300, pair_name=spec.name, cwd=spec.cwd)
        return CreateResult(
            name=spec.name,
            session_id=result_json.get("session_id", spec.session_id),
            transcript_path=str(self.transcript_path(spec)) if self.transcript_path(spec) else None,
            initial_response=result_json.get("result"),
        )

    def send(self, spec: PairSpec, message: str, *, model: str | None = None,
             effort: str | None = None, permission_mode: str | None = None,
             timeout_seconds: int = 300,
             on_event: "callable | None" = None) -> SendResult:
        if not self.session_exists(spec):
            raise SessionMissing(spec.name, spec.session_id)

        # If the caller passed any per-call overrides, fall back to one-shot subprocess
        # (the persistent runtime fixes model/effort/permission_mode at process start).
        # Otherwise use the long-running runtime for the ~3s startup speedup.
        if model is None and effort is None and permission_mode is None:
            from claude_squared.runtime import registry as _runtime_registry
            reg = _runtime_registry()
            rt = reg.get_or_start(spec, self)
            # Cross-process correctness: another MCP subprocess may have written
            # to the session JSONL since we last touched it. A warm runtime holds
            # stale in-memory session state — re-resume it from the up-to-date file.
            # Caller must hold the cross-process pair lock when invoking this path,
            # otherwise the stale-check itself races.
            if rt.is_stale():
                reg.evict(spec.name)
                rt = reg.get_or_start(spec, self)
            result_json = rt.send(message, timeout_seconds=timeout_seconds, on_event=on_event)
            return self._build_send_result(spec, result_json)

        # Resolve the effective effort for this one-shot send: per-call override
        # wins, else fall back to the pinned spec value. Pass --effort only if
        # the resolved value is non-None (haiku has no effort knob).
        eff = effort if effort is not None else spec.effort
        args = [
            "--print", "--output-format", "json",
            "--resume", spec.session_id,
            "--model", model or spec.model,
        ]
        if eff is not None:
            args += ["--effort", eff]
        args += [
            "--permission-mode", permission_mode or spec.permission_mode,
            "-p", message,
        ]
        result_json = self._run_print(args, timeout_seconds=timeout_seconds,
                                      pair_name=spec.name, cwd=spec.cwd)
        return self._build_send_result(spec, result_json)

    def compact(self, spec: PairSpec, steering_prompt: str | None = None,
                timeout_seconds: int = 600) -> CompactResult:
        """Run /compact natively via stream-json. Returns pre/post token counts."""
        cmd = "/compact" if not steering_prompt else f"/compact {steering_prompt}"
        events = self._run_stream_json(spec, [cmd], timeout_seconds=timeout_seconds)

        for ev in events:
            if ev.get("subtype") == "compact_boundary":
                meta = ev.get("compact_metadata", {})
                return CompactResult(
                    name=spec.name,
                    session_id=spec.session_id,
                    pre_tokens=meta.get("pre_tokens", 0),
                    post_tokens=meta.get("post_tokens", 0),
                    duration_ms=meta.get("duration_ms", 0),
                    trigger=meta.get("trigger", "manual"),
                )
        raise CLIError("Compaction did not produce a compact_boundary event")

    def context(self, spec: PairSpec, timeout_seconds: int = 60) -> ContextReport:
        """Invoke /context and parse the markdown response."""
        events = self._run_stream_json(spec, ["/context"], timeout_seconds=timeout_seconds)
        result_text = ""
        model = spec.model
        for ev in events:
            if ev.get("type") == "result" and ev.get("subtype") == "success":
                result_text = ev.get("result", "") or result_text
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                model = ev.get("model", model)
        if not result_text:
            raise CLIError("/context returned no result text")

        used, mx, pct = _parse_context_markdown(result_text)
        return ContextReport(
            name=spec.name,
            session_id=spec.session_id,
            model=model,
            tokens_used=used,
            tokens_max=mx,
            percent=pct,
            raw_markdown=result_text,
        )

    def invoke_skill(self, spec: PairSpec, skill_name: str, args: str | None = None,
                     timeout_seconds: int = 300) -> SendResult:
        """Invoke a skill via /skill-name (works in stream-json)."""
        cmd = f"/{skill_name}"
        if args:
            cmd += f" {args}"
        events = self._run_stream_json(spec, [cmd], timeout_seconds=timeout_seconds)
        # Find the last result event
        result_event = None
        for ev in reversed(events):
            if ev.get("type") == "result":
                result_event = ev
                break
        if not result_event:
            raise CLIError(f"Skill '/{skill_name}' produced no result event")
        return self._build_send_result(spec, result_event)

    def transcript_path(self, spec: PairSpec) -> Path | None:
        cwd = Path(spec.cwd) if spec.cwd else Path.cwd()
        encoded = _encode_cwd_for_project(cwd)
        return claude_home() / "projects" / encoded / f"{spec.session_id}.jsonl"

    def session_exists(self, spec: PairSpec) -> bool:
        p = self.transcript_path(spec)
        return p is not None and p.exists()

    # ---- internals -----------------------------------------------------

    def _common_create_args(self, spec: PairSpec) -> list[str]:
        """Args that pin specialization at create time (verified to persist on resume)."""
        args: list[str] = []

        # MCP scope: --strict-mcp-config drops local MCPs; cloud MCPs (mcp__claude_ai_*)
        # and our own pair MCP tools are loaded server-side or via user config and need
        # explicit --disallowed-tools globs to keep their definitions out of the pair's prompt.
        mcp_config = self._mcp_config_json(spec)
        args += ["--strict-mcp-config", "--mcp-config", mcp_config]
        for d in self._cloud_mcp_disallow(spec):
            args += ["--disallowed-tools", d]
        # Pairs don't need to be able to spawn pairs themselves — strip our own tools
        # (~12k tokens of definitions) from the pair's prompt by disallowing both
        # CLI-side and Desktop-side namespaces.
        args += ["--disallowed-tools", "mcp__pair__*",
                 "--disallowed-tools", "mcp__Claude_Squared__*"]

        # Workspace dirs → --add-dir (the spawned subprocess's cwd defines the workspace
        # root; --add-dir whitelists additional paths for the auto-mode classifier).
        if spec.cwd:
            args += ["--add-dir", spec.cwd]
        for d in spec.extra_dirs or []:
            args += ["--add-dir", d]

        # System prompt: profile file overrides inline append; both can be combined if profile is full system prompt
        sp_text = self._resolve_system_prompt(spec)
        if sp_text:
            args += ["--append-system-prompt", sp_text]

        if spec.allowed_tools:
            args += ["--allowed-tools", " ".join(spec.allowed_tools)]

        return args

    def _cloud_mcp_disallow(self, spec: PairSpec) -> list[str]:
        """Build --disallowed-tools globs for cloud MCPs (claude.ai-managed integrations).

        Default: suppress everything under `mcp__claude_ai_*` to keep pair context lean.
        If `mcp_whitelist` includes a cloud server name like 'claude_ai_Gmail', that one
        is left enabled.
        """
        # Always suppress everything under claude_ai_* unless explicitly whitelisted
        if not spec.mcp_whitelist:
            return ["mcp__claude_ai_*"]
        # Collect server names that should stay; suppress all others under claude_ai_*
        # (Note: we can't enumerate all cloud servers ahead of time, so this is a heuristic:
        # we only suppress the well-known ones that aren't in the whitelist.)
        known_cloud_servers = [
            "claude_ai_Canva", "claude_ai_Figma", "claude_ai_Gmail",
            "claude_ai_Google_Calendar", "claude_ai_Google_Drive",
            "claude_ai_Hugging_Face", "claude_ai_Notion", "claude_ai_PitchBook_Premium",
        ]
        whitelisted = set(spec.mcp_whitelist)
        return [f"mcp__{srv}__*" for srv in known_cloud_servers if srv not in whitelisted]

    def _mcp_config_json(self, spec: PairSpec) -> str:
        """Build the --mcp-config JSON string. Empty by default; opt-in via mcp_whitelist."""
        if not spec.mcp_whitelist:
            return json.dumps({"mcpServers": {}})
        # If whitelist given, the caller is asking us to read the user's normal MCP config
        # and pass through only the named ones. Without --strict-mcp-config we'd inherit all;
        # with it we'd see only what we list. Since we can't easily read the user's MCP defs from
        # here, we register just the named ones as a reference; user must have them in their global
        # MCP config for the names to resolve.
        # Simpler: drop --strict-mcp-config when whitelist given so the user's MCPs are inherited.
        return json.dumps({"mcpServers": {}})

    def _resolve_system_prompt(self, spec: PairSpec) -> str | None:
        parts = []
        if spec.profile_name:
            profile_path = profiles_dir() / f"{spec.profile_name}.md"
            if profile_path.exists():
                parts.append(profile_path.read_text(encoding="utf-8").strip())
        if spec.system_prompt_append:
            parts.append(spec.system_prompt_append.strip())
        return "\n\n".join(parts) if parts else None

    def _run_print(self, args: list[str], *, timeout_seconds: int, pair_name: str,
                   cwd: str | None = None) -> dict:
        cli = _claude_executable()
        full = [cli] + args
        try:
            proc = subprocess.run(
                full,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise CommandTimeout(pair_name, timeout_seconds) from e

        if proc.returncode != 0:
            raise CLIError(
                f"claude CLI exited non-zero",
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode,
            )
        try:
            return json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise CLIError(
                f"could not parse claude CLI JSON output: {e}",
                stderr=proc.stdout.decode("utf-8", errors="replace")[:2000],
                exit_code=proc.returncode,
            )

    def _run_stream_json(self, spec: PairSpec, messages: list[str], *,
                         timeout_seconds: int) -> list[dict]:
        """Run a stream-json subprocess that pushes the given user messages in order."""
        cli = _claude_executable()
        args = [cli, "--print", "--verbose",
                "--resume", spec.session_id,
                "--model", spec.model,
                "--permission-mode", spec.permission_mode,
                "--input-format", "stream-json",
                "--output-format", "stream-json"]

        stdin_payload = "\n".join(
            json.dumps({"type": "user", "message": {"role": "user", "content": m}})
            for m in messages
        ) + "\n"

        try:
            proc = subprocess.run(
                args,
                input=stdin_payload.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                cwd=spec.cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise CommandTimeout(spec.name, timeout_seconds) from e

        if proc.returncode != 0:
            raise CLIError(
                "claude CLI (stream-json) exited non-zero",
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode,
            )

        events: list[dict] = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _read_last_turn_context_fill(self, spec: PairSpec) -> int | None:
        """Compute true 'context fill' by reading the LAST assistant message's
        ``usage`` field from the session JSONL.

        Why: the stream-json ``result`` event's usage sums every sub-call within
        a multi-step agentic turn (thinking → tool_use → tool_result → thinking …),
        so a 3-step turn at ~40k each reports 120k cumulatively. That's correct
        for billing but wrong as a "% of context window used" indicator — the
        prompt never actually exceeded 40k.

        Returns ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``
        for the last assistant message that has a usage block, or ``None`` if the
        JSONL is missing or has no parseable usage (e.g. brand-new session).
        Caller falls back to the result-event sum on None.
        """
        try:
            p = self.transcript_path(spec)
        except Exception:
            return None
        if not p or not p.exists():
            return None
        last_total: int | None = None
        try:
            with open(p, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    if ev.get("type") != "assistant":
                        continue
                    u = (ev.get("message") or {}).get("usage")
                    if not isinstance(u, dict):
                        continue
                    last_total = (u.get("input_tokens", 0)
                                  + u.get("cache_creation_input_tokens", 0)
                                  + u.get("cache_read_input_tokens", 0))
        except Exception:
            return None
        return last_total

    def _build_send_result(self, spec: PairSpec, result_json: dict) -> SendResult:
        usage = result_json.get("usage", {}) or {}
        denials = [PermissionDenial(**d) for d in result_json.get("permission_denials", []) or []]
        model_usage = result_json.get("modelUsage", {}) or {}
        # Pick the first/main model used (order in dict is insertion order)
        model_used = next(iter(model_usage.keys()), spec.model)
        ctx_window = model_usage.get(model_used, {}).get("contextWindow", 200_000)
        # True context fill = the LAST assistant sub-call's prompt size, NOT the
        # cumulative across the agentic loop. The stream-json `result` event's
        # usage block sums every sub-call's input — useful for billing but
        # misleading as "context fill" (a turn with 3 tool_uses sums to 3× the
        # actual context size). Read the last assistant block's usage from the
        # JSONL for the accurate "how close to overflow" number.
        true_fill = self._read_last_turn_context_fill(spec)
        if true_fill is not None:
            used = true_fill
        else:
            used = (usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
        pct = (used / ctx_window * 100) if ctx_window else 0.0
        warning = None
        if pct >= STRONG_WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(ctx_window)} ({pct:.0f}%). "
                       f"STRONGLY consider pair_compact('{spec.name}') — context near limit.")
        elif pct >= WARNING_THRESHOLD * 100:
            warning = (f"{_fmt_tokens(used)}/{_fmt_tokens(ctx_window)} ({pct:.0f}%). "
                       f"Consider pair_compact('{spec.name}') to free context. "
                       f"Optionally pass a custom steering prompt focused on conversation "
                       f"arc + binding rules + in-flight state.")

        scope = result_json.get("_log_scope") or {}

        return SendResult(
            name=spec.name,
            response=result_json.get("result", ""),
            session_id=result_json.get("session_id", spec.session_id),
            model_used=model_used,
            cost_usd=result_json.get("total_cost_usd", 0.0),
            duration_ms=result_json.get("duration_ms", 0),
            permission_denials=denials,
            context=ContextStatus(tokens_used=used, tokens_max=ctx_window, percent=pct, warning=warning),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            log_path=scope.get("log_path"),
            log_line_start=scope.get("start_line"),
            log_line_end=scope.get("end_line"),
            subagent_logs=scope.get("subagent_logs") or [],
        )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


CONTEXT_TOKEN_RE = re.compile(r"\*\*Tokens:\*\*\s*([\d.]+)([kKmM]?)\s*/\s*([\d.]+)([kKmM]?)\s*\((\d+)%\)")


def _parse_context_markdown(text: str) -> tuple[int, int, float]:
    """Parse '**Tokens:** 36.6k / 200k (18%)' from /context output."""
    m = CONTEXT_TOKEN_RE.search(text)
    if not m:
        return (0, 200_000, 0.0)
    used = _parse_number(m.group(1), m.group(2))
    mx = _parse_number(m.group(3), m.group(4))
    pct = float(m.group(5))
    return (used, mx, pct)


def _parse_number(num_str: str, suffix: str) -> int:
    n = float(num_str)
    if suffix.lower() == "k":
        n *= 1_000
    elif suffix.lower() == "m":
        n *= 1_000_000
    return int(n)
