"""Release-bundle regressions using tiny fixtures, with no dependency install."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bundle_builder", ROOT / "scripts" / "build_and_install_extension.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cs-package-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        # An ancestor called 'build' must not exclude the whole distribution.
        self.root = Path(self.temp.name) / "build"
        self.src = self.root / "src" / "claude_squared"
        self.ext = self.root / "extension"
        self.src.mkdir(parents=True)
        self.ext.mkdir()
        (self.src / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
        (self.src / "server.py").write_text("VALUE = 'current'\n", encoding="utf-8")
        (self.ext / "manifest.json").write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
        (self.ext / "pyproject.toml").write_text('version = "1.2.3"\n', encoding="utf-8")
        for name in ("server/main.py", "server/lib/fastmcp/__init__.py", "server/lib/pydantic/__init__.py", "server/lib/filelock/__init__.py"):
            path = self.ext / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        settings = patch.multiple(builder, PROJECT_ROOT=self.root, SRC_DIR=self.src,
                                  EXT_DIR=self.ext, DIST_DIR=self.root / "dist")
        settings.start()
        self.addCleanup(settings.stop)

    def test_pack_and_stable_copy_match_current_source(self):
        builder.sync_source_into_extension()
        archive = builder.pack("1.2.3")
        stable = builder.refresh_root_bundle(archive)
        self.assertEqual(stable.read_bytes(), archive.read_bytes())
        builder.verify_bundle(stable)
        with zipfile.ZipFile(stable) as bundle:
            self.assertEqual(bundle.read("src/claude_squared/server.py"), (self.src / "server.py").read_bytes())

    def test_stale_staging_does_not_replace_existing_bundle(self):
        builder.sync_source_into_extension()
        archive = builder.pack("1.2.3")
        before = archive.read_bytes()
        (self.src / "server.py").write_text("VALUE = 'newer'\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source differs"):
            builder.pack("1.2.3")
        self.assertEqual(archive.read_bytes(), before)

    def test_version_drift_fails_before_bump(self):
        (self.ext / "manifest.json").write_text('{"version":"1.2.2"}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Version mismatch"):
            builder.bump_version("patch")
        self.assertEqual(builder.source_version(), "1.2.3")
        self.assertEqual(builder.read_version(), "1.2.2")

    def test_missing_vendor_files_fail_verification(self):
        builder.sync_source_into_extension()
        (self.ext / "server/lib/fastmcp/__init__.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing bundled runtime"):
            builder.pack("1.2.3")

    def test_verify_accepts_git_python_line_ending_conversion(self):
        source = self.src / "server.py"
        source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
        builder.sync_source_into_extension()
        archive = builder.pack("1.2.3")
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
        builder.verify_bundle(archive)
        source.write_bytes(b"VALUE = 'different'\r\n")
        with self.assertRaisesRegex(RuntimeError, "source differs"):
            builder.verify_bundle(archive)


if __name__ == "__main__":
    unittest.main()
