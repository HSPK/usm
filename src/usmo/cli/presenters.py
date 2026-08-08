"""Rich rendering for the CLI: tables, the landing page, help, update diffs.

Pure presentation — every function here only formats data and writes to the
shared console; no catalog or filesystem logic lives in this module.

``rich`` is imported inside the functions that need it so importing this
module stays free (see :mod:`usmo.cli.output`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from usmo import ui

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


# -- landing page -----------------------------------------------------------


LANDING: list[tuple[str, str]] = [
    ("usm list", "See every available command"),
    ("usm search <query>", "Find a command by name or description"),
    ("usm <name> --help", "Help for one command"),
    ("usm update", "Refresh the catalog"),
]


def print_landing(version: str) -> None:
    """The bare ``usm`` page: what this is, and where to go next.

    Rendered with click rather than rich — this is the most common invocation
    and a fixed four-row layout does not need a table engine, which roughly
    halves the time to first paint. It follows the same palette as everything
    else (cyan identifier, dim context), it just gets there cheaply. It also
    never touches the catalog, so it is instant on a cold machine.
    """
    import click

    click.echo(
        click.style("usm", fg="cyan", bold=True) + " " + click.style(version, dim=True)
    )
    click.echo(click.style("Run cached utility scripts from one CLI.", dim=True))
    click.echo()
    pad = max(len(cmd) for cmd, _ in LANDING)
    for cmd, blurb in LANDING:
        click.echo(
            "  "
            + click.style(cmd.ljust(pad), bold=True)
            + "   "
            + click.style(blurb, dim=True)
        )


# -- listings ---------------------------------------------------------------


def scripts_table(
    scripts: "Scripts",
    *,
    title: str = "Commands",
    highlight: str | None = None,
    terminal_width: int | None = None,
):
    built = ui.table(
        ui.Column("", justify="center", min_width=1),
        ui.Column("name", style=ui.STYLE_ID, min_width=6),
        ui.Column("version", style=ui.STYLE_MUTED, min_width=6, hide_below=60),
        ui.Column("description", min_width=24, ratio=1),
        title=title,
        terminal_width=terminal_width,
    )
    narrow = (terminal_width or ui.width()) < 60
    for name in sorted(scripts):
        script = scripts[name]
        row = [
            ui.state(script.cached_path.exists()),
            _mark(name, highlight),
            f"v{script.version}" if script.version else "v?",
            _mark(script.description, highlight),
        ]
        built.add_row(*(row[:2] + row[3:] if narrow else row))
    return built


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
    ui.print(scripts_table(scripts, title=title, highlight=highlight))
    if footer:
        cached = sum(1 for s in scripts.values() if s.cached_path.exists())
        ui.hint(
            ui.joined(
                ui.legend((ui.CACHED, "cached"), (ui.MISSING, "not yet downloaded")),
                f"{cached}/{len(scripts)} cached",
                "usm <name> --help for details",
            )
        )


def print_builtins() -> None:
    built = ui.table(
        ui.Column("name", style=ui.STYLE_ID, min_width=6),
        ui.Column("help", min_width=24, ratio=1),
        title="Built-in",
    )
    built.show_header = False
    for name, help_text in BUILTIN_HELP:
        built.add_row(name, help_text)
    ui.print(built)


def print_no_matches(query: str, scripts: "Scripts") -> None:
    ui.warn(f"No command matches {query}.")
    close = _core().closest_names(query, scripts)
    if close:
        ui.hint(f"Did you mean: {', '.join(close)}?")
    else:
        ui.hint("Run usm list to see everything.")


def print_unknown_command(command: str, scripts: "Scripts") -> None:
    ui.fail(f"Unknown command '{command}'.")
    close = _core().closest_names(command, scripts)
    if close:
        ui.hint(f"Did you mean: {', '.join(close)}?")
    ui.hint("Run usm list to see every command, or usm search <query> to look one up.")


def print_script_help(script: "Script") -> None:
    ui.title(script.name, subtitle=script.description)
    rows = [("usage", f"usm {script.name} [ARGS...]")]
    if script.requirements:
        rows.append(("requirements", ", ".join(script.requirements)))
    if script.modules:
        rows.append(("shared modules", ", ".join(script.modules)))
    if script.python:
        rows.append(("python", script.python))
    rows.append(
        (
            "state",
            ui.joined(
                "cached" if script.cached_path.exists() else "not downloaded yet",
                ui.shorten_path(script.cached_path),
            ),
        )
    )
    ui.print()
    ui.print_detail(rows)


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
    return ui.table(
        ui.Column("script", style=ui.STYLE_ID, min_width=6),
        ui.Column("version", min_width=12),
        ui.Column("hash", min_width=10, hide_below=70),
        title=title,
    )


def print_catalog_changes(changes: "list[CatalogChange]", *, cold: bool) -> None:
    if not changes:
        ui.ok("Catalog is up to date.")
        return
    if cold:
        ui.ok(f"Fetched catalog ({ui.plural(len(changes), 'script')}).")
        return
    table = changes_table(f"Catalog changes ({len(changes)})")
    for c in changes:
        table.add_row(*change_row(c))
    ui.print(table)


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
    ui.print(table)
