"""Script execution: persistent per-script virtualenvs and the run dispatcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from .catalog import ensure_script_file
from .constants import ProgressHook, _null_hook
from .errors import EnvBuildError, MissingUv
from .model import Script

ENV_MARKER_NAME = ".usm-env.json"


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


# Per-script virtualenvs ----------------------------------------------------


def _env_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _env_spec(script: Script) -> dict:
    return {
        "requirements": list(script.requirements),
        "python": script.interpreter_version(),
    }


def env_ready(script: Script) -> bool:
    """True if the script's venv exists and matches its current requirements."""
    if not script.uses_uv:
        return True
    py = _env_python(script.env_dir)
    if not py.exists():
        return False
    try:
        marker = json.loads((script.env_dir / ENV_MARKER_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return marker == _env_spec(script)


def _build_env(script: Script, *, on_progress: ProgressHook = _null_hook) -> Path:
    """Create the script's venv and install its requirements (needs network once)."""
    env_dir = script.env_dir
    on_progress(script.name)
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(env_dir, ignore_errors=True)
    py_ver = script.interpreter_version()
    try:
        subprocess.run(
            ["uv", "venv", "--python", py_ver, str(env_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(_env_python(env_dir)),
                *script.requirements,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(env_dir, ignore_errors=True)
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise EnvBuildError(script.name, detail) from exc
    (env_dir / ENV_MARKER_NAME).write_text(json.dumps(_env_spec(script)))
    return _env_python(env_dir)


def ensure_env(
    script: Script,
    *,
    upgrade: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> str:
    """Return a Python executable that satisfies *script*'s requirements.

    Scripts without requirements use the usm interpreter. Scripts with
    requirements get a persistent venv under ``~/.cache/usm/envs/<name>`` that
    is built once (and rebuilt only when requirements change or ``upgrade``);
    afterwards runs need no network or package resolution.

    Raises :class:`MissingUv` if uv is required but absent, or
    :class:`EnvBuildError` if building the venv fails.
    """
    if not script.uses_uv:
        return sys.executable
    if not shutil.which("uv"):
        raise MissingUv(script.requirements)
    if not upgrade and env_ready(script):
        return str(_env_python(script.env_dir))
    return str(_build_env(script, on_progress=on_progress))


def run_script(
    script: Script,
    args: Iterable[str],
    *,
    debug: bool = False,
    upgrade: bool = False,
    on_progress: ProgressHook = _null_hook,
    on_setup: ProgressHook = _null_hook,
) -> None:
    """Execute *script* with *args*.

    Raises ``MissingUv`` if uv is required but missing, ``EnvBuildError`` if the
    per-script venv can't be built, and ``subprocess.CalledProcessError`` /
    ``OSError`` if the script subprocess itself fails.
    """
    script_path = resolve_script_path(
        script, debug=debug, upgrade=upgrade, on_progress=on_progress
    )
    python = ensure_env(script, upgrade=upgrade, on_progress=on_setup)
    argv = script.build_argv(script_path, args, python=python)
    subprocess.run(argv, check=True, text=True)
