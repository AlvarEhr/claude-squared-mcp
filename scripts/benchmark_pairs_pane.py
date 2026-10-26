"""Offline pane-reader timings; fixture homes never touch live pair state."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
scratch = ROOT / ".test-homes"
scratch.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=scratch, prefix="pane-benchmark-") as folder:
    home = Path(folder)
    os.environ["CLAUDE_HOME"] = str(home)
    os.environ["CODEX_HOME"] = str(home / "codex")
    sys.path.insert(0, str(ROOT / "src"))
    from claude_squared._wait_script import install_wait_script
    pairs = home / "pairs"
    install_wait_script(pairs / "wait.py")
    now = datetime.now(timezone.utc).isoformat()
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    write(pairs / "registry.json", {"pairs": {f"pair-{i:02}": {"backend": "claude", "model": "opus"} for i in range(45)}})
    for i in range(45):
        name = f"pair-{i:02}"
        write(pairs / "activity" / name / f"{os.getpid()}.json", {
            "version": 1, "pair": name, "owner_pid": os.getpid(), "state": "live", "backend": "claude",
            "context": {"used": 120000, "window": 1000000, "at": now},
            "turn": {"task_id": name, "started_at": now} if i < 3 else None})
        write(pairs / "async" / f"{name}.json", {"task_id": name, "pair_name": name,
            "status": "running" if i < 3 else "done", "owner_pid": os.getpid(), "started_at": now,
            "finished_at": None if i < 3 else now, "result": {"response": "Fixture reply\n" * 10}})
    timings, cache = [], {}
    for _ in range(7):
        start = time.perf_counter()
        result = subprocess.run([sys.executable, "-B", str(ROOT / "mods/pairs-pane/read_pairs.py"), "--home", str(home)],
            input=json.dumps({"all": True, "cache": cache}), capture_output=True, text=True, timeout=10, check=True)
        timings.append(round((time.perf_counter() - start) * 1000, 2))
        data = json.loads(result.stdout)
        assert not data.get("error"), data
        cache = data.get("cache", {})
    print(json.dumps({"pairs": 45, "samples_ms": timings, "median_ms": statistics.median(timings), "io": data.get("io")}, indent=2))
