"""Run a catalog script and translate SDK errors into CLI output/exit codes."""

from __future__ import annotations

import subprocess
import sys

import click

from usmo import core
from usmo.core import Script

from .output import console, on_download, on_env_build


def run_script(
    script: Script,
    args: tuple[str, ...],
    *,
    debug: bool,
    upgrade: bool,
) -> None:
    try:
        core.run_script(
            script,
            args,
            debug=debug,
            upgrade=upgrade,
            on_progress=on_download,
            on_setup=on_env_build,
        )
    except core.MissingUv as exc:
        console.print(
            "[bold red]Error:[/bold red] this command declares "
            f"requirements ({', '.join(exc.requirements)}) but 'uv' was "
            "not found on PATH."
        )
        console.print(f"Install uv first: {core.UV_INSTALL_HINT}")
        raise click.ClickException(str(exc)) from exc
    except core.EnvBuildError as exc:
        console.print(
            "[bold red]Error:[/bold red] couldn't prepare the environment for "
            f"[bold]{exc.name}[/bold]."
        )
        if exc.detail:
            console.print(f"[dim]{exc.detail}[/dim]")
        console.print(
            "If PyPI is blocked, set a mirror and retry, e.g.\n"
            "  [bold]export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple[/bold]\n"
            f"then run [bold]usm -U {exc.name}[/bold] to rebuild the environment."
        )
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
        console.print(f"[bold red]An error occurred:[/bold red] {exc}")
        raise click.ClickException(str(exc)) from exc
