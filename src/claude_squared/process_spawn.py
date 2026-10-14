"""Windows visibility policy for headless CLI processes.

Tool-capable roots need a hidden console for ordinary console descendants to
inherit. CREATE_NO_WINDOW suppresses only the root's console; its children may
otherwise allocate visible consoles. Short leaf queries and taskkill need no
console. This does not prevent a child from explicitly creating/showing a new
window. POSIX launch options pass through unchanged.
"""

from __future__ import annotations

import copy
import os
import subprocess
from typing import Any


def hidden_process_kwargs(kwargs: dict[str, Any] | None = None, *,
                          console: bool = True) -> dict[str, Any]:
    """Copy launch options and hide either an inheritable console or a leaf."""
    result = dict(kwargs or {})
    if os.name != "nt":
        return result
    flags = result.get("creationflags", 0)
    # These choices are mutually exclusive. NEW_PROCESS_GROUP is ignored with
    # NEW_CONSOLE; Windows cancellation uses PID-scoped taskkill /F /T, not
    # GenerateConsoleCtrlEvent. Keep unrelated flags (e.g. priority) intact.
    flags &= ~(subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW
               | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    result["creationflags"] = flags | (subprocess.CREATE_NEW_CONSOLE if console
                                       else subprocess.CREATE_NO_WINDOW)
    startupinfo = result.get("startupinfo")
    startupinfo = copy.copy(startupinfo) if startupinfo is not None else subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    result["startupinfo"] = startupinfo
    return result
