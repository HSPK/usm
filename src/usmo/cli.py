"""``usm`` CLI: thin click + rich layer over :mod:`usmo.core`."""

from __future__ import annotations

import subprocess
import sys
from typing import Callable

import click
import rich

from usmo import core
from usmo.core import Script, Scripts


# Presentation helpers ------------------------------------------------------


def _on_download(filename: str) -> None:
    rich.print(f"[bold green]Downloading:[/bold green] {filename}")


def _print_overview(scripts: Scripts) -> None:
    rich.print("[bold]Available commands:[/bold]\n")
    rich.print("[bold underline]Scripts:[/bold underline]")
    for name in sorted(scripts):
        s = scripts[name]
        status = (
            "[green]cached[/green]"
            if s.cached_path.exists()
            else "[dim]not cached[/dim]"
        )
        uv_tag = f"  [cyan]+uv[/cyan]({len(s.requirements)} req)" if s.uses_uv else ""
        rich.print(f"  [bold]{name:20s}[/bold] {s.description:50s}  {status}{uv_tag}")
    rich.print("\n[bold underline]Built-in:[/bold underline]")
    for name, help_text in _BUILTIN_HELP:
        rich.print(f"  [bold]{name:20s}[/bold] {help_text}")


def _print_script_help(script: Script) -> None:
    rich.print(f"[bold]{script.name}[/bold]: {script.description}")
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


def _cmd_update() -> None:
    for name, updated in core.iter_updates(on_progress=_on_download):
        if updated:
            rich.print(f"  [green]✓[/green] {name}")
        else:
            rich.print(f"  [dim]–[/dim] {name} (not cached, skipped)")
    rich.print("[bold green]Update complete.[/bold green]")


def _cmd_clean() -> None:
    removed = core.clean_cache()
    if removed:
        rich.print(f"[bold green]Removed:[/bold green] {removed}")
    else:
        rich.print("[dim]Cache directory does not exist – nothing to clean.[/dim]")


def _cmd_version() -> None:
    rich.print(f"[bold]usm[/bold] version {core.resolve_version()}")


# Standalone built-ins skip the config load (and its potential download).
_STANDALONE_BUILTINS: dict[str, Callable[[], None]] = {
    "version": _cmd_version,
    "clean": _cmd_clean,
    "update": _cmd_update,
}
_SCRIPTED_BUILTINS: dict[str, Callable[[Scripts], None]] = {
    "list": _cmd_list,
}

_BUILTIN_HELP: list[tuple[str, str]] = [
    ("list", "List all available commands."),
    ("update", "Re-download config and all cached scripts."),
    ("clean", "Remove the script cache directory."),
    ("version", "Show usm version."),
]


# Auto-check ----------------------------------------------------------------

# Commands that should never trigger an auto-check (cheap built-ins or the
# update flow itself).
_AUTO_CHECK_SKIP_COMMANDS = {"update", "clean", "version"}


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
        rich.print("[dim]      Run 'usm update' to refresh.[/dim]")
        return
    try:
        proceed = click.confirm("Run 'usm update' now?", default=False)
    except click.Abort:
        return
    if proceed:
        try:
            _cmd_update()
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
        sys.exit(exc.returncode)
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
