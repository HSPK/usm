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

import json
import sys
from pathlib import Path

import click

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from usmo.core import HashChange, sync_manifest  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "scripts" / "_config.json"
BUMP_LEVELS = ("patch", "minor", "major")


def _print_list(config_path: Path) -> int:
    entries = json.loads(config_path.read_text()).get("scripts", {})
    if not entries:
        click.echo("No scripts defined.")
        return 0
    width = max(len(n) for n in entries)
    click.echo(f"  {'name':<{width}}  {'version':<8}  hash")
    for name, entry in sorted(entries.items()):
        ver = entry.get("version", "-")
        digest = (entry.get("hash") or "-")[:23]
        click.echo(f"  {name:<{width}}  {ver:<8}  {digest}")
    return 0


def _report(
    changes: list[HashChange], *, config_path: Path, check_only: bool
) -> int:
    if not changes:
        click.echo(f"{config_path} is in sync.")
        return 0
    verb = "would update" if check_only else "updated"
    click.echo(f"{verb} {len(changes)} script entry(ies):")
    for c in changes:
        click.echo(
            f"  {c.name}: {c.old_version or '-'} -> {c.new_version}  "
            f"({(c.old_hash or '-')[:19]}... -> {c.new_hash[:19]}...)"
        )
    suffix = (
        f"Run 'python dev/bump_version.py' (without --check) and re-stage "
        f"{config_path.name}."
        if check_only
        else f"Re-stage {config_path.name} before committing."
    )
    click.echo(f"\n{suffix}", err=check_only)
    return 1


@click.command(
    context_settings={"show_default": True, "help_option_names": ["-h", "--help"]},
    help=__doc__.splitlines()[0],
    epilog=__doc__[__doc__.index("Examples:"):],
)
@click.argument("names", nargs=-1, metavar="[NAME ...]")
@click.option(
    "--check", is_flag=True,
    help="Verify only; do not rewrite the manifest. Exit 1 on drift.",
)
@click.option(
    "--bump", type=click.Choice(BUMP_LEVELS), default=None,
    help="Force a version bump at this level even if the hash matches. "
         "Without --bump, drift triggers an auto patch bump.",
)
@click.option(
    "--list", "list_mode", is_flag=True,
    help="Print current versions and hashes; do not modify.",
)
@click.option(
    "--config", "config_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=DEFAULT_CONFIG, show_default=True,
    help="Path to _config.json.",
)
@click.option(
    "--scripts-dir",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=None,
    help="Directory holding script files (default: dir of --config).",
)
def cli(
    names: tuple[str, ...],
    check: bool,
    bump: str | None,
    list_mode: bool,
    config_path: Path,
    scripts_dir: Path | None,
) -> None:
    if list_mode:
        sys.exit(_print_list(config_path))
    try:
        changes = sync_manifest(
            config_path, scripts_dir,
            names=names or None,
            bump=bump or "patch",
            force=bool(bump),
            check_only=check,
        )
    except KeyError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
    sys.exit(_report(changes, config_path=config_path, check_only=check))


if __name__ == "__main__":
    cli()
