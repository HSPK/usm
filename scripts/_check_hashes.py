#!/usr/bin/env python3
"""Pre-commit hook: verify each script's hash and bump version when changed.

Walks every entry in ``scripts/_config.json``, computes the sha256 of the
referenced file, and compares it with the declared hash. If they differ
(or version/hash is missing), the patch version is bumped and the hash is
written back into the manifest.

Usage:
  python scripts/_check_hashes.py            # rewrite manifest in place
  python scripts/_check_hashes.py --check    # exit 1 if anything would change

Exit codes:
  0  Manifest is in sync (or was just synced in non-check mode).
  1  Drift detected. In write mode this means the manifest was updated and
     should be re-staged; in --check mode this is a verification failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from usmo.core import sync_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify only; do not rewrite the manifest. Exit 1 on drift.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "scripts" / "_config.json",
        help="Path to _config.json (default: scripts/_config.json).",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=None,
        help="Directory holding script files (default: dir of --config).",
    )
    args = parser.parse_args(argv)

    changes = sync_manifest(
        args.config, args.scripts_dir, check_only=args.check
    )
    if not changes:
        print("scripts/_config.json is in sync.")
        return 0

    verb = "would update" if args.check else "updated"
    print(f"{verb} {len(changes)} script entry(ies):")
    for c in changes:
        print(
            f"  {c.name}: "
            f"{c.old_version or '-'} -> {c.new_version}  "
            f"({(c.old_hash or '-')[:19]}... -> {c.new_hash[:19]}...)"
        )
    if args.check:
        print(
            "\nRun 'python scripts/_check_hashes.py' (without --check) and "
            "re-stage scripts/_config.json.",
            file=sys.stderr,
        )
        return 1
    print("\nRe-stage scripts/_config.json before committing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
