"""Installed-version lookup and the background per-script update probe."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from importlib.metadata import version as pkg_version

from . import constants


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


@dataclass(frozen=True)
class VersionDiff:
    """A script whose remote version differs from the local cached one."""

    name: str
    local_version: str | None
    remote_version: str | None


def _script_versions(raw_scripts: dict) -> dict[str, str | None]:
    return {
        name: (entry.get("version") if isinstance(entry, dict) else None)
        for name, entry in raw_scripts.items()
    }


def _load_local_script_versions() -> dict[str, str | None] | None:
    """Return ``{name: version}`` from the cached _config.json, or None."""
    path = constants.CACHE_SCRIPT_DIR / constants.CONFIG_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _script_versions(data.get("scripts", {}))


def fetch_remote_script_versions(timeout: float = 5.0) -> dict[str, str | None] | None:
    """Fetch the remote _config.json (in memory) and return per-script versions."""
    import requests

    try:
        r = requests.get(
            f"{constants.RESOURCE_BASE_URL}{constants.CONFIG_FILENAME}", timeout=timeout
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = json.loads(r.text)
    except json.JSONDecodeError:
        return None
    return _script_versions(data.get("scripts", {}))


def auto_check_interval() -> int:
    """Read ``USM_AUTO_CHECK_INTERVAL`` (seconds). 0 disables. Invalid → default."""
    raw = os.environ.get(constants.AUTO_CHECK_ENV)
    if raw is None or raw == "":
        return constants.DEFAULT_AUTO_CHECK_INTERVAL
    try:
        return max(0, int(raw))
    except ValueError:
        return constants.DEFAULT_AUTO_CHECK_INTERVAL


def should_auto_check() -> bool:
    """Return True if the auto-check interval has elapsed (or never ran)."""
    interval = auto_check_interval()
    if interval <= 0:
        return False
    try:
        last = constants.LAST_CHECK_FILE.stat().st_mtime
    except OSError:
        return True
    return time.time() - last >= interval


def mark_checked() -> None:
    """Touch the last-check timestamp file."""
    try:
        constants.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        constants.LAST_CHECK_FILE.touch()
        os.utime(constants.LAST_CHECK_FILE, None)
    except OSError:
        pass


def check_for_update(*, force: bool = False) -> list[VersionDiff] | None:
    """If due, fetch the remote manifest and diff per-script versions.

    Returns the list of changed scripts (possibly empty if everything matches),
    or ``None`` when no check was run (interval not due, cold cache, or
    network failure). Always touches the last-check timestamp on completion.
    """
    if not force and not should_auto_check():
        return None
    local = _load_local_script_versions()
    if local is None:
        mark_checked()
        return None
    remote = fetch_remote_script_versions()
    mark_checked()
    if remote is None:
        return None
    diffs: list[VersionDiff] = []
    for name in sorted(set(local) | set(remote)):
        lv, rv = local.get(name), remote.get(name)
        if lv != rv:
            diffs.append(VersionDiff(name=name, local_version=lv, remote_version=rv))
    return diffs
