"""Rebuild the .mcpb bundle from src/, optionally reinstall into Claude Desktop.

Usage:
    python scripts/build_and_install_extension.py                                # build only
    python scripts/build_and_install_extension.py --install                      # build + install
    python scripts/build_and_install_extension.py --install --clean              # wipe install dir first
    python scripts/build_and_install_extension.py --bump patch --install --clean # release flow

Use --bump (patch|minor|major) when you want Claude Desktop to recognize the bundle
as an update — without a higher version, Claude Desktop's installer may treat the
new .mcpb as already-installed and skip refreshing extension code.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = PROJECT_ROOT / "extension"
SRC_DIR = PROJECT_ROOT / "src" / "claude_squared"
DIST_DIR = PROJECT_ROOT / "dist"

# Extension namespace — must match the ``name`` field in extension/manifest.json
# (kept generic, no author prefix, so forks don't clash with each other).
_EXT_NAMESPACE = "local.claude-squared"


def _claude_extensions_dir() -> Path:
    """Per-OS path to Claude Desktop's Extensions directory.

    Empirically verified on Windows (Claude Desktop installs from the Microsoft
    Store mirror Roaming/Claude/Claude Extensions). macOS + Linux paths follow
    Anthropic's standard "Claude Desktop" install conventions; if those change
    upstream, this is the one place to update.
    """
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "Claude" / "Claude Extensions"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "Claude Extensions"
    # Linux + other POSIX
    return home / ".config" / "Claude" / "Claude Extensions"


INSTALL_DIR = _claude_extensions_dir() / _EXT_NAMESPACE

EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".git", "dist", "build"}
# Vendored deps' dist-info dirs are large and not needed at runtime; pyproject.toml
# is also unused with type=python (deps come from server/lib/).
EXCLUDE_DIR_SUFFIXES = (".dist-info",)
EXCLUDE_FILES = {".gitignore", "uv.lock"}


def _is_excluded(p: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return True
    if any(part.endswith(EXCLUDE_DIR_SUFFIXES) for part in p.parts):
        return True
    if p.name in EXCLUDE_FILES:
        return True
    return False


def sync_source_into_extension() -> None:
    """Mirror current src/claude_squared into extension/src/claude_squared."""
    target = EXT_DIR / "src" / "claude_squared"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SRC_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"  synced source -> {target}")


def read_version() -> str:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest.get("version", "0.0.0")


def bump_version(part: str) -> str:
    """Bump major/minor/patch in extension/manifest.json AND src/__init__.py.

    Returns the new version string.
    """
    if part not in ("patch", "minor", "major"):
        raise ValueError(f"--bump must be patch|minor|major (got {part!r})")

    manifest_path = EXT_DIR / "manifest.json"
    init_path = SRC_DIR / "__init__.py"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cur = manifest.get("version", "0.0.0")
    nums = [int(x) for x in cur.split(".")]
    while len(nums) < 3:
        nums.append(0)
    if part == "major":
        nums = [nums[0] + 1, 0, 0]
    elif part == "minor":
        nums = [nums[0], nums[1] + 1, 0]
    else:
        nums = [nums[0], nums[1], nums[2] + 1]
    new = ".".join(str(n) for n in nums)

    manifest["version"] = new
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  manifest.json: {cur} -> {new}")

    if init_path.exists():
        text = init_path.read_text(encoding="utf-8")
        new_text = text.replace(f'__version__ = "{cur}"', f'__version__ = "{new}"')
        if new_text != text:
            init_path.write_text(new_text, encoding="utf-8")
            print(f"  __init__.py:    {cur} -> {new}")
        else:
            print(f"  __init__.py:    no __version__ line matching '{cur}' found, skipped")

    return new


def pack(version: str) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"claude-squared-{version}.mcpb"
    if out.exists():
        out.unlink()
    files_packed = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in EXT_DIR.rglob("*"):
            if _is_excluded(p):
                continue
            if p.is_file():
                z.write(p, p.relative_to(EXT_DIR))
                files_packed += 1
    size_kb = out.stat().st_size / 1024
    print(f"  packed {files_packed} files -> {out} ({size_kb:.1f} KB)")
    return out


def install(mcpb: Path, clean: bool) -> None:
    """Extract bundle, tolerating files locked by a running Claude Desktop subprocess.

    Vendored deps (server/lib/*.pyd) often stay locked while Claude Desktop runs;
    they only need refreshing when dependencies change, so locked .pyd misses are
    survivable. Manifest + source updates always succeed because .py files aren't
    locked.
    """
    # Notice if any legacy install dirs exist. Two prior renames:
    #   <v0.8.0:  ``local.alvar.claude-pair-mcp`` (fork-neutrality scrub in v0.8.0)
    #   <v0.9.0:  ``local.claude-pair-mcp``        (project rename to claude-squared in v0.9.0)
    # New dir installs alongside; user should manually remove old ones once
    # Desktop has loaded the new bundle to avoid duplicate MCP tool surfaces.
    for legacy_name in ("local.alvar.claude-pair-mcp", "local.claude-pair-mcp"):
        legacy_dir = _claude_extensions_dir() / legacy_name
        if legacy_dir.exists() and legacy_dir != INSTALL_DIR:
            print(f"  NOTE: legacy install dir present at {legacy_dir}")
            print(f"        After verifying the new install at {INSTALL_DIR.name} works,")
            print(f"        you can safely remove the legacy dir to avoid duplicate Desktop MCP tools.")

    if clean and INSTALL_DIR.exists():
        try:
            shutil.rmtree(INSTALL_DIR)
            print(f"  cleaned {INSTALL_DIR}")
        except PermissionError as e:
            print(f"  WARNING: could not fully clean ({e}); continuing with overwrite-in-place")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []
    written = 0
    with zipfile.ZipFile(mcpb) as z:
        for member in z.namelist():
            target = INSTALL_DIR / member
            try:
                z.extract(member, INSTALL_DIR)
                written += 1
            except PermissionError:
                skipped.append(member)
            except OSError as e:
                skipped.append(f"{member} ({e})")
    print(f"  installed -> {INSTALL_DIR} ({written} files written"
          + (f", {len(skipped)} skipped due to locks" if skipped else "")
          + ")")
    if skipped:
        print(f"  Skipped: {skipped[0]}{' (and ' + str(len(skipped)-1) + ' more)' if len(skipped) > 1 else ''}")
        print("  These are usually locked vendored deps — fine if their content didn't change.")
    print("  Restart Claude Desktop to load the new extension code.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--install", action="store_true", help="Install into Claude Extensions dir after build")
    ap.add_argument("--clean", action="store_true", help="Wipe install dir before extracting")
    ap.add_argument("--bump", choices=["patch", "minor", "major"],
                    help="Bump manifest.json + __init__.py version BEFORE syncing/packing. "
                         "Required to get Claude Desktop to recognize an update.")
    args = ap.parse_args()

    if args.bump:
        print("== bump version ==")
        bump_version(args.bump)

    print("== sync source ==")
    sync_source_into_extension()

    version = read_version()
    print(f"== pack v{version} ==")
    mcpb = pack(version)

    if args.install:
        print("== install ==")
        install(mcpb, clean=args.clean)
    else:
        print("Not installing (pass --install to install).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
