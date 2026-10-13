"""ClaudeAdapter: wraps the `claude` CLI for create / send / compact / context / invoke_skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from claude_squared.adapters.base import PairAdapter
from claude_squared import connectors
from claude_squared.cli_paths import encode_cwd_for_project as _encode_cwd_for_project
from claude_squared.errors import BillingError, CLIError, CommandTimeout, PairError, SessionMissing
from claude_squared.models import (
    CLAUDE_PERMISSION_MAP,
    HEADLESS_INCOMPATIBLE_TOOLS,
    CompactResult,
    ContextReport,
    ContextStatus,
    CreateResult,
    PairSpec,
    PermissionDenial,
    SendResult,
    model_substitution_note,
    normalize_model_id,
    normalize_permission,
    parse_model_id,
    split_model_tier,
)
from claude_squared.registry import claude_home, pairs_dir, profiles_dir


WARNING_THRESHOLD = 0.60
STRONG_WARNING_THRESHOLD = 0.85
# v0.11.0: the CLI's exact phrasing when a model is pulled / access lost. The
# turn emits this both as stderr (on the non-stream --output-format json path,
# with exit 1 AND a valid result JSON on STDOUT carrying is_error/api_error 404)
# and as the result event's `result` text (on the stream-json runtime path,
# is_error + api_error_status 404, modelUsage empty). Single source of truth so
# the create path (_run_print) and the send path (_build_send_result) detect it
# identically. Verified against CLI 2.1.170 the day Fable 5 was pulled.
MODEL_UNAVAILABLE_MARKER = "issue with the selected model"
_MODEL_UNAVAILABLE_HELP = (
    "the selected model is unavailable — it may have been pulled, or you may have "
    "lost access to it (e.g. a subscription/trial-flagged model). Switch the pair "
    "with pair_update(name, model=\"<available-model>\"), or set a fallback via "
    "pair_update(name, fallback_model=\"<model>\") so future sends auto-fall-back."
)
# _encode_cwd_for_project: kept as a local alias for backward compat within this
# module (multiple call sites). Source of truth: cli_paths.encode_cwd_for_project.

# v0.17.0: flags a raw ``backend_options.args`` entry may not carry. Principle:
# anything that loosens a pair must stay visible in its permission_mode /
# allowed_tools / extra_dirs / mcp_whitelist, and nothing may break the spawn.
# (1) Flags the pair sets from its own fields, or that a field covers — a
# second ``--settings`` would replace the merged JSON, not add to it; the
# tool set stays visible in disallowed_tools. Value = the field to use instead.
_MANAGED_FLAGS: dict[str, str] = {
    "--settings": "backend_options.settings",
    "--disallowed-tools": "disallowed_tools", "--disallowedTools": "disallowed_tools",
    "--allowed-tools": "allowed_tools", "--allowedTools": "allowed_tools",
    "--model": "model", "--fallback-model": "fallback_model", "--effort": "effort",
    "--permission-mode": "permission_mode",
    "--dangerously-skip-permissions": "permission_mode",
    "--allow-dangerously-skip-permissions": "permission_mode",
    "--append-system-prompt": "system_prompt_append / profile_name",
    "--append-system-prompt-file": "system_prompt_append / profile_name",
    "--mcp-config": "mcp_whitelist", "--strict-mcp-config": "mcp_whitelist",
    "--add-dir": "extra_dirs",
    "--inherit-permission-mode": "permission_mode", "--enable-auto-mode": "permission_mode",
    "--tools": "disallowed_tools",
}
# (2) Flags refused outright. Value = why. Flag list read from ``claude
# --help`` on CLI 2.1.295 (2026-10-09), then every option the root command
# defines in the 2.1.295 binary, hidden ones included (second review of
# b07509e found --managed-settings and --plugin-dir-no-mcp there). The
# hidden flags' reasons come from their own descriptions, not from probes.
# The third review added --chrome and --disable-slash-commands, plus the
# flags the CLI acts on BEFORE its option parser (--routine with --bg;
# --exec, -m, --handle-uri), which a sweep of option definitions can't see.
_PROTOCOL = "every spawn path sets the stream protocol the runtime reads itself"
_STREAM = "it adds to or reshapes the messages the runtime reads"
_OTHER_SESSION = "it starts or attaches a different kind of session than the pair's own"
_DISPATCH = "it hands the command line to another session instead of starting the pair's"
_PLUGINS = "plugins bring MCP servers, hooks and agents past the mcp_whitelist opt-in"
_AGENT = "an agent definition can carry its own permissionMode and tools"
_MOVED = "the session would move to another directory, where the pair can't find its transcript"
_TRUNCATED = "it cuts the resumed session short"
_TEAMMATE = "it makes the pair a team member whose permission requests go to a team lead"
_REFUSED_FLAGS: dict[str, str] = {
    "--print": _PROTOCOL, "-p": _PROTOCOL, "--resume": _PROTOCOL, "-r": _PROTOCOL,
    "--continue": _PROTOCOL, "-c": _PROTOCOL, "--session-id": _PROTOCOL,
    "--fork-session": _PROTOCOL, "--input-format": _PROTOCOL, "--output-format": _PROTOCOL,
    "--replay-user-messages": _PROTOCOL, "--include-partial-messages": _PROTOCOL,
    "--forward-subagent-text": _PROTOCOL, "--include-hook-events": _PROTOCOL,
    "--sdk-url": _PROTOCOL, "--await-claim": "it waits for a host's claim the pair never sends",
    "--await-initialize": "it waits for an initialize request the pair never sends",
    "--session-mirror": _STREAM, "--json-schema": _STREAM, "--enable-auth-status": _STREAM,
    "--prompt-suggestions": _STREAM,
    "--setting-sources": "it would stop loading your settings files, and with them their deny "
                         "rules and hooks",
    "--restricted": "it ignores your settings files, and with them their deny rules and hooks",
    "--safe-mode": "it switches off hooks, CLAUDE.md and other customizations",
    "--bare": "it skips keychain auth (a subscription login can't start) and your hooks",
    "--managed-settings": "it is a second settings channel (policy tier) that nothing here checks",
    "--client-data-url": "it loads a remote configuration document that nothing here checks",
    "--project-config-root": "it reads another directory's project settings, .mcp.json and agents",
    "--permission-prompt-tool": "approvals are the pair's permission_mode (stdio hangs a headless "
                                "pair; an MCP tool would take approvals over)",
    "--permission-prompts": "approvals are the pair's permission_mode",
    "--plugin-dir": _PLUGINS, "--plugin-url": _PLUGINS,
    "--plugin-dir-no-mcp": "it still loads the plugin's hooks and agents (only its MCP servers "
                           "stay out)",
    "--dangerously-load-development-channels": "it loads channel servers that are not on the "
                                               "approved list",
    "--agent": _AGENT, "--agents": _AGENT, "--agent-type": _AGENT,
    # Together they make the session a team member, and the binary routes a
    # team member's permission requests to the team lead over a mailbox.
    "--agent-id": _TEAMMATE, "--agent-name": _TEAMMATE, "--team-name": _TEAMMATE,
    "--no-session-persistence": "the session would not be saved, so the next respawn would lose "
                                "the conversation",
    "--resume-session-at": _TRUNCATED, "--resume-drops-turn": _TRUNCATED,
    "--reply-on-resume": "it can start a turn on its own when the pair respawns",
    "--rewind-files": "it restores files to an earlier state and exits",
    "--worktree": _MOVED, "-w": _MOVED, "--deep-link-cwd-b64": _MOVED,
    "--tmux": "it needs --worktree",
    "--bg": _OTHER_SESSION, "--background": _OTHER_SESSION, "--cloud": _OTHER_SESSION,
    "--remote": _OTHER_SESSION, "--desktop": _OTHER_SESSION, "--environment": _OTHER_SESSION,
    "--pool": _OTHER_SESSION, "--attach-serve": _OTHER_SESSION,
    "--remote-control": _OTHER_SESSION, "--rc": _OTHER_SESSION, "--teleport": _OTHER_SESSION,
    "--from-pr": _OTHER_SESSION, "--ide": _OTHER_SESSION, "--routine": _OTHER_SESSION,
    "--exec": _DISPATCH, "-m": _DISPATCH, "--handle-uri": _DISPATCH,
    "--chrome": "it connects Claude in Chrome — your real, signed-in browser, in visible "
                "windows — past the mcp_whitelist opt-in",
    "--disable-slash-commands": "it disables skills, and with them /compact, /context and "
                                "pair_invoke",
    "--init-only": "it runs setup hooks and exits — the pair would never start",
    "--version": "it prints and exits — the pair would never start", "-v": "it prints and exits",
    "--help": "it prints and exits — the pair would never start", "-h": "it prints and exits",
    "--": "it ends option parsing, so everything after it would become the prompt",
    "-": "it ends option parsing, so everything after it would become the prompt",
}
# ``backend_options.settings`` is an ALLOWLIST (second review of b07509e,
# P2). The denylist it replaces missed ``autoMode``, whose allow / soft_deny
# / hard_deny lists replace the auto-mode classifier's rules (--settings is
# one of the sources the classifier reads) while pair_info still says
# ``auto``; the 2.1.295 schema also has command helpers that run outside the
# permission system (apiKeyHelper, awsAuthRefresh, awsCredentialExport,
# otelHeadersHelper, processWrapper …), cleanupPeriodDays (the retention of
# ALL your transcripts), statusLine, claudeMd, autoMemoryDirectory … Each key
# below was read from that schema (2026-10-09) and can't loosen the pair or
# change what it loads: model is inert (every spawn passes --model), the rest
# shape replies, commit text and Bash output size. permissions and env are
# checked one level down.
_ALLOWED_SETTINGS = ("permissions", "env", "model", "outputStyle", "language",
                     "alwaysThinkingEnabled", "attribution", "includeCoAuthoredBy",
                     "includeGitInstructions", "bashOutputMaxChars")
# Tightening only: blockReadsOutsideWorkingDirectories refuses file-tool reads
# outside the working directories, and only ``true`` passes — the schema says
# true in any settings source wins, but a ``false`` has no use here either.
# (disableBypassPermissionsMode / disableAutoMode would contradict an
# unrestricted / auto pair, so they stay out too.)
_ALLOWED_PERMISSIONS = ("deny", "ask", "blockReadsOutsideWorkingDirectories")
# settings.env reaches the CLI and every command the pair runs, so the names
# are an allowlist too: CLAUDE_CODE_SHELL_PREFIX wraps every command,
# CLAUDE_CODE_PROCESS_WRAPPER / NODE_OPTIONS / PATH / BASH_ENV / GIT_SSH_COMMAND
# change what an approved command runs, CLAUDE_CONFIG_DIR swaps the whole
# config, ANTHROPIC_* swap the credentials or the endpoint. What passes:
# Claude Code's own timeouts, output limits and telemetry switches (each name
# found in the 2.1.295 binary).
_ALLOWED_ENV = ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", "BASH_MAX_OUTPUT_LENGTH",
                "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "MCP_TIMEOUT",
                "MCP_TOOL_TIMEOUT", "MAX_MCP_OUTPUT_TOKENS", "DISABLE_TELEMETRY",
                "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR")
# Refused keys a pair field covers: the refusal names the field.
_SETTINGS_FIELDS: dict[str, str] = {
    "fallbackModel": "fallback_model", "effortLevel": "effort",
    "enableAllProjectMcpServers": "mcp_whitelist", "enabledMcpjsonServers": "mcp_whitelist",
}
_PERMISSION_FIELDS: dict[str, str] = {
    "allow": "allowed_tools", "defaultMode": "permission_mode",
    "additionalDirectories": "extra_dirs",
}
CLAUDE_BACKEND_OPTION_KEYS = ("settings", "args")


def _check_settings(settings: dict[str, Any]) -> None:
    """Raise ValueError unless every key of ``settings`` is on the allowlist."""
    if "ultracode" in settings:
        raise ValueError("set Ultracode with the ultracode parameter, not backend_options.settings "
                         "— both go into the same --settings JSON")
    for key in settings:
        if key in _SETTINGS_FIELDS:
            raise ValueError(f"backend_options.settings may not contain {key!r}: set it with "
                             f"{_SETTINGS_FIELDS[key]} instead.")
        if key not in _ALLOWED_SETTINGS:
            raise ValueError(
                f"backend_options.settings may not contain {key!r}: only {', '.join(_ALLOWED_SETTINGS)} "
                f"pass. Other keys can loosen the pair or reach past it (hooks, autoMode, command "
                f"helpers such as apiKeyHelper, cleanupPeriodDays …); keep those in your own settings files.")
    perms = settings.get("permissions")
    if perms is not None and not isinstance(perms, dict):
        raise ValueError("backend_options.settings.permissions must be a JSON object")
    for key in perms or {}:
        if key in _PERMISSION_FIELDS:
            raise ValueError(f"backend_options.settings may not contain permissions.{key}: set it with "
                             f"{_PERMISSION_FIELDS[key]} instead. Tightening rules (permissions.deny / "
                             f"ask) are fine.")
        if key not in _ALLOWED_PERMISSIONS:
            raise ValueError(f"backend_options.settings may not contain permissions.{key}: only "
                             f"{' / '.join(_ALLOWED_PERMISSIONS)} pass.")
        if key == "blockReadsOutsideWorkingDirectories" and perms[key] is not True:
            raise ValueError(f"backend_options.settings.permissions.{key} may only be true (got "
                             f"{json.dumps(perms[key])}): it is here to tighten, never to loosen.")
    env = settings.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("backend_options.settings.env must be a JSON object")
    for name in env or {}:
        if name not in _ALLOWED_ENV:
            raise ValueError(
                f"backend_options.settings.env may not set {name!r}: only Claude Code's own timeout, "
                f"output-limit and telemetry variables pass ({', '.join(_ALLOWED_ENV)}). Others can "
                f"change what the pair's commands run or where it connects (CLAUDE_CODE_SHELL_PREFIX, "
                f"CLAUDE_CONFIG_DIR, ANTHROPIC_*, PATH …).")


def _refused_arg(token: str) -> str | None:
    """Why a raw ``args`` token is refused, or None. Checks ``--flag=value``
    and combined short flags (``-cp`` = ``-c -p``)."""
    flag = token.split("=", 1)[0]
    if flag in _MANAGED_FLAGS:
        return f"set it with {_MANAGED_FLAGS[flag]} instead"
    if flag in _REFUSED_FLAGS:
        return _REFUSED_FLAGS[flag]
    if len(flag) > 2 and flag[0] == "-" and flag[1] != "-":
        for ch in flag[1:]:
            if f"-{ch}" in _REFUSED_FLAGS:
                return f"-{ch}: {_REFUSED_FLAGS['-' + ch]}"
    return None


# ---- v0.18.0: API-key billing ------------------------------------------------
# Measured on CLI 2.1.296, Windows, 2026-10-11 (HANDOFF "API-key billing for
# pairs"): a pair inherits CLAUDE_CODE_ENTRYPOINT=claude-desktop from a desktop
# session, and with it the CLI IGNORES ANTHROPIC_API_KEY (a fake key still got
# an answer, on the plan); dropping only that variable made the key win
# ("Invalid API key"). ANTHROPIC_AUTH_TOKEN outranks the key (docs).
API_KEY_FILE = "api_key"
# Any Console API key: personal (sk-ant-usr…), service-account, legacy
# workspace (sk-ant-api…). Verified 2026-10-11 by the manager: a personal key
# in ANTHROPIC_API_KEY answered a direct `claude -p` Haiku turn, covered by
# the credits. The known NON-API kinds are refused by prefix.
API_KEY_PREFIX = "sk-ant-"
_NON_API_KEYS = (
    ("sk-ant-oat", "an OAuth access token (from a claude.ai login or setup-token)"),
    ("sk-ant-ort", "an OAuth refresh token"),
    ("sk-ant-admin", "an Admin API key (it manages the org; it can't run requests)"),
)
OFFICIAL_BASE_URL = "https://api.anthropic.com"
_API_DROP_ENV = ("CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_AUTH_TOKEN")
_CLOUD_PROVIDER_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# Docs text when credits run out: "Your credit balance is too low to access the
# Anthropic API…" (API) / "Credit balance too low · Add funds…" (Claude Code).
# Not reproducible while credits remain. Matched only on an error result
# (is_error / api_error_status) of an API-billed pair, never on an answer.
_CREDIT_RE = re.compile(r"credit balance (is )?too low", re.IGNORECASE)
# A rejected key or org: "Invalid API key · Fix external API key" (fake-key
# probe through the warm runtime, 2.1.296, 2026-10-11: subtype success,
# is_error true, api_error_status 401) and the API's 403 texts for a key or
# org that was switched off. Same gating as _CREDIT_RE.
_INVALID_KEY_RE = re.compile(r"invalid api key|api key authentication is disabled"
                             r"|organization has been disabled", re.IGNORECASE)
# Docs: a personal or service-account key not scoped to one workspace gets a
# 400 invalid_request_error "anthropic-workspace-id is required when
# authenticating with an identity-linked API key…". Same gating.
_WORKSPACE_REQUIRED_RE = re.compile(r"anthropic-workspace-id is required", re.IGNORECASE)


def api_key_path() -> Path:
    """``<CLAUDE_HOME>/pairs/api_key``: the one place the key lives."""
    return pairs_dir() / API_KEY_FILE


def read_api_key() -> str:
    """The key for billing="api" pairs, read fresh at every spawn. Raises
    BillingError — whose text never includes any of the file's content."""
    path = api_key_path()
    try:
        key = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise BillingError(
            f"the API key file {path} does not exist. Write an API key of the Console org "
            f"linked to your plan there (one line, starting with {API_KEY_PREFIX}), or move "
            f"the pair back to the plan with pair_update(name, billing=\"plan\").") from None
    except (OSError, UnicodeError) as e:
        # Only the type: a decode error's text quotes the file's bytes.
        raise BillingError(f"the API key file {path} could not be read ({type(e).__name__}).") from None
    if not key.startswith(API_KEY_PREFIX) or any(ch.isspace() for ch in key):
        raise BillingError(f"the API key file {path} must hold exactly one API key (a single line "
                           f"starting with {API_KEY_PREFIX}).")
    for prefix, kind in _NON_API_KEYS:
        if key.startswith(prefix):
            raise BillingError(f"the API key file {path} holds {kind}, not an API key. Create a "
                               f"personal or service-account API key in the Console of the org "
                               f"linked to your plan.")
    return key


def api_key_fingerprint(key: str | None) -> str | None:
    """A short hash to notice a rotated key without keeping the key around."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else None


def current_key_fingerprint() -> str | None:
    try:
        return api_key_fingerprint(read_api_key())
    except BillingError:
        return None


def api_key_status() -> str:
    """For pair_info and pair_billing: ``key …abcd`` (the last four characters
    only) or why there is no usable key."""
    try:
        key = read_api_key()
    except BillingError:
        path = api_key_path()
        return f"key file missing: {path}" if not path.exists() else f"key file unusable: {path}"
    return f"key …{key[-4:]}"


def book_cost(name: str, cost: float, billing: str) -> None:
    """Add spend outside a send (compaction, pair_invoke, a result with no
    turn open) to the pair's totals — and to api_cost_usd on API billing.
    Best effort: bookkeeping never fails the operation."""
    if not cost:
        return
    try:
        from claude_squared.registry import get_pair, update_pair
        cur = get_pair(name)
        fields: dict[str, Any] = {"total_cost_usd": cur.total_cost_usd + cost}
        if billing == "api":
            fields["api_cost_usd"] = cur.api_cost_usd + cost
        update_pair(name, **fields)
    except Exception:
        pass


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def claude_env(billing: str = "plan") -> dict[str, str]:
    """The environment of a claude spawn — the ONE builder every Claude spawn
    site uses (warm runtime, one-shot --print, stream-json, triage, ``claude
    mcp list``, ``--help``).

    ``plan``: this process's environment minus ``ANTHROPIC_API_KEY``. In -p a
    key always beats the claude.ai login (docs), so a key in the server's
    environment would silently bill every pair; plan is plan by construction.

    ``api``: the key file's key, without ``CLAUDE_CODE_ENTRYPOINT`` (with it
    the CLI ignores the key) and ``ANTHROPIC_AUTH_TOKEN`` (it outranks the
    key). Refuses an environment that would send the key elsewhere — a cloud
    provider switch or a non-official ``ANTHROPIC_BASE_URL`` (the official one,
    which the desktop app sets, is dropped). Sets
    ``CLAUDE_CODE_PROMPT_CACHE_TTL=1h`` (an API key otherwise gets 5 min).
    The key never enters ``os.environ`` — but the pair's own processes (its
    Bash, hooks, MCP servers) inherit it. No CLAUDE_CODE_SUBPROCESS_ENV_SCRUB:
    on 2.1.296 it forces the permission mode to ``default`` ("allowed_non_write_users
    hardening"; review round 1, 2026-10-11), and Bash could read the key file anyway."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    if billing != "api":
        return env
    for var in _CLOUD_PROVIDER_ENV:
        if _truthy(env.get(var)):
            raise BillingError(f"{var} is set in the MCP server's environment: the pair would run "
                               f"on that cloud provider, not on the API key.")
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    if base_url and base_url.lower() != OFFICIAL_BASE_URL:
        raise BillingError("ANTHROPIC_BASE_URL in the MCP server's environment points somewhere other "
                           f"than {OFFICIAL_BASE_URL}; an API-billed pair won't send the key there.")
    env.pop("ANTHROPIC_BASE_URL", None)
    for var in _API_DROP_ENV:
        env.pop(var, None)
    env["ANTHROPIC_API_KEY"] = read_api_key()
    env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "1h"
    return env


def key_file_deny_rule() -> str:
    """``Read(//c/Users/…/pairs/api_key)``: a scoped deny rule for the key file,
    passed on every Claude spawn so the key can't land in a transcript through
    the Read tool. Absolute rule paths are POSIX with the drive letter as the
    first segment on Windows. Probed 2026-10-11 (2.1.296, Haiku, a canary
    file): this form, ``~/…``, ``//C/…``, ``C:/…`` and ``C:\\…`` all denied the
    Read; with no rule the turn read the canary. ``cat`` in Bash is not covered."""
    path = Path(os.path.abspath(api_key_path()))
    posix = path.as_posix()
    if path.drive:
        posix = "/" + path.drive.rstrip(":").lower() + posix[len(path.drive):]
    return f"Read(/{posix})"


def _workspace_help(name: str) -> str:
    return (f"the API wants a workspace for this key: a personal or service-account key must be "
            f"created scoped to a single workspace (Console → API keys). Replace the key in "
            f"{api_key_path()}, or move '{name}' back to the plan with pair_update('{name}', "
            f"billing=\"plan\").")


def _key_rejected_help(name: str) -> str:
    return (f"the API rejected the key in {api_key_path()} (or its org is switched off). Fix the "
            f"key file (the next spawn reads "
            f"it again), or move '{name}' back to the plan with pair_update('{name}', "
            f"billing=\"plan\").")


def _billing_failure(spec: "PairSpec | None", text: str) -> str | None:
    """The clear message for a failed process of an API-billed pair whose
    output says the credits ran out or the key was rejected, else None.
    Only for processes that already failed (non-zero exit)."""
    if spec is None or spec.billing != "api":
        return None
    if _CREDIT_RE.search(text):
        return _credit_help(spec.name)
    if _WORKSPACE_REQUIRED_RE.search(text):
        return _workspace_help(spec.name)
    if _INVALID_KEY_RE.search(text):
        return _key_rejected_help(spec.name)
    return None


def _failure_text(stdout: str, stderr: str) -> str:
    """What a failed process said about itself: the LAST result event's text
    (``--output-format json`` or stream-json), else stderr. Never the rest of
    stdout — tool results there may quote anything (review: an
    error_max_turns run that had quoted the key error read as a rejected key)."""
    try:
        whole = json.loads(stdout)
    except ValueError:
        whole = None
    if isinstance(whole, dict) and whole.get("type") == "result":
        return str(whole.get("result") or "")
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return str(event.get("result") or "")
    return stderr


def _credit_help(name: str) -> str:
    return (f"the API rejected the turn: the Console org's credits are used up. Move '{name}' back "
            f"to the plan with pair_update('{name}', billing=\"plan\") and re-send, or wait for "
            f"next month's credits.")


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


_PERM_CHOICES_CACHE: dict[str, tuple[str, ...]] = {}


def _cli_permission_choices() -> tuple[str, ...]:
    """The ``--permission-mode`` choices the installed CLI advertises in
    ``--help`` (e.g. acceptEdits, auto, bypassPermissions, manual, dontAsk,
    plan on 2.1.258). Empty tuple when it can't be read — callers then fall
    back to the static map. One subprocess per MCP process lifetime."""
    if "choices" in _PERM_CHOICES_CACHE:
        return _PERM_CHOICES_CACHE["choices"]
    choices: tuple[str, ...] = ()
    try:
        out = subprocess.run([_claude_executable(), "--help"], capture_output=True, text=True,
                             timeout=20, stdin=subprocess.DEVNULL, env=claude_env()).stdout
        i = out.find("--permission-mode")
        seg = out[i:i + 600] if i >= 0 else ""
        m = re.search(r"choices:\s*(.*?)\)", seg, re.S)
        if m:
            choices = tuple(re.findall(r'"([A-Za-z]+)"', m.group(1)))
    except Exception:
        choices = ()
    _PERM_CHOICES_CACHE["choices"] = choices
    return choices


def _extract_stop_details(result_json: dict) -> dict | None:
    """Find a ``stop_details`` object across plausible envelope locations.

    The CLI's exact passthrough of the API's ``stop_details`` (carrying the
    refusal ``category`` — cyber/bio) isn't repro-verified, so we don't assume
    it's hoisted to the top level. Checks ``stop_details`` at the top, under a
    ``message`` object, and under ``result_meta`` (defensive). Returns the first
    dict found, else ``None`` — callers treat ``None`` as "category unknown",
    which is a clean degrade from the ``stop_reason`` boolean floor.
    """
    for path in (("stop_details",), ("message", "stop_details"),
                 ("result_meta", "stop_details")):
        cur: Any = result_json
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):
            return cur
    return None


def _select_primary_model(model_usage: dict, requested: str) -> str:
    """Pick the model the turn actually ran on from a multi-key ``modelUsage``.

    The CLI's ``modelUsage`` routinely carries the lightweight helper model
    (``claude-haiku-4-5-*``, used for summaries/titles) alongside the real one,
    and their order is insertion-order — NOT guaranteed to put the main model
    first (verified: the non-stream ``--output-format json`` path listed haiku
    first). ``next(iter(...))`` would mislabel the turn AND read the helper's
    200k context window. Tie-break: (1) the key whose family matches the
    requested family, (2) the key with the largest contextWindow (the real work
    model; the helper has the small window), (3) first key. Falls back to the
    requested id when ``modelUsage`` is empty.
    """
    keys = list(model_usage.keys())
    if not keys:
        return requested
    rf, _ = parse_model_id(requested)
    if rf:
        for k in keys:
            kf, _ = parse_model_id(k)
            if kf == rf:
                return k

    def _cw(k: str) -> int:
        try:
            return int((model_usage.get(k) or {}).get("contextWindow") or 0)
        except (TypeError, ValueError):
            return 0

    return max(keys, key=_cw)


class ClaudeAdapter(PairAdapter):
    backend_name = "claude"

    # ---- neutral → native translation ----------------------------------

    @staticmethod
    def cli_model(spec: PairSpec, override: str | None = None) -> str:
        """The ``--model`` value: the bare id plus ``[1m]`` when the pair's
        ``context_window`` is ``"1m"`` (or the override itself carries a
        suffix). v0.13.0: the suffix lives in ``context_window``, never in
        ``spec.model`` — the registry validator strips it on load. Redundant
        on Opus 5 / Fable (1M bare, verified 2026-08-07) and harmless."""
        bare, cw = split_model_tier(override or spec.model)
        if cw == "1m" or spec.context_window == "1m":
            return f"{bare}[1m]"
        return bare

    @staticmethod
    def validate_backend_options(backend_options: dict[str, Any] | None, *,
                                 allow_unknown: bool = False) -> dict[str, Any]:
        """Check (and normalize) a Claude pair's ``backend_options``; raises
        ValueError. ``settings``: a JSON object (dict or JSON string), merged
        with the ultracode key into the one ``--settings`` flag, holding only
        the keys ``_check_settings`` allows. ``args``: a list of raw CLI
        tokens, none of them in ``_MANAGED_FLAGS`` / ``_REFUSED_FLAGS``.

        ``allow_unknown`` is the spawn-time mode: before v0.17.0 a Claude pair
        accepted any keys and ignored them, so a stored unknown key must not
        stop the pair from starting — only the two keys it reads are checked.
        """
        bo = dict(backend_options or {})
        unknown = sorted(set(bo) - set(CLAUDE_BACKEND_OPTION_KEYS))
        if unknown and not allow_unknown:
            raise ValueError(
                f"backend_options {unknown} are not Claude options. Claude takes 'settings' (a "
                f"settings JSON object, passed as --settings) and 'args' (a list of raw CLI args); "
                f"'config' / 'sandbox' / 'approval' are Codex-only.")
        if "settings" in bo:
            settings = bo["settings"]
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except ValueError as e:
                    raise ValueError(f"backend_options.settings must be a JSON object: {e}") from None
            if not isinstance(settings, dict):
                raise ValueError("backend_options.settings must be a JSON object (a dict)")
            _check_settings(settings)
            bo["settings"] = settings
        if "args" in bo:
            raw = bo["args"]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
                raise ValueError("backend_options.args must be a list of strings, one CLI token each "
                                 "(e.g. [\"--max-budget-usd\", \"5\"])")
            if raw and not raw[0].startswith("-"):
                # They end every argv: a leading value would become a second
                # prompt (or the prompt, on the stream-json paths).
                raise ValueError(f"backend_options.args must start with a flag (got {raw[0]!r})")
            for token in raw:
                why = _refused_arg(token)
                if why:
                    raise ValueError(f"backend_options.args may not contain {token.split('=', 1)[0]}: {why}.")
            bo["args"] = raw
        return bo

    @staticmethod
    def native_permission(level: str | None) -> str:
        """Neutral level (or any accepted alias) → ``--permission-mode`` value.

        The CLI renamed ``default`` to ``manual`` in 2.1.258 (``--help`` lists
        manual; ``default`` is still accepted but undocumented — verified
        2026-09-07). The installed CLI's own ``--help`` decides which spelling
        we emit for ``read-only``, so neither an older nor a future CLI
        breaks read-only pairs. Cached per process.
        """
        native = CLAUDE_PERMISSION_MAP[normalize_permission(level)]
        if native in ("manual", "default"):
            choices = _cli_permission_choices()
            if choices:
                if "manual" in choices:
                    return "manual"
                if "default" in choices:
                    return "default"
        return native

    # ---- public surface ------------------------------------------------

    def create(self, spec: PairSpec, initial_message: str | None = None) -> CreateResult:
        """Create a new session with pinned config; optionally send the first message.

        Connector selection is a deliberate per-pair opt-in, default none;
        its flags are re-applied on resume. System instructions remain pinned.
        """
        if not spec.session_id:
            spec.session_id = str(uuid.uuid4())

        args = self._common_create_args(spec)
        # Use --print + initial message (or a no-op probe) to actually create the session on disk.
        prompt = initial_message or "Reply with exactly: pair-ready"
        args += ["--print", "--output-format", "json",
                 "--session-id", spec.session_id,
                 "--model", self.cli_model(spec),
                 "--permission-mode", self.native_permission(spec.permission_mode),
                 "-p", prompt]
        args += self._extra_args(spec)

        result_json = self._run_print(args, timeout_seconds=300, pair_name=spec.name, cwd=spec.cwd,
                                      env_spec=spec,
                                      **({"connector_spec": spec} if connectors.effective(spec.mcp_whitelist) else {}))
        return CreateResult(
            name=spec.name,
            session_id=result_json.get("session_id", spec.session_id),
            transcript_path=str(self.transcript_path(spec)) if self.transcript_path(spec) else None,
            initial_response=result_json.get("result"),
            notes=connectors.claude_init_notes(spec.mcp_whitelist, result_json.get("_mcp_servers")),
        )

    def send(self, spec: PairSpec, message: str, *, model: str | None = None,
             effort: str | None = None, permission_mode: str | None = None,
             timeout_seconds: int | None = 300,
             on_event: "callable | None" = None,
             should_stop: "callable | None" = None,
             task_id: str | None = None) -> SendResult:
        # Warm sends are promoted by the runtime only after implicit work ends.
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
            # v0.12.0: wait out any self-woken turn BEFORE the stale check —
            # the turn is writing the JSONL, which would otherwise read as
            # stale and evict (tree-kill) the pair's in-flight background work.
            # The self-woken turn's close refreshes the watermark (our own
            # write), so the warm runtime is kept; only a genuine foreign write
            # respawns it here. Background work never moves the JSONL on its
            # own (verified 2026-09-25), so no refresh is needed between turns.
            pre_wait_s = rt.wait_for_implicit_idle(timeout_seconds, should_stop)
            if rt.is_stale():
                reg.evict(spec.name, "another process wrote the pair's session; the stale "
                                     "runtime was respawned")
                rt = reg.get_or_start(spec, self)
            result_json = rt.send(message, timeout_seconds=timeout_seconds,
                                  on_event=on_event, should_stop=should_stop, task_id=task_id)
            if pre_wait_s:
                sw = result_json.setdefault("_self_woken", {})
                sw["waited_s"] = float(sw.get("waited_s") or 0.0) + pre_wait_s
            return self._build_send_result(spec, result_json)

        # Resolve the effective effort for this one-shot send: per-call override
        # wins, else fall back to the pinned spec value. Pass --effort only if
        # the resolved value is non-None (haiku has no effort knob).
        eff = effort if effort is not None else spec.effort
        args = [
            "--print", "--output-format", "json",
            "--resume", spec.session_id,
            "--model", self.cli_model(spec, model),
        ]
        args += self._fallback_args(spec)
        args += self._mcp_args(spec, permission_mode)
        args += self._settings_args(spec)
        if eff is not None:
            args += ["--effort", eff]
        args += [
            "--permission-mode", self.native_permission(permission_mode or spec.permission_mode),
            "-p", message,
        ]
        args += self._extra_args(spec)
        env = self._spawn_env(spec)  # a missing key refuses before the task starts
        from claude_squared.async_tasks import mark_task_executing
        from claude_squared.runtime import refuse_if_shutting_down
        refuse_if_shutting_down()
        mark_task_executing(task_id)
        result_json = self._run_print(args, timeout_seconds=timeout_seconds,
                                      pair_name=spec.name, cwd=spec.cwd, env_spec=spec, env=env,
                                      **({"connector_spec": spec} if connectors.effective(spec.mcp_whitelist) else {}))
        return self._build_send_result(spec, result_json)

    def compact(self, spec: PairSpec, steering_prompt: str | None = None,
                timeout_seconds: int = 600, should_stop: "callable | None" = None) -> CompactResult:
        """Run /compact natively via stream-json. Returns pre/post token counts.
        ``should_stop`` is accepted for parity with CodexAdapter (a one-shot
        subprocess.run can't be interrupted mid-way here)."""
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

    def fork(self, spec: PairSpec, sentinel: str, timeout_seconds: int = 300) -> str:
        """Fork the pair's session via native ``--fork-session``; return the new
        session_id. (v0.10.0)

        ``--fork-session`` resumes the existing session but writes to a NEW
        session id, leaving the original untouched, and correctly rewrites the
        copied history's internal id references (verified empirically — far
        safer than hand-copying the JSONL, and future-proof against schema
        drift). ``--print`` requires a turn, so we send one ``sentinel`` turn;
        the caller truncates it back off the new JSONL's tip afterward (see
        ``transcript.find_sentinel_line`` + ``truncate_jsonl_before_line``).
        """
        args = [
            "--print", "--output-format", "json",
            "--resume", spec.session_id, "--fork-session",
            "--model", self.cli_model(spec),
            *self._fallback_args(spec),
            "--permission-mode", self.native_permission(spec.permission_mode),
            "-p", sentinel,
        ]
        args += self._mcp_args(spec)
        args += self._option_args(spec)  # raw args last
        result_json = self._run_print(
            args, timeout_seconds=timeout_seconds, pair_name=spec.name, cwd=spec.cwd, env_spec=spec,
            **({"connector_spec": spec} if connectors.effective(spec.mcp_whitelist) else {})
        )
        new_sid = result_json.get("session_id")
        if not new_sid or new_sid == spec.session_id:
            raise CLIError(
                f"--fork-session did not produce a new session id (got {new_sid!r}); "
                f"the pair's original session is unchanged."
            )
        return new_sid

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

    @staticmethod
    def _fallback_args(spec: PairSpec) -> list[str]:
        """``--fallback-model`` args when the pair has one configured, else []."""
        fb = (spec.fallback_model or "").strip()
        return ["--fallback-model", fb] if fb else []

    def _common_create_args(self, spec: PairSpec) -> list[str]:
        """Args that pin specialization at create time (verified to persist on resume)."""
        args: list[str] = []

        args += self._mcp_args(spec)
        # v0.9.10: Ultracode mode. Anthropic's mechanism is a session settings
        # key (--settings), NOT a --effort value. The CLI rejects --effort
        # ultracode with a warning ("Unknown --effort value 'ultracode'") but
        # silently accepts --settings '{"ultracode": true}'. Verified against
        # CLI 2.1.165 via direct probe; binary strings document the design
        # ("ultracode (xhigh effort plus standing dynamic-workflow
        # orchestration) is active for the session. Set per session via the
        # 'ultracode' settings key"). Pass it alongside any --effort value —
        # ultracode and effort are independent fields in the CLI's data model
        # (binary: ``effortValue:_,ultracode:f``). v0.17.0: merged with
        # backend_options.settings into ONE --settings JSON (_settings_args).
        args += self._settings_args(spec)

        # v0.11.0: automatic fallback when the primary model is overloaded or
        # unavailable (e.g. a subscription/trial-flagged model loses access).
        # ``--fallback-model`` is print-only (all our spawn paths are --print)
        # and re-tries the primary at the start of each user turn. Pinned here
        # so both create() and the persistent runtime (runtime.start, which also
        # calls _common_create_args) inherit it.
        args += self._fallback_args(spec)

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

        if spec.allowed_tools and not connectors.effective(spec.mcp_whitelist):
            args += ["--allowed-tools", " ".join(spec.allowed_tools)]

        # v0.17.0: backend_options.args are NOT here — both callers append
        # more flags, which a raw flag missing its value would swallow
        # (["--system-prompt"] eating --print). They end every argv instead.
        return args

    def _mcp_args(self, spec: PairSpec, permission_mode: str | None = None) -> list[str]:
        args = connectors.claude_args(
            spec.mcp_whitelist, permission_mode or spec.permission_mode, spec.cwd, spec.allowed_tools,
        )
        # Reapplying disallow flags on resume must retain headless exclusions.
        for tool in HEADLESS_INCOMPATIBLE_TOOLS:
            args += ["--disallowed-tools", tool]
        # Claude in Chrome drives the user's real, signed-in Chrome, and
        # --strict-mcp-config doesn't stop it. The 2.1.295 binary decides it
        # from --chrome / --no-chrome first, then CLAUDE_CODE_ENABLE_CFC in the
        # environment a pair inherits, then (interactive sessions only)
        # claudeInChromeDefaultEnabled — so --no-chrome settles it for every
        # pair whose mcp_whitelist doesn't name it (third review of ac6d2c4).
        # The flag is in the 2.1.258 and 2.1.280 binaries too.
        if not connectors.chrome_selected(spec.mcp_whitelist):
            args.append("--no-chrome")
        # v0.18.0: the API key file is never readable through the Read tool,
        # on any pair. A scoped rule leaves the tool list (and with it the
        # prompt cache) unchanged.
        args += ["--disallowed-tools", key_file_deny_rule()]
        # v0.17.0: the pair's own removals ride with the connector flags, so
        # every spawn path (runtime, override send, compact, fork, /context)
        # sends the same tool list — a different list on one path would miss
        # the prompt cache for the whole context — and a pair_update applies
        # at the next spawn without a pair_clear.
        for tool in self.disallowed_tools(spec):
            args += ["--disallowed-tools", tool]
        return args

    @staticmethod
    def disallowed_tools(spec: PairSpec) -> list[str]:
        """The pair's ``disallowed_tools``: stripped, de-duplicated in order,
        minus the headless exclusions ``_mcp_args`` already passes."""
        out: list[str] = []
        for tool in spec.disallowed_tools or []:
            tool = str(tool).strip()
            if tool and tool not in out and tool not in HEADLESS_INCOMPATIBLE_TOOLS:
                out.append(tool)
        return out

    @classmethod
    def _claude_options(cls, spec: PairSpec) -> dict[str, Any]:
        """The pair's validated ``backend_options`` (spawn-time mode). A
        malformed ``settings`` / ``args`` refuses the spawn: silently dropping
        them could drop a permission rule the user put in ``settings``."""
        try:
            return cls.validate_backend_options(spec.backend_options, allow_unknown=True)
        except ValueError as e:
            raise PairError(f"pair '{spec.name}' has unusable backend_options: {e}. "
                           f"Fix it with pair_update(name='{spec.name}', backend_options=...).") from None

    def _settings_args(self, spec: PairSpec) -> list[str]:
        """ONE ``--settings`` JSON: backend_options.settings plus the ultracode
        key. Keys sorted so the same config always yields the same argument
        (ultracode alone is still ``{"ultracode": true}``, as before v0.17)."""
        merged = dict(self._claude_options(spec).get("settings") or {})
        if spec.ultracode:
            merged["ultracode"] = True
        return ["--settings", json.dumps(merged, sort_keys=True)] if merged else []

    def _extra_args(self, spec: PairSpec) -> list[str]:
        """backend_options.args, verbatim. Every spawn path puts them LAST, so
        a raw flag missing its value can't consume one of the pair's own."""
        return list(self._claude_options(spec).get("args") or [])

    def _option_args(self, spec: PairSpec) -> list[str]:
        """Settings + raw args, for the fork and stream-json paths whose argv
        ends with the tool scope. v0.17.0: ultracode used to reach only
        create and the runtime."""
        return self._settings_args(spec) + self._extra_args(spec)

    def _resolve_system_prompt(self, spec: PairSpec) -> str | None:
        parts = []
        if spec.profile_name:
            profile_path = profiles_dir() / f"{spec.profile_name}.md"
            if profile_path.exists():
                parts.append(profile_path.read_text(encoding="utf-8").strip())
        if spec.system_prompt_append:
            parts.append(spec.system_prompt_append.strip())
        return "\n\n".join(parts) if parts else None

    @staticmethod
    def _spawn_env(spec: PairSpec | None) -> dict[str, str]:
        """claude_env for this pair's billing (plan when there's no spec)."""
        return claude_env(spec.billing if spec is not None else "plan")

    def _run_print(self, args: list[str], *, timeout_seconds: int, pair_name: str,
                   cwd: str | None = None, connector_spec: PairSpec | None = None,
                   env_spec: PairSpec | None = None, env: dict[str, str] | None = None) -> dict:
        cli = _claude_executable()
        if env is None:
            env = self._spawn_env(env_spec)
        if connector_spec is not None and connectors.effective(connector_spec.mcp_whitelist):
            args = list(args)
            i = args.index("--output-format")
            args[i + 1] = "stream-json"
            args.insert(i + 2, "--verbose")  # not appended: raw args stay last
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
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise CommandTimeout(pair_name, timeout_seconds) from e

        if proc.returncode != 0:
            stderr_txt = proc.stderr.decode("utf-8", errors="replace")
            stdout_txt = proc.stdout.decode("utf-8", errors="replace")
            # v0.11.0: surface the "model is gone" case clearly. When a model is
            # pulled / access is lost, the CLI exits 1 — but for --output-format
            # json the error lands on STDOUT as a valid result JSON (is_error,
            # api_error_status 404), with stderr EMPTY (verified the day Fable 5
            # was pulled). So check BOTH streams for the marker, not just stderr.
            combined = (stderr_txt + "\n" + stdout_txt).lower()
            failure = _billing_failure(env_spec, _failure_text(stdout_txt, stderr_txt))
            if failure:
                raise CLIError(failure, stderr=stderr_txt or stdout_txt[:2000], exit_code=proc.returncode)
            if MODEL_UNAVAILABLE_MARKER in combined:
                raise CLIError(
                    _MODEL_UNAVAILABLE_HELP,
                    stderr=stderr_txt or stdout_txt[:2000],
                    exit_code=proc.returncode,
                )
            raise CLIError(
                f"claude CLI exited non-zero",
                stderr=stderr_txt or stdout_txt[:2000],
                exit_code=proc.returncode,
            )
        try:
            if connector_spec is not None and connectors.effective(connector_spec.mcp_whitelist):
                events = []
                for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
                self._attach_mcp_init(events)
                for event in reversed(events):
                    if event.get("type") == "result":
                        return event
                raise CLIError("claude CLI stream contained no result event")
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
                "--model", self.cli_model(spec),
                *self._fallback_args(spec),
                "--permission-mode", self.native_permission(spec.permission_mode),
                "--input-format", "stream-json",
                "--output-format", "stream-json"]
        args += self._mcp_args(spec)
        args += self._option_args(spec)  # raw args last

        stdin_payload = "\n".join(
            json.dumps({"type": "user", "message": {"role": "user", "content": m}})
            for m in messages
        ) + "\n"

        env = self._spawn_env(spec)
        try:
            proc = subprocess.run(
                args,
                input=stdin_payload.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                cwd=spec.cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise CommandTimeout(spec.name, timeout_seconds) from e

        if proc.returncode != 0:
            stderr_txt = proc.stderr.decode("utf-8", errors="replace")
            # v0.18.0: compaction / /context / pair_invoke on an API pair whose
            # credits ran out or whose key was rejected say so, as _run_print does.
            failure = _billing_failure(
                spec, _failure_text(proc.stdout.decode("utf-8", errors="replace"), stderr_txt))
            raise CLIError(failure or "claude CLI (stream-json) exited non-zero",
                           stderr=stderr_txt, exit_code=proc.returncode)

        events: list[dict] = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if connectors.effective(spec.mcp_whitelist):
            self._attach_mcp_init(events)
        # v0.18.0: compaction and pair_invoke spend counts too (one process,
        # so its last result's total is its whole cost).
        totals = [float(e.get("total_cost_usd") or 0.0) for e in events if e.get("type") == "result"]
        if totals and max(totals) > 0:
            book_cost(spec.name, max(totals), spec.billing)
        return events

    @staticmethod
    def _attach_mcp_init(events: list[dict]) -> None:
        init = next((event for event in reversed(events)
                     if event.get("type") == "system" and event.get("subtype") == "init"
                     and not event.get("parent_tool_use_id")), {})
        for event in events:
            if event.get("type") == "result":
                event["_mcp_servers"] = init.get("mcp_servers")

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
        # Robust primary-model pick (NOT next(iter(...)) — modelUsage carries the
        # haiku helper too, in nondeterministic order). See _select_primary_model.
        model_used = _select_primary_model(model_usage, spec.model)
        # Prefer the CLI-reported window (authoritative — correctly reports 1M
        # for million-context models). Fall back only when it's absent: infer 1M
        # for 1m-variant model names (substring), else the standard 200k. Keeps
        # the context-fill % honest on million-context pairs even if a future
        # CLI omits contextWindow from modelUsage.
        ctx_window = model_usage.get(model_used, {}).get("contextWindow")
        window_source = "reported" if ctx_window else "estimated"
        if not ctx_window:
            from claude_squared.models import default_claude_window
            ctx_window = default_claude_window(model_used or spec.model, spec.context_window)
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
        # v0.12.0: injected by runtime.send() — self-woken turns that completed
        # since the previous send + how long this send queued behind one.
        self_woken = result_json.get("_self_woken") or {}
        waited = self_woken.get("waited_s")
        waited_s = float(waited) if (waited and float(waited) >= 0.5) else None

        # v0.11.0 model-handling hardening — all three are None on a normal turn.
        #
        # (a) Silent model substitution: the CLI served a different model than the
        #     pair requested. This is the UNIFYING signal for "the model changed
        #     under you" — a safety downgrade (fable → opus-4-8 on a cyber/bio
        #     classifier trip), --fallback-model kicking in on overload/trial-loss,
        #     or a capacity fallback. Detected purely from served-vs-requested, so
        #     it catches all three regardless of cause. The haiku helper in
        #     modelUsage does NOT false-trigger: _select_primary_model already
        #     resolved model_used to the requested family when present.
        model_substitution = model_substitution_note(spec.model, model_used)
        #
        # (b) Content-safety block/refusal. The documented signal (Anthropic API)
        #     is stop_reason=='refusal' with stop_details.category in {cyber, bio}
        #     — exactly the classifier in play. Displayed generically; the exact
        #     CLI passthrough of stop_details isn't repro-verified (reproducing a
        #     real cyber/bio block is off-limits), so we surface what's present
        #     and let the human interpret rather than hard-matching values.
        stop_reason = result_json.get("stop_reason")
        api_err = result_json.get("api_error_status")
        safety_signal: str | None = None
        safety_kind: str | None = None
        if stop_reason == "refusal":
            # The ONLY genuine safety event of the three — earns the safety
            # banner. stop_details location isn't repro-verified, so probe a few
            # plausible spots rather than only the top level (else the cyber/bio
            # category enrichment would be silently unreachable wherever the CLI
            # nests it). The stop_reason boolean is the floor; category is bonus.
            details = _extract_stop_details(result_json)
            cat = details.get("category") if isinstance(details, dict) else None
            expl = details.get("explanation") if isinstance(details, dict) else None
            parts = ["content-safety REFUSAL (stop_reason='refusal')"]
            if cat:
                parts.append(f"category={cat}")
            if expl:
                parts.append(str(expl)[:240])
            safety_signal = " — ".join(parts)
            safety_kind = "refusal"
        elif (spec.billing == "api" and (result_json.get("is_error") or api_err)
              and _CREDIT_RE.search(result_json.get("result") or "")):
            # v0.18.0: the Console org's credits ran out. Only an ERROR of an
            # API-billed pair — an answer quoting the text must not read as one.
            safety_signal = (f"{_credit_help(spec.name)} The API said: "
                             f"{(result_json.get('result') or '').strip()[:160]!r}")
            safety_kind = "credit_exhausted"
        elif (spec.billing == "api" and (result_json.get("is_error") or api_err)
              and _WORKSPACE_REQUIRED_RE.search(result_json.get("result") or "")):
            safety_signal = (f"{_workspace_help(spec.name)} The API said: "
                             f"{(result_json.get('result') or '').strip()[:160]!r}")
            safety_kind = "api_key_rejected"
        elif (spec.billing == "api" and (result_json.get("is_error") or api_err)
              and _INVALID_KEY_RE.search(result_json.get("result") or "")):
            # Otherwise it reads as a transient API error ("retry usually
            # resolves it") — a bad key never does.
            safety_signal = (f"{_key_rejected_help(spec.name)} The API said: "
                             f"{(result_json.get('result') or '').strip()[:160]!r}")
            safety_kind = "api_key_rejected"
        elif _USAGE_LIMIT_RE.search(result_json.get("result") or ""):
            # v0.12.0: the CLI reports a usage-limit hit as subtype SUCCESS with
            # the limit notice as the result text (observed on par6, 2026-08-22:
            # each background sub-agent completion woke the pair, which hit the
            # limit and "succeeded" with "You've hit your session limit · resets
            # 2pm"). Nobody was told. Labeled distinctly — it's neither a safety
            # event nor transient in the retry-now sense.
            safety_signal = (
                f"usage limit reached — the CLI returned the limit notice as this "
                f"turn's result: {(result_json.get('result') or '').strip()[:160]!r}"
            )
            safety_kind = "usage_limit"
        elif MODEL_UNAVAILABLE_MARKER in (result_json.get("result") or "").lower():
            # PERMANENT, not transient: the model was pulled / access lost (the
            # stream-json path returns this as a result event with api_error_status
            # 404 + this text + empty modelUsage). Must NOT read as "retry resolves
            # it" (it won't) — give the switch-model / set-fallback guidance.
            safety_signal = _MODEL_UNAVAILABLE_HELP
            safety_kind = "model_unavailable"
        elif api_err:
            # NOT a safety event — a transient API error (overload/rate-limit).
            # Labeled "error" so _fmt_send_result renders it as abnormal-not-block.
            safety_signal = (
                f"API error status ({api_err}) — most likely a transient overload "
                f"or rate-limit; retry usually resolves it (not a safety block)"
            )
            safety_kind = "error"
        elif result_json.get("is_error") and stop_reason not in (
            None, "end_turn", "tool_use", "stop_sequence", "max_tokens"
        ):
            safety_signal = (
                f"the turn ended with is_error and stop_reason={stop_reason!r} "
                f"(abnormal completion; not necessarily a safety block)"
            )
            safety_kind = "error"
        # Keep the raw stop_reason for transparency when it's not a normal finish.
        stop_reason_kept = stop_reason if stop_reason not in (None, "end_turn") else None
        try:
            api_calls = int(result_json.get("num_turns") or 0)
        except (TypeError, ValueError):
            api_calls = 0
        refused_outright = (safety_kind in ("credit_exhausted", "api_key_rejected")
                            and api_calls <= 1)

        return SendResult(
            name=spec.name,
            response=result_json.get("result", ""),
            session_id=result_json.get("session_id", spec.session_id),
            model_used=model_used,
            # v0.18.0: a warm runtime's total_cost_usd is a running total for
            # the process; its reader puts this turn's share in _turn_cost_usd.
            # A one-shot process reports its own (single-turn) total.
            # A turn the API refused outright (no credits, rejected key) costs
            # nothing; one that ran out partway keeps what it spent before.
            cost_usd=(0.0 if refused_outright
                      else result_json["_turn_cost_usd"] if result_json.get("_turn_cost_usd") is not None
                      else result_json.get("total_cost_usd", 0.0)),
            duration_ms=result_json.get("duration_ms", 0),
            permission_denials=denials,
            context=ContextStatus(tokens_used=used, tokens_max=ctx_window, percent=pct,
                                  warning=warning, window_source=window_source),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            log_path=scope.get("log_path"),
            log_line_start=scope.get("start_line"),
            log_line_end=scope.get("end_line"),
            subagent_logs=scope.get("subagent_logs") or [],
            model_substitution=model_substitution,
            safety_signal=safety_signal,
            safety_kind=safety_kind,
            stop_reason=stop_reason_kept,
            background_launches=list(scope.get("background_launches") or []),
            self_woken_completed=list(self_woken.get("completed") or []),
            self_woken_waited_s=waited_s,
            terminal_reason=result_json.get("terminal_reason"),
            backend="claude",
            billing=spec.billing,  # v0.18.0: self-woken turns' footers show API cost too
            notes=connectors.claude_init_notes(spec.mcp_whitelist, result_json.get("_mcp_servers")),
        )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# v0.12.0: the CLI's usage-limit notice, returned as a SUCCESS result's text.
# Observed: "You've hit your session limit · resets 2pm (Europe/London)". The
# middle is left loose ("weekly", "usage", ...) — the exact wording isn't
# contractual. Case-insensitive; tolerates a straight or curly apostrophe.
_USAGE_LIMIT_RE = re.compile(r"you[’']?ve hit your [a-z ]{0,30}limit", re.IGNORECASE)

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
