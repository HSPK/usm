"""``usm`` CLI: thin click + rich layer over :mod:`usmo.core`."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable

import click
import rich
from rich import box
from rich.console import Console
from rich.table import Table

from usmo import core
from usmo.core import Script, Scripts

console = Console()


# Presentation helpers ------------------------------------------------------


def _on_download(filename: str) -> None:
    rich.print(f"[bold green]Downloading:[/bold green] {filename}")


def _scripts_table(scripts: Scripts) -> Table:
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


def _builtin_table() -> Table:
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
    for name, help_text in _BUILTIN_HELP:
        table.add_row(name, help_text)
    return table


def _print_overview(scripts: Scripts) -> None:
    console.print(_scripts_table(scripts))
    console.print(_builtin_table())
    console.print(
        "[dim]Run [bold]usm <name> --help[/bold] for command-specific help.[/dim]"
    )


def _print_script_help(script: Script) -> None:
    header = f"[bold]{script.name}[/bold]"
    if script.version:
        header += f" [dim]v{script.version}[/dim]"
    rich.print(f"{header}: {script.description}")
    rich.print("Usage:")
    rich.print(f"  usm {script.name} [ARGS...]")
    if script.requirements:
        rich.print(
            "Requirements (installed on first run via [cyan]uv[/cyan]): "
            + ", ".join(script.requirements)
        )
    if script.python:
        rich.print(f"Python: {script.python}")


# Built-in commands ---------------------------------------------------------


def _cmd_list(scripts: Scripts) -> None:
    _print_overview(scripts)


def _cmd_update(args: tuple[str, ...] = ()) -> None:
    flags = {a for a in args if a.startswith("-")}
    names = tuple(a for a in args if not a.startswith("-"))
    unknown = flags - {"--all", "-a"}
    if unknown:
        raise click.ClickException(f"unknown option(s): {', '.join(sorted(unknown))}")
    all_scripts = bool(flags & {"--all", "-a"})

    if not names and not all_scripts:
        try:
            core.update_config(on_progress=_on_download)
        except core.DownloadError as exc:
            raise click.ClickException(str(exc)) from exc
        rich.print(
            "[bold green]Catalog updated.[/bold green] "
            "Use [bold]usm update --all[/bold] to refresh cached scripts, "
            "or [bold]usm update NAME[/bold] for one."
        )
        return

    try:
        results = list(core.iter_updates(names=names or None, on_progress=_on_download))
    except core.UnknownCommand as exc:
        rich.print(f"[bold red]Error:[/bold red] Unknown command '{exc.name}'.")
        rich.print(f"Available: {', '.join(exc.available)}")
        raise click.ClickException(str(exc)) from exc
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc
    for name, updated in results:
        if updated:
            rich.print(f"  [green]✓[/green] {name}")
        else:
            rich.print(f"  [dim]–[/dim] {name} (not cached, skipped)")
    rich.print("[bold green]Update complete.[/bold green]")


def _cmd_install(args: tuple[str, ...], *, debug: bool = False) -> None:
    names = [a for a in args if not a.startswith("-")]
    if len(names) != 2:
        raise click.ClickException("usage: usm install <script> <alias>")
    script, alias = names
    try:
        scripts = core.load_scripts(debug=debug, on_progress=_on_download)
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc
    if script not in scripts:
        rich.print(f"[bold red]Error:[/bold red] Unknown script '{script}'.")
        rich.print(f"Available: {', '.join(sorted(scripts))}")
        raise click.ClickException(f"Unknown script '{script}'.")

    path, status = core.alias_status(alias)
    if status == "foreign":
        rich.print(f"[yellow]{path} already exists and is not a usm alias.[/yellow]")
        if not click.confirm("Overwrite it?", default=False):
            raise click.ClickException("aborted.")

    usm_bin = shutil.which("usm") or sys.argv[0]
    core.install_alias(script, alias, usm_bin=usm_bin)
    verb = "Updated" if status == "ours" else "Installed"
    rich.print(
        f"[bold green]{verb}:[/bold green] [bold]{alias}[/bold] → usm {script}  "
        f"[dim]({path})[/dim]"
    )
    if not core.local_bin_in_path():
        rich.print(
            f"[yellow]note:[/yellow] {core.LOCAL_BIN_DIR} is not on your PATH. "
            "Add it so the alias is found:"
        )
        rich.print(
            '  [bold]export PATH="$HOME/.local/bin:$PATH"[/bold] '
            "[dim](append to ~/.bashrc or ~/.zshrc, then restart the shell)[/dim]"
        )


def _cmd_uninstall(args: tuple[str, ...]) -> None:
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
        rich.print(f"[dim]No usm alias '{alias}' in {core.LOCAL_BIN_DIR}.[/dim]")
    else:
        rich.print(f"[bold green]Removed:[/bold green] {alias} [dim]({removed})[/dim]")


def _cmd_clean() -> None:
    removed = core.clean_cache()
    if removed:
        rich.print(f"[bold green]Removed:[/bold green] {removed}")
    else:
        rich.print("[dim]Cache directory does not exist – nothing to clean.[/dim]")


def _cmd_version() -> None:
    rich.print(f"[bold]usm[/bold] version {core.resolve_version()}")


_STANDALONE_BUILTINS: dict[str, Callable[[], None]] = {
    "version": _cmd_version,
    "clean": _cmd_clean,
}
_SCRIPTED_BUILTINS: dict[str, Callable[[Scripts], None]] = {
    "list": _cmd_list,
}

_BUILTIN_HELP: list[tuple[str, str]] = [
    ("list", "List all commands."),
    ("update", "Refresh the catalog; --all or NAME pulls scripts."),
    ("install", "Install a script as an alias in ~/.local/bin."),
    ("uninstall", "Remove an installed alias."),
    ("clean", "Remove the script cache."),
    ("version", "Show usm version."),
]


# Auto-check ----------------------------------------------------------------

# Commands that should never trigger an auto-check (cheap built-ins or the
# update flow itself).
_AUTO_CHECK_SKIP_COMMANDS = {"update", "clean", "version", "install", "uninstall"}


def _maybe_auto_check(command: str | None, debug: bool) -> None:
    """Probe the remote config for per-script version bumps; prompt to update."""
    if debug or command in _AUTO_CHECK_SKIP_COMMANDS:
        return
    try:
        diffs = core.check_for_update()
    except Exception:
        return  # never fail the user's command on auto-check
    if not diffs:
        return
    rich.print(
        f"[bold yellow]usm:[/bold yellow] {len(diffs)} script(s) have updates available:"
    )
    for d in diffs:
        local = d.local_version or "[dim]missing[/dim]"
        remote = d.remote_version or "[dim]removed[/dim]"
        rich.print(f"  [bold]{d.name}[/bold]: {local} → [cyan]{remote}[/cyan]")
    if not sys.stdin.isatty():
        rich.print("[dim]      Run 'usm update --all' to refresh.[/dim]")
        return
    try:
        proceed = click.confirm("Pull the updated scripts now?", default=False)
    except click.Abort:
        return
    if proceed:
        try:
            _cmd_update(("--all",))
        except Exception as exc:
            rich.print(f"[yellow]Update failed:[/yellow] {exc}")


# Script dispatch -----------------------------------------------------------


def _run_script(
    script: Script,
    args: tuple[str, ...],
    *,
    debug: bool,
    upgrade: bool,
) -> None:
    try:
        core.run_script(
            script, args, debug=debug, upgrade=upgrade, on_progress=_on_download
        )
    except core.MissingUv as exc:
        rich.print(
            "[bold red]Error:[/bold red] this command declares "
            f"requirements ({', '.join(exc.requirements)}) but 'uv' was "
            "not found on PATH."
        )
        rich.print(f"Install uv first: {core.UV_INSTALL_HINT}")
        raise click.ClickException(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        # Translate signal-death (negative returncode) to the shell convention
        # 128+N (e.g. SIGINT -> 130, SIGTERM -> 143, SIGKILL -> 137) so callers
        # can recognise cancellation via `$? -eq 130` etc.
        rc = exc.returncode
        if rc is not None and rc < 0:
            rc = 128 - rc
        sys.exit(rc if rc is not None else 1)
    except OSError as exc:
        rich.print(f"[bold red]An error occurred:[/bold red] {exc}")
        raise click.ClickException(str(exc)) from exc


# Entry-point ---------------------------------------------------------------


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
        allow_interspersed_args=False,
    )
)
@click.argument("command", type=str, required=False, default=None)
@click.argument("args", nargs=-1, type=str)
@click.option(
    "-h", "--help", "show_help", is_flag=True, help="Show this message and exit."
)
@click.option(
    "--upgrade", "-U", is_flag=True, help="Upgrade the script before running."
)
@click.option("--debug", is_flag=True, help="Enable debug mode.")
def cli(
    command: str | None,
    args: tuple[str, ...],
    show_help: bool,
    upgrade: bool,
    debug: bool,
) -> None:
    _maybe_auto_check(command, debug)

    def _load() -> Scripts:
        try:
            return core.load_scripts(
                debug=debug, force_download=upgrade, on_progress=_on_download
            )
        except core.DownloadError as exc:
            raise click.ClickException(str(exc)) from exc

    if command is None:
        _print_overview(_load())
        return

    if command == "update":
        _cmd_update(args)
        return

    if command == "install":
        _cmd_install(args, debug=debug)
        return

    if command == "uninstall":
        _cmd_uninstall(args)
        return

    if command in _STANDALONE_BUILTINS:
        _STANDALONE_BUILTINS[command]()
        return

    scripts = _load()

    if command in _SCRIPTED_BUILTINS:
        _SCRIPTED_BUILTINS[command](scripts)
        return

    if command not in scripts:
        rich.print(f"[bold red]Error:[/bold red] Unknown command '{command}'.")
        _print_overview(scripts)
        raise click.ClickException(f"Unknown command '{command}'.")

    script = scripts[command]
    if show_help:
        _print_script_help(script)
        return

    _run_script(script, args, debug=debug, upgrade=upgrade)


if __name__ == "__main__":
    cli()
