"""The ``usm`` entry point: parse arguments and route to a handler.

Deliberately thin — presentation lives in :mod:`~usmo.cli.presenters`,
built-ins in :mod:`~usmo.cli.commands`, execution in :mod:`~usmo.cli.runner`,
and the SDK in :mod:`usmo.core`.

Everything heavy (rich, the SDK, the catalog) is imported inside the branch
that needs it, so ``usm`` and ``usm <script>`` start essentially instantly.
"""

from __future__ import annotations

import click

USAGE = """[bold]Usage[/bold]
  usm <command> [ARGS...]        Run a command (downloaded on first use)
  usm <command> --help           Help for one command
  usm list [PATTERN]             List available commands
  usm search <query>             Find a command

[bold]Options[/bold]
  -U, --upgrade                  Re-download the command before running
      --debug                    Load scripts from ./scripts instead of the cache
  -V, --version                  Show the usm version
  -h, --help                     Show this message
"""


def _version() -> str:
    # Straight to the leaf module: usmo.core resolves its exports lazily, so
    # this costs nothing beyond the version lookup itself.
    from usmo.core.version import resolve_version

    return resolve_version()


def _print_help() -> None:
    from . import presenters
    from .output import get_console

    presenters.print_landing(_version())
    console = get_console()
    console.print()
    console.print(USAGE.rstrip())


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
        allow_interspersed_args=False,
        # Handled explicitly below so `-h` can also mean "help for this script".
        help_option_names=[],
    )
)
@click.argument("command", type=str, required=False, default=None)
@click.argument("args", nargs=-1, type=str)
@click.option("-h", "--help", "show_help", is_flag=True)
@click.option("-V", "--version", "show_version", is_flag=True)
@click.option(
    "--upgrade", "-U", is_flag=True, help="Upgrade the script before running."
)
@click.option("--debug", is_flag=True, help="Enable debug mode.")
def cli(
    command: str | None,
    args: tuple[str, ...],
    show_help: bool,
    show_version: bool,
    upgrade: bool,
    debug: bool,
) -> None:
    from . import commands

    if show_version:
        click.echo(f"usm version {_version()}")
        return

    if command is None:
        if show_help:
            _print_help()
        else:
            from . import presenters

            presenters.print_landing(_version())
        return

    builtin = commands.COMMANDS.get(command)
    if builtin is not None:
        # Hand the rest of argv to a real click command so --help, option
        # parsing and usage errors all behave the way they should.
        extra = list(args) + (["--help"] if show_help else [])
        builtin.main(
            args=extra,
            prog_name=f"usm {command}",
            obj={"debug": debug, "upgrade": upgrade},
        )
        return

    from . import presenters, runner

    scripts = commands.load_scripts(debug=debug, upgrade=upgrade)
    script = scripts.get(command)
    if script is None:
        presenters.print_unknown_command(command, scripts)
        raise SystemExit(2)

    if show_help:
        presenters.print_script_help(script)
        return

    runner.run_script(script, args, debug=debug, upgrade=upgrade)


if __name__ == "__main__":
    cli()
