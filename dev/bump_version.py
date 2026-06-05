#!/usr/bin/env python3
"""Bump per-script versions and hashes in ``scripts/_config.json``.

Default mode walks every entry, hashes the referenced file, and bumps the
patch version whenever drift is detected. Used by pre-commit, but also
runnable on demand to release a specific tool at a higher level.

Examples:
  python dev/bump_version.py                       # auto-sync all (patch on drift)
  python dev/bump_version.py --check               # verify only; exit 1 on drift
  python dev/bump_version.py openai-proxy          # only auto-sync that one
  python dev/bump_version.py openai-proxy --bump minor
                                                   # explicit minor bump
  python dev/bump_version.py --list                # show current versions/hashes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from usmo.core import HashChange, sync_manifest  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "scripts" / "_config.json"
BUMP_LEVELS = ("patch", "minor", "major")


def cmd_list(config_path: Path) -> int:
    entries = json.loads(config_path.read_text()).get("scripts", {})
    if not entries:
        print("No scripts defined.")
        return 0
    width = max(len(n) for n in entries)
    print(f"  {'name':<{width}}  {'version':<8}  hash")
    for name, entry in sorted(entries.items()):
        ver = entry.get("version", "-")
        digest = (entry.get("hash") or "-")[:23]
        print(f"  {name:<{width}}  {ver:<8}  {digest}")
    return 0


def report(changes: list[HashChange], *, config_path: Path, check_only: bool) -> int:
    if not changes:
        print(f"{config_path} is in sync.")
        return 0
    verb = "would update" if check_only else "updated"
    print(f"{verb} {len(changes)} script entry(ies):")
    for c in changes:
        print(
            f"  {c.name}: {c.old_version or '-'} -> {c.new_version}  "
            f"({(c.old_hash or '-')[:19]}... -> {c.new_hash[:19]}...)"
        )
    suffix = (
        "Run 'python dev/bump_version.py' (without --check) and re-stage "
        f"{config_path.name}."
        if check_only
        else f"Re-stage {config_path.name} before committing."
    )
    print(f"\n{suffix}", file=sys.stderr if check_only else sys.stdout)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__[__doc__.index("Examples:"):],
    )
    p.add_argument(
        "names", nargs="*", metavar="NAME",
        help="Script name(s) to operate on. Empty = all entries.",
    )
    p.add_argument(
        "--check", action="store_true",
        help="Verify only; do not rewrite the manifest. Exit 1 on drift.",
    )
    p.add_argument(
        "--bump", choices=BUMP_LEVELS, default=None,
        help=(
            "Force a version bump at this level even if the hash matches. "
            "Without --bump, drift triggers an auto patch bump."
        ),
    )
    p.add_argument(
        "--list", action="store_true",
        help="Print current versions and hashes; do not modify.",
    )
    p.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help=f"Path to _config.json (default: {DEFAULT_CONFIG.relative_to(REPO_ROOT)}).",
    )
    p.add_argument(
        "--scripts-dir", type=Path, default=None,
        help="Directory holding script files (default: dir of --config).",
    )
    args = p.parse_args(argv)

    if args.list:
        return cmd_list(args.config)

    try:
        changes = sync_manifest(
            args.config,
            args.scripts_dir,
            names=args.names or None,
            bump=args.bump or "patch",
            force=bool(args.bump),
            check_only=args.check,
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return report(changes, config_path=args.config, check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
