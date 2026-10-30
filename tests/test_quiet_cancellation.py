"""A cancelled MCP request must get no response (2026-10-30 disconnect)."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

HOME = tempfile.TemporaryDirectory(prefix="cs-quiet-cancel-")
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
os.environ["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_HOME"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import anyio  # noqa: E402

try:  # mcp 1.x; 2.x reorganised its sessions and the patch then does nothing
    from mcp.shared.session import RequestResponder  # noqa: E402
except ImportError:  # pragma: no cover - depends on the installed SDK
    RequestResponder = None

from claude_squared import server  # noqa: E402,F401  (installs the patch)


class FakeSession:
    def __init__(self):
        self.sent = []

    async def _send_response(self, request_id, response):
        self.sent.append((request_id, response))


@unittest.skipIf(RequestResponder is None, "mcp SDK without mcp.shared.session (2.x)")
class QuietCancellationTests(unittest.TestCase):
    def test_cancel_sends_no_response(self):
        session = FakeSession()
        completed = []

        async def scenario():
            responder = RequestResponder(request_id=7, request_meta=None, request=None, session=session,
                                         on_complete=completed.append)
            with responder:
                await responder.cancel()
            return responder

        responder = anyio.run(scenario)
        self.assertEqual(session.sent, [])
        self.assertEqual(completed, [responder])  # still leaves the in-flight table

    def test_patch_is_idempotent(self):
        before = RequestResponder.cancel
        server._quiet_cancellation()
        self.assertIs(RequestResponder.cancel, before)


if __name__ == "__main__":
    unittest.main()
