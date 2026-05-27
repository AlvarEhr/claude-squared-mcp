"""Custom exceptions raised by the pair MCP. All map to actionable error messages."""


class PairError(Exception):
    """Base for all pair MCP errors."""


class PairNotFound(PairError):
    def __init__(self, name: str):
        super().__init__(
            f"No pair named '{name}' in registry. "
            f"Use pair_list to see available pairs, or pair_create to make one."
        )
        self.name = name


class PairAlreadyExists(PairError):
    def __init__(self, name: str):
        super().__init__(
            f"A pair named '{name}' already exists. "
            f"Use pair_update to modify it, pair_forget then pair_create to recreate, "
            f"or pick a different name."
        )
        self.name = name


class SessionMissing(PairError):
    def __init__(self, name: str, session_id: str):
        super().__init__(
            f"Pair '{name}' references session '{session_id}' but its transcript was not found "
            f"under ~/.claude/projects/. The session may have been deleted manually. "
            f"Run pair_forget('{name}') and pair_create to start fresh, or pair_adopt with a valid session_id."
        )
        self.name = name
        self.session_id = session_id


class CLIError(PairError):
    def __init__(self, message: str, stderr: str | None = None, exit_code: int | None = None):
        body = message
        if exit_code is not None:
            body += f" (exit {exit_code})"
        if stderr:
            body += f"\nstderr: {stderr.strip()[:1000]}"
        super().__init__(body)


class CommandTimeout(PairError):
    """Raised when ``hard_timeout_seconds`` auto-kills the underlying claude
    operation. NOT raised by sync-wait expiration — that path returns an async
    handle ("still running") rather than failing.

    By the time an agent reads this error, the work IS gone (the subprocess was
    killed). Recovery is to re-fire the same send with a larger or no ceiling.
    """

    def __init__(self, name: str, seconds: int):
        super().__init__(
            f"Pair '{name}' was auto-killed after {seconds}s by the "
            f"hard_timeout_seconds ceiling — the underlying claude operation "
            f"didn't finish in time. The pair runtime itself is still healthy; "
            f"only this turn was killed. To recover: re-fire the same send with "
            f"a larger hard_timeout_seconds (or omit it / pass None for no "
            f"ceiling — recommended for most uses; long Opus + sub-agent runs "
            f"can legitimately take 30+ minutes)."
        )
        self.name = name
        self.seconds = seconds
