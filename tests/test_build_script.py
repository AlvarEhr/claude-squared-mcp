"""Offline: the release build refuses stale editable-install metadata.

An editable install's pip metadata (``pip show``, ``importlib.metadata``) is
written at install time and never follows ``__version__``: it read 0.15.0
through the 0.16.x releases. ``--bump`` now refreshes it, and a build stops
when it lags. No pip call is made here; the distribution is faked.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_script", ROOT / "scripts" / "build_and_install_extension.py")
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


class FakeDist:
    def __init__(self, version: str, root: Path | None, editable: bool = True):
        self.version = version
        self._direct = None if root is None else json.dumps(
            {"dir_info": {"editable": editable}, "url": root.resolve().as_uri()})

    def read_text(self, name: str):
        return self._direct if name == "direct_url.json" else None


class BuildScriptMetadataTests(unittest.TestCase):
    def dist(self, *args, **kwargs):
        return patch.object(importlib.metadata, "distribution", return_value=FakeDist(*args, **kwargs))

    def test_stale_metadata_of_this_checkout_stops_the_build(self):
        with self.dist("0.15.0", B.PROJECT_ROOT):
            with self.assertRaises(RuntimeError) as caught:
                B.check_editable_metadata("0.16.2")
        self.assertIn("reports version 0.15.0", str(caught.exception))
        self.assertIn("pip install -e", str(caught.exception))

    def test_matching_metadata_passes(self):
        with self.dist("0.16.2", B.PROJECT_ROOT):
            B.check_editable_metadata("0.16.2")

    def test_an_install_of_another_checkout_is_not_ours_to_judge(self):
        # e.g. building in a feature worktree while main is the editable install
        with self.dist("0.15.0", B.PROJECT_ROOT.parent / "some-other-checkout"):
            B.check_editable_metadata("0.16.2")
        with self.dist("0.15.0", B.PROJECT_ROOT, editable=False):
            B.check_editable_metadata("0.16.2")
        with self.dist("0.15.0", None):
            B.check_editable_metadata("0.16.2")

    def test_a_checkout_path_with_spaces_is_recognised(self):
        # pip percent-encodes the file URL (``file:///C:/my%20code/...``)
        root = B.PROJECT_ROOT.parent / "my code" / "claude squared"
        with patch.object(B, "PROJECT_ROOT", root), self.dist("0.15.0", root):
            self.assertIn("%20", importlib.metadata.distribution("x").read_text("direct_url.json"))
            with self.assertRaises(RuntimeError):
                B.check_editable_metadata("0.16.2")

    def test_a_legacy_install_without_direct_url_is_skipped(self):
        with self.dist("0.15.0", None):
            self.assertEqual(B._editable_install_of_this_checkout(), ("0.15.0", False))
            B.check_editable_metadata("0.16.2")

    def test_not_installed_passes(self):
        with patch.object(importlib.metadata, "distribution",
                          side_effect=importlib.metadata.PackageNotFoundError("claude-squared")):
            B.check_editable_metadata("0.16.2")

    def test_bump_refreshes_only_a_stale_install_of_this_checkout(self):
        with patch("subprocess.run") as run:
            with self.dist("0.16.1", B.PROJECT_ROOT):
                B.refresh_editable_metadata("0.16.2")
            self.assertEqual(run.call_count, 1)
            argv = run.call_args.args[0]
            self.assertEqual(argv[1:5], ["-m", "pip", "install", "-e"])
            self.assertIn("--no-deps", argv)
            with self.dist("0.16.2", B.PROJECT_ROOT):
                B.refresh_editable_metadata("0.16.2")
            with self.dist("0.16.1", B.PROJECT_ROOT.parent / "some-other-checkout"):
                B.refresh_editable_metadata("0.16.2")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
