"""Shared console and progress hooks for the CLI (the only output sink)."""

from __future__ import annotations

from rich.console import Console

console = Console()


def on_download(filename: str) -> None:
    console.print(f"[bold green]Downloading:[/bold green] {filename}")


def on_env_build(name: str) -> None:
    console.print(
        f"[bold yellow]usm:[/bold yellow] preparing environment for "
        f"[bold]{name}[/bold] (one-time; needs network)…"
    )
