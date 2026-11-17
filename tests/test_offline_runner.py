"""The offline runner's --list / --only selection (no suites are run here)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_offline_tests", ROOT / "scripts" / "run_offline_tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class SelectionTests(unittest.TestCase):
    names = ["smoke_v094", "smoke_v095", "smoke_v0100", "host_shutdown", "pairs_pane", "pairs_pane_mod"]

    def test_no_patterns_selects_everything_in_order(self):
        self.assertEqual(runner._select(self.names, []), (self.names, []))

    def test_exact_names_globs_and_commas(self):
        selected, unmatched = runner._select(self.names, ["host_shutdown,smoke_v09*", "pairs_pane"])
        self.assertEqual(selected, ["smoke_v094", "smoke_v095", "host_shutdown", "pairs_pane"])
        self.assertEqual(unmatched, [])

    def test_unknown_names_are_reported(self):
        selected, unmatched = runner._select(self.names, ["host_shutdown", "bogus"])
        self.assertEqual(selected, ["host_shutdown"])
        self.assertEqual(unmatched, ["bogus"])

    def test_list_prints_every_suite_and_runs_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(runner.main(["--list"]), 0)
        names = out.getvalue().split()
        self.assertIn("host_shutdown", names)
        self.assertIn("offline_runner", names)
        self.assertEqual(names[-1], runner.PANE_SUITE)
        self.assertEqual(len(names), len(set(names)))

    def test_unknown_only_exits_2_before_running(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(runner.main(["--only", "bogus"]), 2)
        self.assertIn("bogus", err.getvalue())


if __name__ == "__main__":
    unittest.main()
