"""Typed exceptions raised by the SDK (UI-free)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class UsmError(Exception):
    """Base class for SDK errors."""


class MissingUv(UsmError):
    def __init__(self, requirements: tuple[str, ...]) -> None:
        super().__init__("'uv' is required to satisfy script requirements.")
        self.requirements = requirements


class EnvBuildError(UsmError):
    """Building a script's virtualenv failed (often a network/index issue)."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Failed to prepare the environment for '{name}'.")
        self.name = name
        self.detail = detail


class UnknownCommand(UsmError):
    def __init__(self, name: str, available: Iterable[str]) -> None:
        super().__init__(f"Unknown command '{name}'.")
        self.name = name
        self.available = sorted(available)


class DownloadError(UsmError):
    def __init__(self, filename: str, status: int) -> None:
        detail = "network error" if status == 0 else f"HTTP {status}"
        super().__init__(f"Failed to download {filename} ({detail}).")
        self.filename = filename
        self.status = status


class ForeignAlias(UsmError):
    """An alias target exists but was not installed by usm."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists and is not a usm-managed alias.")
        self.path = path
