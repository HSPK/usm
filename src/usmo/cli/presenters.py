"""Rich rendering for the CLI: tables, the landing page, help, update diffs.

Pure presentation — every function here only formats data and writes to the
shared console; no catalog or filesystem logic lives in this module.

``rich`` is imported inside the functions that need it so importing this
module stays free (see :mod:`usmo.cli.output`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .output import console, get_console

if TYPE_CHECKING:  # pragma: no cover - typing only
    from usmo.core import CatalogChange, Script, Scripts


def _core():
    from usmo import core

    return core


BUILTIN_HELP: list[tuple[str, str]] = [
    ("list", "List available commands."),
    ("search", "Find commands by name or description."),
    ("update", "Refresh the catalog; --all or NAME pulls scripts."),
    ("install", "Install a script as an alias in ~/.local/bin."),
    ("uninstall", "Remove an installed alias."),
    ("clean", "Remove the script cache."),
    ("version", "Show the usm version."),
]

CACHED_GLYPH = "[green]●[/green]"
MISSING_GLYPH = "[dim]○[/dim]"


def _table(**kwargs):
    from rich import box
    from rich.table import Table

    kwargs.setdefault("box", box.SIMPLE_HEAD)
    kwargs.setdefault("header_style", "dim")
    kwargs.setdefault("pad_edge", False)
    kwargs.setdefault("padding", (0, 2, 0, 0))
    kwargs.setdefault("expand", False)
    kwargs.setdefault("title_justify", "left")
    kwargs.setdefault("title_style", "bold")
    return Table(**kwargs)


# -- landing page -----------------------------------------------------------


LANDING: list[tuple[str, str]] = [
    ("usm list", "See every available command"),
    ("usm search <query>", "Find a command by name or description"),
    ("usm <name> --help", "Help for one command"),
    ("usm update", "Refresh the catalog"),
]


def print_landing(version: str) -> None:
    """The bare ``usm`` page: what this is, and where to go next.

    Rendered with click rather than rich: this is the most common invocation
    and a fixed four-row layout does not need a table engine, which roughly
    halves the time to first paint. It also never touches the catalog, so it
    is instant on a cold machine with no network.
    """
    import click

    click.echo(
        click.style("usm", fg="cyan", bold=True) + " " + click.style(version, dim=True)
    )
    click.echo(click.style("Run cached utility scripts from one CLI.", dim=True))
    click.echo()
    width = max(len(cmd) for cmd, _ in LANDING)
    for cmd, blurb in LANDING:
        click.echo(
            "  "
            + click.style(cmd.ljust(width), bold=True)
            + "   "
            + click.style(blurb, dim=True)
        )


# -- listings ---------------------------------------------------------------


def scripts_table(
    scripts: "Scripts",
    *,
    title: str = "Commands",
    highlight: str | None = None,
) -> "object":
    table = _table(title=title)
    table.add_column("", no_wrap=True, justify="center")
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("version", style="dim", no_wrap=True)
    table.add_column("description", overflow="fold", min_width=24, ratio=1)

    for name in sorted(scripts):
        script = scripts[name]
        table.add_row(
            CACHED_GLYPH if script.cached_path.exists() else MISSING_GLYPH,
            _mark(name, highlight),
            f"v{script.version}" if script.version else "v?",
            _mark(script.description, highlight),
        )
    return table


def _mark(text: str, needle: str | None) -> str:
    """Bold the matched span so a search result shows *why* it matched."""
    if not needle or not text:
        return text
    lowered = text.lower()
    start = lowered.find(needle.lower())
    if start < 0:
        return text
    end = start + len(needle)
    return f"{text[:start]}[bold yellow]{text[start:end]}[/bold yellow]{text[end:]}"


def print_scripts(
    scripts: "Scripts",
    *,
    title: str = "Commands",
    highlight: str | None = None,
    footer: bool = True,
) -> None:
    console = get_console()
    console.print(scripts_table(scripts, title=title, highlight=highlight))
    if footer:
        cached = sum(1 for s in scripts.values() if s.cached_path.exists())
        console.print(
            f"[dim]{CACHED_GLYPH} cached[/dim]  [dim]{MISSING_GLYPH} not yet "
            f"downloaded[/dim]  [dim]· {cached}/{len(scripts)} cached · "
            f"usm <name> --help for details[/dim]"
        )


def print_builtins() -> None:
    table = _table(title="Built-in", show_header=False)
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("help", overflow="fold", min_width=24, ratio=1)
    for name, help_text in BUILTIN_HELP:
        table.add_row(name, help_text)
    get_console().print(table)


def print_no_matches(query: str, scripts: "Scripts") -> None:
    console = get_console()
    console.print(f"[yellow]No command matches[/yellow] [bold]{query}[/bold].")
    close = _core().closest_names(query, scripts)
    if close:
        console.print(f"[dim]Did you mean: {', '.join(close)}?[/dim]")
    else:
        console.print("[dim]Run [bold]usm list[/bold] to see everything.[/dim]")


def print_unknown_command(command: str, scripts: "Scripts") -> None:
    console = get_console()
    console.print(f"[bold red]Error:[/bold red] Unknown command '{command}'.")
    close = _core().closest_names(command, scripts)
    if close:
        console.print(f"[dim]Did you mean: {', '.join(close)}?[/dim]")
    console.print(
        "[dim]Run [bold]usm list[/bold] to see every command, or "
        "[bold]usm search <query>[/bold] to look one up.[/dim]"
    )


def print_script_help(script: "Script") -> None:
    console = get_console()
    header = f"[bold cyan]{script.name}[/bold cyan]"
    if script.version:
        header += f" [dim]v{script.version}[/dim]"
    console.print(f"{header}  {script.description}")
    console.print(f"\n[bold]Usage[/bold]\n  usm {script.name} [ARGS...]")
    if script.requirements:
        console.print(
            "\n[bold]Requirements[/bold] [dim](installed on first run via "
            "uv)[/dim]\n  " + ", ".join(script.requirements)
        )
    if script.modules:
        console.print("\n[bold]Shared modules[/bold]\n  " + ", ".join(script.modules))
    if script.python:
        console.print(f"\n[bold]Python[/bold]\n  {script.python}")
    state = "cached" if script.cached_path.exists() else "not downloaded yet"
    console.print(f"\n[dim]{state} · {script.cached_path}[/dim]")


# -- update diffs -----------------------------------------------------------


def change_row(c: "CatalogChange") -> tuple[str, str, str]:
    """(script, version, hash) cells for one catalog change."""
    if c.status == "added":
        return (
            c.name,
            f"[green]new {c.new_version}[/green]",
            f"[green]{_core().short_hash(c.new_hash)}[/green]",
        )
    if c.status == "removed":
        return (
            f"[dim]{c.name}[/dim]",
            "[red]removed[/red]",
            f"[dim]{_core().short_hash(c.old_hash)}[/dim]",
        )
    return (
        c.name,
        f"{c.old_version} [dim]→[/dim] [cyan]{c.new_version}[/cyan]",
        f"{_core().short_hash(c.old_hash)} [dim]→[/dim] {_core().short_hash(c.new_hash)}",
    )


def changes_table(title: str):
    table = _table(title=title)
    table.add_column("script", style="bold cyan", no_wrap=True)
    table.add_column("version")
    table.add_column("hash")
    return table


def print_catalog_changes(changes: "list[CatalogChange]", *, cold: bool) -> None:
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
    get_console().print(table)


def print_named_update(names: tuple[str, ...], changes: "list[CatalogChange]") -> None:
    by_name = {c.name: c for c in changes}
    meta = _core().read_catalog_meta()
    table = changes_table("Updated")
    for name in names:
        if name in by_name:
            table.add_row(*change_row(by_name[name]))
        else:
            version, h = meta.get(name, (None, None))
            table.add_row(name, version or "?", _core().short_hash(h))
    get_console().print(table)
