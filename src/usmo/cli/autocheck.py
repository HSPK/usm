"""The opportunistic per-script update probe shown before running a command."""

from __future__ import annotations

import sys

import click

from usmo import core

from . import commands
from .output import console

# Commands that should never trigger an auto-check (cheap built-ins or the
# update flow itself).
AUTO_CHECK_SKIP_COMMANDS = {"update", "clean", "version", "install", "uninstall"}


def maybe_auto_check(command: str | None, debug: bool) -> None:
    """Probe the remote config for per-script version bumps; prompt to update."""
    if debug or command in AUTO_CHECK_SKIP_COMMANDS:
        return
    try:
        diffs = core.check_for_update()
    except Exception:
        return  # never fail the user's command on auto-check
    if not diffs:
        return
    console.print(
        f"[bold yellow]usm:[/bold yellow] {len(diffs)} script(s) have updates available:"
    )
    for d in diffs:
        local = d.local_version or "[dim]missing[/dim]"
        remote = d.remote_version or "[dim]removed[/dim]"
        console.print(f"  [bold]{d.name}[/bold]: {local} → [cyan]{remote}[/cyan]")
    if not sys.stdin.isatty():
        console.print("[dim]      Run 'usm update --all' to refresh.[/dim]")
        return
    try:
        proceed = click.confirm("Pull the updated scripts now?", default=False)
    except click.Abort:
        return
    if proceed:
        try:
            commands.cmd_update(("--all",))
        except Exception as exc:
            console.print(f"[yellow]Update failed:[/yellow] {exc}")
