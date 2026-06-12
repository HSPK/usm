"""Rich rendering for the CLI: tables, overview, help, and update diffs.

Pure presentation — every function here only formats data and writes to the
shared console; no catalog or filesystem logic lives in this module.
"""

from __future__ import annotations

from rich import box
from rich.table import Table

from usmo import core
from usmo.core import Script, Scripts

from .output import console

BUILTIN_HELP: list[tuple[str, str]] = [
    ("list", "List all commands."),
    ("update", "Refresh the catalog; --all or NAME pulls scripts."),
    ("install", "Install a script as an alias in ~/.local/bin."),
    ("uninstall", "Remove an installed alias."),
    ("clean", "Remove the script cache."),
    ("version", "Show usm version."),
]


def scripts_table(scripts: Scripts) -> Table:
    table = Table(
        title="Scripts",
        title_justify="left",
        title_style="bold",
        show_header=True,
        header_style="dim",
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        expand=False,
    )
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("version", style="dim", no_wrap=True)
    table.add_column("description", overflow="fold", min_width=30, ratio=1)
    table.add_column("uv", no_wrap=True, justify="center")
    table.add_column("status", no_wrap=True, justify="right")

    for name in sorted(scripts):
        s = scripts[name]
        status = (
            "[green]●[/green] cached"
            if s.cached_path.exists()
            else "[dim]○ missing[/dim]"
        )
        table.add_row(
            name,
            f"v{s.version}" if s.version else "v?",
            s.description,
            "[cyan]uv[/cyan]" if s.uses_uv else "",
            status,
        )
    return table


def builtin_table() -> Table:
    table = Table(
        title="Built-in",
        title_justify="left",
        title_style="bold",
        show_header=False,
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 2, 0, 0),
        expand=False,
    )
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("help", overflow="fold", min_width=30, ratio=1)
    for name, help_text in BUILTIN_HELP:
        table.add_row(name, help_text)
    return table


def print_overview(scripts: Scripts) -> None:
    console.print(scripts_table(scripts))
    console.print(builtin_table())
    console.print(
        "[dim]Run [bold]usm <name> --help[/bold] for command-specific help.[/dim]"
    )


def print_unknown_command(command: str, scripts: Scripts) -> None:
    console.print(f"[bold red]Error:[/bold red] Unknown command '{command}'.")
    print_overview(scripts)


def print_script_help(script: Script) -> None:
    header = f"[bold]{script.name}[/bold]"
    if script.version:
        header += f" [dim]v{script.version}[/dim]"
    console.print(f"{header}: {script.description}")
    console.print("Usage:")
    console.print(f"  usm {script.name} [ARGS...]")
    if script.requirements:
        console.print(
            "Requirements (installed on first run via [cyan]uv[/cyan]): "
            + ", ".join(script.requirements)
        )
    if script.python:
        console.print(f"Python: {script.python}")


def change_row(c: core.CatalogChange) -> tuple[str, str, str]:
    """(script, version, hash) cells for one catalog change."""
    if c.status == "added":
        return (
            c.name,
            f"[green]new {c.new_version}[/green]",
            f"[green]{core.short_hash(c.new_hash)}[/green]",
        )
    if c.status == "removed":
        return (
            f"[dim]{c.name}[/dim]",
            "[red]removed[/red]",
            f"[dim]{core.short_hash(c.old_hash)}[/dim]",
        )
    return (
        c.name,
        f"{c.old_version} [dim]→[/dim] [cyan]{c.new_version}[/cyan]",
        f"{core.short_hash(c.old_hash)} [dim]→[/dim] {core.short_hash(c.new_hash)}",
    )


def changes_table(title: str) -> Table:
    table = Table(
        title=title,
        title_justify="left",
        title_style="bold",
        header_style="dim",
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 2, 0, 0),
    )
    table.add_column("script", style="bold cyan", no_wrap=True)
    table.add_column("version")
    table.add_column("hash")
    return table


def print_catalog_changes(changes: list[core.CatalogChange], *, cold: bool) -> None:
    if not changes:
        console.print("[green]✓[/green] Catalog is up to date.")
        return
    if cold:
        console.print(
            f"[green]✓[/green] Fetched catalog ([bold]{len(changes)}[/bold] scripts)."
        )
        return
    table = changes_table(f"Catalog changes ({len(changes)})")
    for c in changes:
        table.add_row(*change_row(c))
    console.print(table)


def print_named_update(
    names: tuple[str, ...], changes: list[core.CatalogChange]
) -> None:
    by_name = {c.name: c for c in changes}
    meta = core.read_catalog_meta()
    table = changes_table("Updated")
    for name in names:
        if name in by_name:
            table.add_row(*change_row(by_name[name]))
        else:
            version, h = meta.get(name, (None, None))
            table.add_row(name, version or "?", core.short_hash(h))
    console.print(table)
