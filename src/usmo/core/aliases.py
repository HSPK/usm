"""Alias shims under ``~/.local/bin`` that re-invoke ``usm <script>``."""

from __future__ import annotations

import os
from pathlib import Path

from . import constants
from .errors import ForeignAlias


def local_bin_in_path() -> bool:
    """True if ``~/.local/bin`` is on ``$PATH``."""
    target = os.path.normcase(os.path.normpath(constants.LOCAL_BIN_DIR))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if os.path.normcase(os.path.normpath(os.path.expanduser(entry))) == target:
            return True
    return False


def alias_path(alias: str) -> Path:
    """Path of the shim file for *alias* (``.cmd`` suffix on Windows)."""
    name = alias
    if os.name == "nt" and not name.lower().endswith(".cmd"):
        name += ".cmd"
    return constants.LOCAL_BIN_DIR / name


def alias_status(alias: str) -> tuple[Path, str]:
    """Return ``(path, status)`` where status is absent/ours/foreign."""
    path = alias_path(alias)
    if not path.exists():
        return path, "absent"
    try:
        owned = constants.ALIAS_SHIM_MARKER in path.read_text(errors="ignore")
    except OSError:
        owned = False
    return path, ("ours" if owned else "foreign")


def install_alias(script: str, alias: str, *, usm_bin: str) -> Path:
    """Write an executable shim *alias* that runs ``usm <script>``.

    Overwrites whatever is at the target; the caller is responsible for
    resolving conflicts (see :func:`alias_status`).
    """
    constants.LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    path = alias_path(alias)
    marker = constants.ALIAS_SHIM_MARKER
    if os.name == "nt":
        body = f'@echo off\r\nrem {marker}: {script}\r\n"{usm_bin}" {script} %*\r\n'
    else:
        body = (
            "#!/usr/bin/env bash\n"
            f"# {marker}: {script}\n"
            f'exec "{usm_bin}" {script} "$@"\n'
        )
    path.write_text(body)
    if os.name != "nt":
        path.chmod(0o755)
    return path


def uninstall_alias(alias: str) -> Path | None:
    """Remove an alias shim we installed.

    Returns the removed path, or ``None`` if it didn't exist. Raises
    :class:`ForeignAlias` if the target exists but wasn't installed by usm.
    """
    path, status = alias_status(alias)
    if status == "absent":
        return None
    if status == "foreign":
        raise ForeignAlias(path)
    path.unlink()
    return path
