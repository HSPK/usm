"""Run an installed package's entry script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

import click
import rich

from . import state as state_mod


def _build_command(entry: Path, args: Sequence[str]) -> list[str]:
    if entry.suffix == ".py":
        return [sys.executable, str(entry), *args]
    return ["bash", str(entry), *args]


def run_installed(name: str, args: Sequence[str]) -> int:
    """Execute the installed entry script for ``name`` with ``args``."""
    pkg = state_mod.get(name)
    if pkg is None:
        raise click.ClickException(
            f"'{name}' is not installed. Run: usm install {name}"
        )
    entry = Path(pkg.install_dir) / pkg.entry
    if not entry.exists():
        raise click.ClickException(
            f"Entry script for '{name}' is missing at {entry}; try: usm install --force {name}"
        )
    cmd = _build_command(entry, list(args))
    try:
        completed = subprocess.run(cmd, check=False, text=True)
    except OSError as exc:
        rich.print(f"[bold red]Failed to launch {name}:[/bold red] {exc}")
        raise click.ClickException(str(exc)) from exc
    return completed.returncode
