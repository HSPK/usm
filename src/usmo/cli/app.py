"""The ``usm`` entry point: parse arguments and route to a handler.

Deliberately thin — all presentation lives in :mod:`~usmo.cli.presenters`,
built-ins in :mod:`~usmo.cli.commands`, execution in :mod:`~usmo.cli.runner`,
and the SDK in :mod:`usmo.core`.
"""

from __future__ import annotations

import click

from . import commands, presenters, runner


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
    if command is None:
        presenters.print_overview(commands.load_scripts(debug=debug, upgrade=upgrade))
        return

    handler = commands.COMMANDS.get(command)
    if handler is not None:
        handler(args, debug=debug, upgrade=upgrade)
        return

    scripts = commands.load_scripts(debug=debug, upgrade=upgrade)
    script = scripts.get(command)
    if script is None:
        presenters.print_unknown_command(command, scripts)
        raise click.ClickException(f"Unknown command '{command}'.")

    if show_help:
        presenters.print_script_help(script)
        return

    runner.run_script(script, args, debug=debug, upgrade=upgrade)


if __name__ == "__main__":
    cli()
