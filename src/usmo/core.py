"""Pure SDK for the usm script catalog.

UI-free (no click, no rich): parses ``_config.json``, manages the local script
cache, builds argv, and raises typed exceptions. The ``usmo.cli`` module wraps
this with click + rich.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Callable, Iterable, Iterator

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
CONFIG_FILENAME = "_config.json"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/scripts/"
UV_INSTALL_HINT = "https://docs.astral.sh/uv/#installation"

ProgressHook = Callable[[str], None]


def _null_hook(_filename: str) -> None:
    pass


# Errors --------------------------------------------------------------------

class UsmError(Exception):
    """Base class for SDK errors."""


class MissingUv(UsmError):
    def __init__(self, requirements: tuple[str, ...]) -> None:
        super().__init__("'uv' is required to satisfy script requirements.")
        self.requirements = requirements


class UnknownCommand(UsmError):
    def __init__(self, name: str, available: Iterable[str]) -> None:
        super().__init__(f"Unknown command '{name}'.")
        self.name = name
        self.available = sorted(available)


class DownloadError(UsmError):
    def __init__(self, filename: str, status: int) -> None:
        super().__init__(f"Failed to download {filename} (HTTP {status}).")
        self.filename = filename
        self.status = status


# Script model --------------------------------------------------------------

@dataclass(frozen=True)
class Script:
    """One entry parsed from ``_config.json``."""

    name: str
    path: str
    description: str = ""
    requirements: tuple[str, ...] = ()
    python: str | None = None

    @classmethod
    def from_config(cls, name: str, raw: dict) -> Script:
        return cls(
            name=name,
            path=raw["path"],
            description=raw.get("description", ""),
            requirements=tuple(raw.get("requirements") or ()),
            python=raw.get("python"),
        )

    @property
    def is_python(self) -> bool:
        return self.path.lower().endswith(".py")

    @property
    def uses_uv(self) -> bool:
        return self.is_python and bool(self.requirements)

    @property
    def cached_path(self) -> Path:
        return CACHE_SCRIPT_DIR / self.path

    def local_path(self, *, debug: bool) -> Path:
        return Path.cwd() / "scripts" / self.path if debug else self.cached_path

    def build_argv(self, script_path: Path, args: Iterable[str]) -> list[str]:
        """Return the argv to run this script. Raises ``MissingUv`` if needed."""
        if self.uses_uv:
            if not shutil.which("uv"):
                raise MissingUv(self.requirements)
            python = self.python or f"{sys.version_info.major}.{sys.version_info.minor}"
            argv = ["uv", "run", "--no-project", "--quiet", "--python", python]
            for req in self.requirements:
                argv += ["--with", req]
            return [*argv, "python", str(script_path), *args]
        runner = sys.executable if self.is_python else "bash"
        return [runner, str(script_path), *args]


Scripts = dict[str, Script]


# Remote fetch & cache ------------------------------------------------------

def download_file(filename: str, *, on_progress: ProgressHook = _null_hook) -> Path:
    """Download a single file from the remote scripts directory."""
    import requests

    on_progress(filename)
    response = requests.get(f"{RESOURCE_BASE_URL}{filename}")
    if response.status_code != 200:
        raise DownloadError(filename, response.status_code)
    dest = CACHE_SCRIPT_DIR / filename
    CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    if not filename.endswith(".json"):
        dest.chmod(dest.stat().st_mode | 0o111)
    return dest


def ensure_script_file(
    script: Script,
    *,
    force: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> Path:
    """Return the cached script path, downloading on cache miss or ``force``."""
    if force or not script.cached_path.exists():
        return download_file(script.path, on_progress=on_progress)
    return script.cached_path


def load_scripts(
    *,
    debug: bool = False,
    force_download: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> Scripts:
    """Return the parsed ``scripts`` mapping from ``_config.json``."""
    if debug:
        config_path = Path.cwd() / "scripts" / CONFIG_FILENAME
    else:
        config_path = CACHE_SCRIPT_DIR / CONFIG_FILENAME
        if force_download or not config_path.exists():
            config_path = download_file(CONFIG_FILENAME, on_progress=on_progress)

    raw = json.loads(config_path.read_text()).get("scripts", {})
    return {name: Script.from_config(name, info) for name, info in raw.items()}


# Maintenance actions -------------------------------------------------------

def clean_cache() -> Path | None:
    """Remove the script cache directory; return the path if it existed."""
    if not CACHE_SCRIPT_DIR.exists():
        return None
    shutil.rmtree(CACHE_SCRIPT_DIR)
    return CACHE_SCRIPT_DIR


def iter_updates(
    *, on_progress: ProgressHook = _null_hook
) -> Iterator[tuple[str, bool]]:
    """Refresh the config and re-download every cached script.

    Yields ``(name, updated)`` per script; ``updated`` is True iff it was
    previously cached and just re-downloaded.
    """
    download_file(CONFIG_FILENAME, on_progress=on_progress)
    for name, script in load_scripts(force_download=True, on_progress=on_progress).items():
        if script.cached_path.exists():
            download_file(script.path, on_progress=on_progress)
            yield name, True
        else:
            yield name, False


def resolve_version() -> str:
    """Return the installed usmo version, or a sentinel when unknown."""
    try:
        from usmo._version import __version__ as ver
        return ver
    except ImportError:
        pass
    try:
        return pkg_version("usmo")
    except Exception:
        return "unknown (editable install without build)"


# Script execution ----------------------------------------------------------

def resolve_script_path(
    script: Script,
    *,
    debug: bool,
    upgrade: bool,
    on_progress: ProgressHook = _null_hook,
) -> Path:
    """Resolve the on-disk location used to execute *script*."""
    if debug:
        return script.local_path(debug=True)
    return ensure_script_file(script, force=upgrade, on_progress=on_progress)


def run_script(
    script: Script,
    args: Iterable[str],
    *,
    debug: bool = False,
    upgrade: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> None:
    """Execute *script* with *args*.

    Raises ``MissingUv`` if uv is required but missing, and
    ``subprocess.CalledProcessError`` / ``OSError`` if the subprocess fails.
    """
    script_path = resolve_script_path(
        script, debug=debug, upgrade=upgrade, on_progress=on_progress
    )
    argv = script.build_argv(script_path, args)
    subprocess.run(argv, check=True, text=True)
