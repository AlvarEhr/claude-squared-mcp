"""Real FastMCP stdio with an offline worker and recording transport."""
import json
import os
from pathlib import Path
import time
from contextlib import contextmanager

# The harness sets isolated homes before this process imports the server.
from claude_squared import server as S, registry as R, desktop_bridge as B
from claude_squared.models import PairSpec, SendResult

state = Path(os.environ["COMPLETION_SMOKE_STATE"])
if "offline-child" not in R.load().pairs:
    R.add_pair(PairSpec(name="offline-child", session_id=os.environ["COMPLETION_SMOKE_SESSION"], cwd=str(state)))

def build(name, message, **kwargs):
    def work(task_id):
        time.sleep(1)
        return SendResult(name=name, session_id=R.get_pair(name).session_id,
                          response="OFFLINE_WORK_DONE", model_used="fixture", duration_ms=1000)
    return work

class Connection:
    def request(self, method, params, **kwargs):
        (state / "delivered.json").write_text(json.dumps(params), encoding="utf-8")
        return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": params["threadId"]})}]}

@contextmanager
def endpoint():
    if os.environ.get("COMPLETION_SMOKE_HOLD") == "1" and not (state / "allow_delivery").exists():
        raise B.Unavailable("simulated bridge unavailable before dispatcher restart")
    yield Connection()

S._build_send_runner = build
B.desktop_ancestor = lambda: {"build": "fixture"}
B.endpoint = endpoint
S.run()
