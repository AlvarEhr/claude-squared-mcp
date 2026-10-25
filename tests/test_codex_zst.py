"""Offline tests for rollouts the Codex app compressed (``….jsonl.zst``,
found 2026-10-25): the resolver and ``session_exists``, the frame-size
header walk, every reader refusing to parse compressed bytes, send/compact
re-resolving after Codex restores the plain file, the sampler, the
read-only views' note, rewind restoring first (app-server ``thread/resume``),
archive copies, adopt, the CLI, and the backend-aware SessionMissing hint.

Nothing here spawns ``codex``. The .zst fixtures are real zstd frames built
from RAW blocks, so their bytes still contain the JSONL lines verbatim — a
reader that parsed them line by line would "find" records in them."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Isolation BEFORE importing the package: importing the server runs an
# orphan sweep over the pairs dir, and the user may have live pairs.
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-zst-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import __main__ as M  # noqa: E402
from claude_squared import activity as A  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared import transcript as T  # noqa: E402
from claude_squared.adapters import codex as CX  # noqa: E402
from claude_squared.errors import CLIError, PairError, SessionMissing  # noqa: E402
from claude_squared.models import PairSpec  # noqa: E402

TID = "01a17430-f712-7f03-b652-69f8f2aeed06"


def tool(value):
    return getattr(value, "fn", value)


def write(path: Path, text: str) -> None:
    """Codex writes LF; ``write_text`` would emit CRLF on Windows."""
    path.write_bytes(text.encode("utf-8"))


def zst_frame(content: bytes, *, single: bool = True, fcs: bool = True, fcs_bytes: int = 4,
              checksum: bool = False, block: int = 1000, rle: bool = False) -> bytes:
    """One zstd frame (RFC 8878) of RAW blocks — or one RLE block when
    ``rle`` (content must then be one repeated byte). ``single`` + 4-byte
    FCS is the header Codex 0.164 wrote (FHD 0xa0, 2026-10-25 probe)."""
    flag = {0: 0, 1: 0, 2: 1, 4: 2, 8: 3}[fcs_bytes] if fcs else 0
    fhd = (flag << 6) | (int(single) << 5) | (int(checksum) << 2)
    out = bytearray(b"\x28\xb5\x2f\xfd") + bytes([fhd])
    if not single:
        out.append(0x58)  # window descriptor
    if fcs:
        n = len(content) - (256 if fcs_bytes == 2 else 0)
        out += n.to_bytes(fcs_bytes, "little")
    if rle:
        out += ((len(content) << 3) | (1 << 1) | 1).to_bytes(3, "little") + content[:1]
    else:
        chunks = [content[i:i + block] for i in range(0, len(content), block)] or [b""]
        for i, chunk in enumerate(chunks):
            out += ((len(chunk) << 3) | int(i == len(chunks) - 1)).to_bytes(3, "little") + chunk
    if checksum:
        out += b"\0\0\0\0"
    return bytes(out)


def rec(kind, payload=None, **extra):
    d = {"timestamp": extra.pop("ts", "2026-10-18T10:00:00.000Z"), "type": kind}
    if payload is not None:
        d["payload"] = payload
    d.update(extra)
    return json.dumps(d)


def token_count(inp, total=None):
    last = {"input_tokens": inp, "output_tokens": 5}
    if total is not None:
        last["total_tokens"] = total
    return rec("event_msg", {"type": "token_count", "info": {
        "last_token_usage": last, "total_token_usage": {"input_tokens": 1_000_000},
        "model_context_window": 258_400}, "rate_limits": None})


def user_turn(text, ts="2026-10-18T10:00:00.000Z"):
    return [rec("turn_context", {"cwd": "x"}, ts=ts),
            rec("response_item", {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": text}]}),
            rec("response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": f"re: {text}"}]}),
            token_count(40_000)]


ROLLOUT = "\n".join([rec("session_meta", {"id": TID})] + user_turn("first") + user_turn("second")) + "\n"


class ZstCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.codex_home = self.root / "codex"
        self.day = self.codex_home / "sessions" / "2026" / "10" / "18"
        self.day.mkdir(parents=True)
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.root / "claude"), "CODEX_HOME": str(self.codex_home)}))
        self.addCleanup(self.retire_activity)
        self.plain = self.day / f"rollout-2026-10-18T10-00-00-{TID}.jsonl"
        self.zst = CX.compressed_sibling(self.plain)
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected Popen")))
        runtime = SimpleNamespace(get_or_none=Mock(return_value=None), evict=Mock(return_value=[]))
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=runtime))

    def retire_activity(self):
        home = str(self.root / "claude" / "pairs")
        with A._WRITERS_LOCK:
            writers = [A._WRITERS.pop(key) for key in list(A._WRITERS) if key[0] == home]
        for writer in writers:
            with writer._io_lock, writer._lock:
                writer._forgotten = True

    def state_db(self, path: Path | str | None):
        con = sqlite3.connect(self.codex_home / "state_5.sqlite")
        with con:
            con.execute("create table threads (id text primary key, rollout_path text)")
            if path is not None:
                con.execute("insert into threads values (?, ?)", (TID, str(path)))
        con.close()

    def compress(self, content: str = ROLLOUT, **kw) -> bytes:
        """What the Codex app leaves: only the .zst; sqlite names the .jsonl."""
        data = zst_frame(content.encode("utf-8"), **kw)
        self.zst.write_bytes(data)
        if self.plain.exists():
            self.plain.unlink()
        return data

    def restore(self, extra: str = "") -> None:
        """What Codex does at resume: the plain file back, the .zst gone."""
        write(self.plain, ROLLOUT + extra)
        self.zst.unlink()

    def spec(self, name="z", **changes):
        data = dict(name=name, session_id=TID, backend="codex", model="gpt-6-luna",
                    permission_mode="read-only", effort="low", cwd=str(self.root))
        data.update(changes)
        return PairSpec(**data)


# ---------------------------------------------------------------------------
# The header walk
# ---------------------------------------------------------------------------

class FrameSizeTests(ZstCase):
    def size_of(self, data: bytes):
        p = self.root / "x.zst"
        p.write_bytes(data)
        return CX.zstd_content_size(p)

    def test_codex_style_single_segment_frame(self):
        data = zst_frame(b"a" * 5000)
        self.assertEqual(data[4], 0xA0)  # the header byte Codex 0.164 wrote
        self.assertEqual(self.size_of(data), 5000)

    def test_windowed_frame_and_every_size_field_width(self):
        self.assertEqual(self.size_of(zst_frame(b"b" * 3000, single=False)), 3000)
        self.assertEqual(self.size_of(zst_frame(b"c" * 300, single=False, fcs_bytes=2)), 300)  # stored as -256
        self.assertEqual(self.size_of(zst_frame(b"d" * 200, single=True, fcs_bytes=1)), 200)
        self.assertEqual(self.size_of(zst_frame(b"e" * 70, single=False, fcs_bytes=8)), 70)

    def test_rle_checksum_skippable_and_several_frames(self):
        skippable = (0x184D2A53).to_bytes(4, "little") + (3).to_bytes(4, "little") + b"xyz"
        data = (zst_frame(b"f" * 900, checksum=True, block=100) + skippable
                + zst_frame(b"g" * 400, rle=True) + zst_frame(b"h" * 10, single=False))
        self.assertEqual(self.size_of(data), 1310)

    def test_unknown_or_unparsable_is_none(self):
        self.assertIsNone(self.size_of(zst_frame(b"i" * 50, single=False, fcs=False)))  # size not recorded
        self.assertIsNone(self.size_of(b""))
        self.assertIsNone(self.size_of(b'{"type":"session_meta"}\n'))
        self.assertIsNone(self.size_of(zst_frame(b"j" * 3000)[:-10]))  # truncated block
        self.assertIsNone(CX.zstd_content_size(self.root / "missing.zst"))

    def test_rollout_size(self):
        write(self.plain, ROLLOUT)
        self.assertEqual(CX.rollout_size(self.plain), len(ROLLOUT.encode()))
        self.compress()
        self.assertEqual(CX.rollout_size(self.zst), len(ROLLOUT.encode()))
        self.assertIsNone(CX.rollout_size(None))
        self.assertIsNone(CX.rollout_size(self.plain))  # gone


# ---------------------------------------------------------------------------
# Resolution + session_exists + SessionMissing
# ---------------------------------------------------------------------------

class ResolverTests(ZstCase):
    def test_sqlite_path_falls_back_to_the_zst_sibling(self):
        self.state_db("\\\\?\\" + str(self.plain) if os.name == "nt" else self.plain)
        self.compress()
        p = CX.rollout_path_for(TID)
        self.assertTrue(CX.is_compressed_rollout(p), p)
        self.assertTrue(CX.CodexAdapter().session_exists(self.spec()))
        self.assertEqual(CX.compressed_rollout_note(self.spec()), CX.COMPRESSED_NOTE)
        self.restore()
        self.assertFalse(CX.is_compressed_rollout(CX.rollout_path_for(TID)))
        self.assertIsNone(CX.compressed_rollout_note(self.spec()))

    def test_sibling_outside_the_sessions_tree(self):
        # `codex archive` moves the rollout to archived_sessions/ and updates
        # sqlite (verified 2026-10-25); the glob never looks there, so only
        # the sibling check finds a compressed archived rollout.
        archived = self.codex_home / "archived_sessions" / self.plain.name
        archived.parent.mkdir()
        self.state_db(archived)
        CX.compressed_sibling(archived).write_bytes(zst_frame(ROLLOUT.encode("utf-8")))
        self.assertEqual(CX.rollout_path_for(TID), CX.compressed_sibling(archived))

    def test_glob_fallback_finds_the_zst_and_prefers_plain(self):
        self.state_db(None)
        self.compress()
        self.assertEqual(CX.rollout_path_for(TID), self.zst)
        write(self.plain, ROLLOUT)  # both present: plain wins
        self.assertEqual(CX.rollout_path_for(TID), self.plain)

    def test_missing_is_none_and_the_hint_names_codex(self):
        self.state_db(self.plain)
        self.assertIsNone(CX.rollout_path_for(TID))
        adapter = CX.CodexAdapter()
        with self.assertRaises(SessionMissing) as cm:
            adapter.send(self.spec(), "hi")
        msg = str(cm.exception)
        self.assertIn("Codex thread", msg)
        self.assertIn(str(self.codex_home / "sessions"), msg)
        self.assertNotIn(".claude", msg)
        self.assertIn("valid thread id", msg)
        claude = str(SessionMissing("c", "sid"))
        self.assertIn("~/.claude/projects/", claude)
        self.assertIn("valid session_id", claude)


# ---------------------------------------------------------------------------
# Readers never parse compressed bytes
# ---------------------------------------------------------------------------

class ReaderTests(ZstCase):
    def test_every_reader_refuses_the_compressed_file(self):
        self.compress(ROLLOUT + rec("compacted", {"replacement_history": ["x" * 400]}) + "\n")
        raw = self.zst.read_bytes()
        self.assertIn(b'"token_count"', raw)  # the raw blocks really carry the lines
        self.assertIsNone(CX.read_last_token_count(self.zst))
        self.assertEqual(CX._turn_contexts_since(self.zst, 0), [])
        self.assertEqual(CX.tail_turns_codex(self.zst), [])
        self.assertEqual(CX.list_turn_points_codex(self.zst), [])
        self.assertEqual(CX._estimate_compacted_tokens(self.zst), 0)
        self.assertIsNone(CX._token_count_before_last_compaction(self.zst))
        self.assertEqual(T.tail_turns(self.zst), [])  # no Claude-parser garbage either
        with self.assertRaises(CLIError):
            CX.truncate_rollout_before_line(self.zst, 1)
        self.assertTrue(self.zst.exists())
        with self.assertRaises(CLIError) as cm:
            CX.CodexAdapter().context(self.spec())
        self.assertIn(CX.COMPRESSED_NOTE, str(cm.exception))

    def test_turn_contexts_without_an_offset_filter_by_time(self):
        write(self.plain, "\n".join([
            rec("turn_context", {"n": "old"}, ts="2026-10-18T10:00:00.000Z"),
            rec("turn_context", {"n": "new"}, ts="2026-10-25T17:55:02.569Z"),
            rec("turn_context", {"n": "odd-fraction"}, ts="2026-10-25T17:55:03.5Z"),
            rec("turn_context", {"n": "no-stamp"}, ts=""),
        ]) + "\n")
        cut = CX._iso_epoch("2026-10-25T17:55:00.000Z")
        got = [d["n"] for d in CX._turn_contexts_since(self.plain, None, not_before=cut)]
        self.assertEqual(got, ["new", "odd-fraction"])
        self.assertEqual(len(CX._turn_contexts_since(self.plain, 0, not_before=cut)), 4)  # offset wins

    def test_pre_compaction_token_count(self):
        write(self.plain, "\n".join([
            token_count(10_000), token_count(90_000), rec("compacted", {"replacement_history": []}),
            token_count(0, total=12_000), token_count(15_000),
            token_count(30_000), rec("compacted", {"replacement_history": []}), token_count(0, total=9_000),
        ]) + "\n")
        before = CX._token_count_before_last_compaction(self.plain)
        self.assertEqual(before["info"]["last_token_usage"]["input_tokens"], 30_000)
        write(self.plain, token_count(1) + "\n")
        self.assertIsNone(CX._token_count_before_last_compaction(self.plain))


# ---------------------------------------------------------------------------
# send / compact / sampler: Codex restores, we re-resolve
# ---------------------------------------------------------------------------

class SendCompactTests(ZstCase):
    def setUp(self):
        super().setUp()
        self.state_db(self.plain)
        self.compress()
        self.stack.enter_context(patch.object(CX, "codex_executable", return_value="codex"))
        self.stack.enter_context(patch.object(CX, "config_readd_args", return_value=[]))
        self.stack.enter_context(patch.object(CX, "resolve_codex_model", return_value=("gpt-6-luna", None)))
        self.stack.enter_context(patch.object(CX, "context_windows", return_value=(258_400, 828_400)))

    def test_send_goes_through_and_reads_the_restored_rollout(self):
        seen = {}

        def fake_run(args, run_spec, **kw):
            seen["args"] = args
            self.restore(token_count(123_000) + "\n")
            return ([{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                     {"type": "turn.completed", "usage": {"input_tokens": 5}}], "", 0, 5)

        with patch.object(CX.CodexAdapter, "_exec_args", return_value=["codex", "exec"]), \
                patch.object(CX.CodexAdapter, "_run", side_effect=fake_run):
            result = CX.CodexAdapter().send(self.spec(), "hi")
        self.assertEqual(seen["args"][-3:], ["resume", TID, "-"])
        self.assertEqual(result.response, "ok")
        self.assertEqual(result.context.tokens_used, 123_000)  # from the plain file, not turn usage

    def test_compact_reports_pre_and_post_from_the_restored_rollout(self):
        def fake_compact(spec, **kw):
            self.restore(rec("compacted", {"replacement_history": []}) + "\n" + token_count(0, total=21_000) + "\n")

        with patch.object(CX.CodexAdapter, "_appserver_compact", side_effect=fake_compact):
            result = CX.CodexAdapter().compact(self.spec())
        self.assertEqual((result.pre_tokens, result.post_tokens), (40_000, 21_000))

    def test_compact_on_a_plain_rollout_is_unchanged(self):
        self.restore()

        def fake_compact(spec, **kw):
            with open(self.plain, "a", encoding="utf-8", newline="") as f:
                f.write(rec("compacted", {"replacement_history": []}) + "\n" + token_count(0, total=7_000) + "\n")

        with patch.object(CX.CodexAdapter, "_appserver_compact", side_effect=fake_compact):
            result = CX.CodexAdapter().compact(self.spec())
        self.assertEqual((result.pre_tokens, result.post_tokens), (40_000, 7_000))

    def test_sampler_never_keeps_the_compressed_path(self):
        R.add_pair(self.spec())
        turn = CX._new_codex_activity(self.spec(), None)
        self.addCleanup(turn.close)
        turn._sample(True)
        self.assertIsNone(turn.used)
        self.restore(token_count(77_000) + "\n")
        turn._sample(True)
        self.assertEqual(turn._rollout, self.plain)
        self.assertEqual(turn.used, 77_000)


# ---------------------------------------------------------------------------
# restore_rollout (app-server thread/resume)
# ---------------------------------------------------------------------------

class FakeClient:
    instances: list = []

    def __init__(self, args, *, cwd, timeout_seconds, kill, **kw):
        self.args, self.cwd, self.calls = args, cwd, []
        self.proc = SimpleNamespace(pid=4242)
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def initialize(self):
        self.calls.append(("initialize", None))

    def request(self, method, params):
        self.calls.append((method, params))
        if FakeClient.on_resume:
            FakeClient.on_resume()
        return {"thread": {"id": params["threadId"]}}


class RestoreTests(ZstCase):
    def setUp(self):
        super().setUp()
        self.state_db(self.plain)
        self.compress()
        FakeClient.instances = []
        FakeClient.on_resume = None
        self.stack.enter_context(patch.object(CX, "AppServerClient", FakeClient))
        self.stack.enter_context(patch.object(CX, "codex_executable", return_value="codex"))
        self.stack.enter_context(patch.object(CX, "config_readd_args", return_value=["-c", "x=1"]))
        self.stack.enter_context(patch.object(CX, "resolve_codex_model", return_value=("gpt-6-luna", None)))

    def test_thread_resume_alone_restores_and_says_so(self):
        FakeClient.on_resume = lambda: self.restore(rec("event_msg", {"type": "thread_settings_applied"}) + "\n")
        spec = self.spec(codex_instructions={"mode": "developer", "sha256": "x", "chars": 3},
                         backend_options={"config": {"developer_instructions": "no"}})
        note = CX.CodexAdapter().restore_rollout(spec)
        self.assertIn("thread_settings_applied", note)
        client = FakeClient.instances[-1]
        self.assertEqual(client.args, ["codex", "app-server", "-c", "x=1"])
        method, params = client.calls[-1]
        self.assertEqual(method, "thread/resume")
        self.assertEqual((params["threadId"], params["sandbox"], params["excludeTurns"]), (TID, "read-only", True))
        self.assertEqual(params["config"].get("mcp_servers"), {})
        self.assertNotIn("developer_instructions", params["config"])
        self.assertNotIn("developerInstructions", params)  # a plain resume, as probed
        self.assertEqual(len(client.calls), 2)  # initialize + resume: no turn, no compaction
        self.assertIn("ROLLOUT RESTORED", (R.logs_dir() / "z" / "main.log").read_text(encoding="utf-8"))
        self.assertNotIn("z", CX._INFLIGHT)

    def test_no_plain_file_is_an_error(self):
        with patch.object(CX, "_RESTORE_WAIT_SECONDS", 0.3):
            with self.assertRaises(CLIError):
                CX.CodexAdapter().restore_rollout(self.spec())
        self.assertNotIn("z", CX._INFLIGHT)


# ---------------------------------------------------------------------------
# Server tools + CLI
# ---------------------------------------------------------------------------

class ToolTests(ZstCase):
    def setUp(self):
        super().setUp()
        self.state_db(self.plain)
        self.compress()
        R.add_pair(self.spec())

    def test_read_only_views_say_compressed(self):
        out = tool(S.pair_transcript)("z")
        self.assertIn(CX.COMPRESSED_NOTE, out)
        self.assertIn(CX.COMPRESSED_NOTE, tool(S.pair_context)("z"))
        self.assertIn(CX.COMPRESSED_NOTE, tool(S.pair_context)("z", verbose=True))
        info = tool(S.pair_info)("z")
        self.assertIn("transcript: ok, compressed", info)
        self.assertNotIn("MISSING", info)
        self.assertTrue(json.loads(tool(S.pair_info)("z", verbose=True))["transcript_exists"])
        status = tool(S.pair_status)("z")
        self.assertIn(f"rollout: {CX.COMPRESSED_NOTE}", status)
        self.assertTrue(json.loads(tool(S.pair_status)("z", verbose=True))["rollout_compressed"])
        self.restore()
        self.assertNotIn("compressed", tool(S.pair_info)("z"))
        self.assertNotIn("rollout:", tool(S.pair_status)("z"))
        self.assertIn("re: second", tool(S.pair_transcript)("z"))

    def test_cli_views_say_compressed(self):
        for cmd in (M._cmd_transcript, M._cmd_info, M._cmd_context):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cmd(["z"]), 0)
            self.assertIn(CX.COMPRESSED_NOTE, buf.getvalue(), cmd.__name__)
            self.assertNotIn("no turns", buf.getvalue(), cmd.__name__)

    def test_forget_archives_the_zst_as_is(self):
        data = self.zst.read_bytes()
        out = tool(S.pair_forget)("z")
        self.assertIn("copied as-is", out)
        archived = list(R.archive_dir().glob("z-*.jsonl.zst"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), data)
        self.assertEqual(list(R.archive_dir().glob("z-*.jsonl")), [])

    def test_archive_helper_plain_and_compressed(self):
        self.assertTrue(S._archive_transcript(self.zst, "a").endswith("so the copy is zstd)"))
        self.restore()
        self.assertEqual(S._archive_transcript(self.plain, "b"), str(R.archive_dir() / "b.jsonl"))

    def test_adopt_accepts_a_compressed_thread(self):
        tool(S.pair_forget)("z", archive=False)
        out = tool(S.pair_adopt)("z2", TID, backend="codex", model="gpt-6-luna", cwd=str(self.root))
        self.assertIn("Adopted codex thread", out)
        self.assertIn(CX.COMPRESSED_NOTE, out)

    def test_rewind_points_and_rewind_restore_first(self):
        calls = []

        def fake_restore(adapter_self, spec, timeout_seconds=120):
            calls.append(spec.name)
            self.restore(rec("event_msg", {"type": "thread_settings_applied"}) + "\n")
            return "restored-note"

        with patch.object(CX.CodexAdapter, "restore_rollout", fake_restore), \
                patch.object(CX, "_history_db_path", return_value=None):
            points = tool(S.pair_rewind_points)("z")
            self.assertIn("2 rewind point(s)", points)
            self.assertIn("restored-note", points)
            self.assertEqual(calls, ["z"])
            # Compressed again (another idle week): rewind restores, then cuts.
            self.compress(self.plain.read_text(encoding="utf-8"))
            out = tool(S.pair_rewind)("z", to_point=2)
        self.assertEqual(calls, ["z", "z"])
        self.assertIn("restored-note", out)
        self.assertFalse(self.zst.exists())
        kept = self.plain.read_text(encoding="utf-8")
        self.assertIn("first", kept)
        self.assertNotIn("second", kept)
        self.assertEqual(len(list(R.archive_dir().glob("z-*-prerewind.jsonl"))), 1)  # plain by then

    def test_rewind_points_on_a_plain_rollout_never_restores(self):
        self.restore()
        with patch.object(CX.CodexAdapter, "restore_rollout", side_effect=AssertionError("restored")):
            self.assertIn("2 rewind point(s)", tool(S.pair_rewind_points)("z"))

    def test_a_failed_restore_refuses_the_rewind_untouched(self):
        with patch.object(CX.CodexAdapter, "restore_rollout", side_effect=CLIError("boom")):
            with self.assertRaises(PairError) as cm:
                tool(S.pair_rewind)("z", to_point=1)
        self.assertIn(CX.COMPRESSED_NOTE, str(cm.exception))
        self.assertIn("boom", str(cm.exception))
        self.assertTrue(self.zst.exists())


if __name__ == "__main__":
    unittest.main()
