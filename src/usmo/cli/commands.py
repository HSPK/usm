"""Built-in command handlers and the dispatch registry.

Each handler shares the uniform ``(args, *, debug, upgrade)`` signature so the
entry point can route to them through :data:`COMMANDS` without special-casing.
"""

from __future__ import annotations

import shutil
import sys
from typing import Callable

import click

from usmo import core
from usmo.core import Scripts

from . import presenters
from .output import console, on_download

CommandHandler = Callable[..., None]


def load_scripts(*, debug: bool, upgrade: bool) -> Scripts:
    """Load the catalog, translating download failures into CLI errors."""
    try:
        return core.load_scripts(
            debug=debug, force_download=upgrade, on_progress=on_download
        )
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc


def cmd_list(
    args: tuple[str, ...], *, debug: bool = False, upgrade: bool = False
) -> None:
    presenters.print_overview(load_scripts(debug=debug, upgrade=upgrade))


def cmd_update(
    args: tuple[str, ...] = (), *, debug: bool = False, upgrade: bool = False
) -> None:
    flags = {a for a in args if a.startswith("-")}
    names = tuple(a for a in args if not a.startswith("-"))
    unknown = flags - {"--all", "-a"}
    if unknown:
        raise click.ClickException(f"unknown option(s): {', '.join(sorted(unknown))}")
    all_scripts = bool(flags & {"--all", "-a"})

    had_cache = core.has_cached_config()
    try:
        changes = core.update_config(on_progress=on_download)
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc

    if names:
        try:
            list(
                core.iter_updates(
                    names=names, refresh_config=False, on_progress=on_download
                )
            )
        except core.UnknownCommand as exc:
            console.print(f"[bold red]Error:[/bold red] Unknown command '{exc.name}'.")
            console.print(f"Available: {', '.join(exc.available)}")
            raise click.ClickException(str(exc)) from exc
        except core.DownloadError as exc:
            raise click.ClickException(str(exc)) from exc
        presenters.print_named_update(names, changes)
        return

    presenters.print_catalog_changes(changes, cold=not had_cache)
    if not all_scripts:
        if changes and had_cache:
            console.print(
                "[dim]Run [bold]usm update --all[/bold] to pull the new scripts.[/dim]"
            )
        return

    try:
        results = list(
            core.iter_updates(names=None, refresh_config=False, on_progress=on_download)
        )
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc
    pulled = [n for n, updated in results if updated]
    if pulled:
        console.print(
            f"[green]✓[/green] Pulled [bold]{len(pulled)}[/bold] cached script(s)."
        )
    else:
        console.print("[dim]No cached scripts to pull.[/dim]")


def cmd_install(
    args: tuple[str, ...], *, debug: bool = False, upgrade: bool = False
) -> None:
    names = [a for a in args if not a.startswith("-")]
    if len(names) != 2:
        raise click.ClickException("usage: usm install <script> <alias>")
    script, alias = names
    scripts = load_scripts(debug=debug, upgrade=False)
    if script not in scripts:
        console.print(f"[bold red]Error:[/bold red] Unknown script '{script}'.")
        console.print(f"Available: {', '.join(sorted(scripts))}")
        raise click.ClickException(f"Unknown script '{script}'.")

    path, status = core.alias_status(alias)
    if status == "foreign":
        console.print(f"[yellow]{path} already exists and is not a usm alias.[/yellow]")
        if not click.confirm("Overwrite it?", default=False):
            raise click.ClickException("aborted.")

    usm_bin = shutil.which("usm") or sys.argv[0]
    core.install_alias(script, alias, usm_bin=usm_bin)
    verb = "Updated" if status == "ours" else "Installed"
    console.print(
        f"[bold green]{verb}:[/bold green] [bold]{alias}[/bold] → usm {script}  "
        f"[dim]({path})[/dim]"
    )
    if not core.local_bin_in_path():
        console.print(
            f"[yellow]note:[/yellow] {core.LOCAL_BIN_DIR} is not on your PATH. "
            "Add it so the alias is found:"
        )
        console.print(
            '  [bold]export PATH="$HOME/.local/bin:$PATH"[/bold] '
            "[dim](append to ~/.bashrc or ~/.zshrc, then restart the shell)[/dim]"
        )


def cmd_uninstall(
    args: tuple[str, ...], *, debug: bool = False, upgrade: bool = False
) -> None:
    names = [a for a in args if not a.startswith("-")]
    if len(names) != 1:
        raise click.ClickException("usage: usm uninstall <alias>")
    alias = names[0]
    try:
        removed = core.uninstall_alias(alias)
    except core.ForeignAlias as exc:
        raise click.ClickException(
            f"{exc.path} is not a usm-managed alias; not removing it."
        ) from exc
    if removed is None:
        console.print(f"[dim]No usm alias '{alias}' in {core.LOCAL_BIN_DIR}.[/dim]")
    else:
        console.print(
            f"[bold green]Removed:[/bold green] {alias} [dim]({removed})[/dim]"
        )


def cmd_clean(
    args: tuple[str, ...], *, debug: bool = False, upgrade: bool = False
) -> None:
    removed = core.clean_cache()
    if removed:
        console.print(
            "[bold green]Removed[/bold green] cached scripts and environments."
        )
    else:
        console.print(
            "[dim]Nothing to clean – no cached scripts or environments.[/dim]"
        )


def cmd_version(
    args: tuple[str, ...], *, debug: bool = False, upgrade: bool = False
) -> None:
    console.print(f"[bold]usm[/bold] version {core.resolve_version()}")


COMMANDS: dict[str, CommandHandler] = {
    "list": cmd_list,
    "update": cmd_update,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "clean": cmd_clean,
    "version": cmd_version,
}
