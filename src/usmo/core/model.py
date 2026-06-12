"""The :class:`Script` model parsed from ``_config.json``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import constants


@dataclass(frozen=True)
class Script:
    """One entry parsed from ``_config.json``."""

    name: str
    path: str
    description: str = ""
    requirements: tuple[str, ...] = ()
    python: str | None = None
    version: str | None = None
    hash: str | None = None

    @classmethod
    def from_config(cls, name: str, raw: dict) -> Script:
        return cls(
            name=name,
            path=raw["path"],
            description=raw.get("description", ""),
            requirements=tuple(raw.get("requirements") or ()),
            python=raw.get("python"),
            version=raw.get("version"),
            hash=raw.get("hash"),
        )

    @property
    def is_python(self) -> bool:
        return self.path.lower().endswith(".py")

    @property
    def uses_uv(self) -> bool:
        return self.is_python and bool(self.requirements)

    @property
    def cached_path(self) -> Path:
        return constants.CACHE_SCRIPT_DIR / self.path

    def local_path(self, *, debug: bool) -> Path:
        return Path.cwd() / "scripts" / self.path if debug else self.cached_path

    @property
    def env_dir(self) -> Path:
        """Directory of this script's persistent virtualenv."""
        return constants.CACHE_ENV_DIR / self.name

    def interpreter_version(self) -> str:
        return self.python or f"{sys.version_info.major}.{sys.version_info.minor}"

    def build_argv(
        self, script_path: Path, args: Iterable[str], *, python: str
    ) -> list[str]:
        """Return the argv to run this script with the given *python* executable.

        Shell scripts run under ``bash``; Python scripts run under *python*
        (a per-script venv interpreter when the script has requirements, or the
        usm interpreter otherwise). No package resolution happens here.
        """
        runner = python if self.is_python else "bash"
        return [runner, str(script_path), *args]


Scripts = dict[str, Script]
