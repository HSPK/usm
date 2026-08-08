"""Shared console and progress hooks for the CLI (the only output sink).

``rich`` costs ~16ms to import, a large slice of a CLI that should feel
instant, so the console is built on first use rather than at import time.
An invocation that only *runs* a script never pays for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console as RichConsole

_console: Any = None


def get_console() -> "RichConsole":
    """Return the shared console, importing rich on first use."""
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console()
    return _console


class _LazyConsole:
    """Forwards to the real console the first time something is printed.

    Lets callers keep ``console.print(...)`` at module scope without dragging
    rich into every ``usm`` invocation.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_console(), name)


console = _LazyConsole()


def on_download(filename: str) -> None:
    get_console().print(f"[bold green]Downloading:[/bold green] {filename}")


def on_env_build(name: str) -> None:
    get_console().print(
        f"[bold yellow]usm:[/bold yellow] preparing environment for "
        f"[bold]{name}[/bold] (one-time; needs network)…"
    )
