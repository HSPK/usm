"""Progress hooks for the CLI.

Rendering lives in :mod:`usmo.ui`, the design system shared with every script;
this module only holds the two catalog progress callbacks and the legacy
``console`` name.
"""

from __future__ import annotations

from typing import Any

from usmo import ui


def get_console():
    """The shared console (rich is imported on first use)."""
    return ui.console()


class _LazyConsole:
    """Forwards to the real console the first time something is printed."""

    def __getattr__(self, name: str) -> Any:
        return getattr(ui.console(), name)


console = _LazyConsole()


def on_download(filename: str) -> None:
    ui.step(f"downloading {ui.identifier(filename)}")


def on_env_build(name: str) -> None:
    ui.step(
        f"preparing environment for {ui.identifier(name)} "
        f"{ui.muted('(one-time; needs network)')}"
    )
