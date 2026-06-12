"""Remote fetch, local cache, and catalog (``_config.json``) operations."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from . import constants
from .constants import ProgressHook, _null_hook
from .errors import DownloadError, UnknownCommand
from .model import Script, Scripts


# Remote fetch & cache ------------------------------------------------------


def download_file(filename: str, *, on_progress: ProgressHook = _null_hook) -> Path:
    """Download a single file from the remote scripts directory."""
    import requests

    on_progress(filename)
    try:
        response = requests.get(
            f"{constants.RESOURCE_BASE_URL}{filename}", timeout=(5, 60)
        )
    except requests.RequestException as exc:
        raise DownloadError(filename, 0) from exc
    if response.status_code != 200:
        raise DownloadError(filename, response.status_code)
    dest = constants.CACHE_SCRIPT_DIR / filename
    constants.CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
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
        config_path = Path.cwd() / "scripts" / constants.CONFIG_FILENAME
    else:
        config_path = constants.CACHE_SCRIPT_DIR / constants.CONFIG_FILENAME
        if force_download or not config_path.exists():
            config_path = download_file(
                constants.CONFIG_FILENAME, on_progress=on_progress
            )

    raw = json.loads(config_path.read_text()).get("scripts", {})
    return {name: Script.from_config(name, info) for name, info in raw.items()}


# Maintenance actions -------------------------------------------------------


def clean_cache() -> Path | None:
    """Remove the script cache (and per-script envs); return the path if any existed."""
    existed = constants.CACHE_SCRIPT_DIR.exists() or constants.CACHE_ENV_DIR.exists()
    shutil.rmtree(constants.CACHE_SCRIPT_DIR, ignore_errors=True)
    shutil.rmtree(constants.CACHE_ENV_DIR, ignore_errors=True)
    return constants.CACHE_SCRIPT_DIR if existed else None


def iter_updates(
    *,
    names: Iterable[str] | None = None,
    refresh_config: bool = True,
    on_progress: ProgressHook = _null_hook,
) -> Iterator[tuple[str, bool]]:
    """Re-download cached scripts (refreshing the config first by default).

    Yields ``(name, updated)`` per script; ``updated`` is True iff the script
    file was actually downloaded. When ``names`` is given, only those scripts
    are considered (and unknown names raise :class:`UnknownCommand`); scripts
    requested explicitly are downloaded even when not previously cached. Pass
    ``refresh_config=False`` when the caller already refreshed the manifest.
    """
    if refresh_config:
        download_file(constants.CONFIG_FILENAME, on_progress=on_progress)
    scripts = load_scripts(on_progress=on_progress)
    if names is None:
        targets = list(scripts.items())
        force_missing = False
    else:
        wanted = list(names)
        unknown = [n for n in wanted if n not in scripts]
        if unknown:
            raise UnknownCommand(unknown[0], scripts.keys())
        targets = [(n, scripts[n]) for n in wanted]
        force_missing = True
    for name, script in targets:
        if script.cached_path.exists() or force_missing:
            download_file(script.path, on_progress=on_progress)
            yield name, True
        else:
            yield name, False


# Catalog diff (used by ``usm update``) -------------------------------------


@dataclass(frozen=True)
class CatalogChange:
    """A script whose version/hash differs between the old and new manifest."""

    name: str
    old_version: str | None
    new_version: str | None
    old_hash: str | None
    new_hash: str | None

    @property
    def status(self) -> str:
        if self.old_version is None and self.old_hash is None:
            return "added"
        if self.new_version is None and self.new_hash is None:
            return "removed"
        return "changed"


def has_cached_config() -> bool:
    return (constants.CACHE_SCRIPT_DIR / constants.CONFIG_FILENAME).exists()


def read_catalog_meta(
    path: Path | None = None,
) -> dict[str, tuple[str | None, str | None]]:
    """Return ``{name: (version, hash)}`` from a manifest (default: cached)."""
    path = path or (constants.CACHE_SCRIPT_DIR / constants.CONFIG_FILENAME)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, tuple[str | None, str | None]] = {}
    for name, entry in data.get("scripts", {}).items():
        if isinstance(entry, dict):
            out[name] = (entry.get("version"), entry.get("hash"))
    return out


def short_hash(value: str | None) -> str:
    """Short form of a ``sha256:<hex>`` digest (7 hex chars)."""
    if not value:
        return "-"
    prefix = constants.HASH_PREFIX
    digest = value[len(prefix) :] if value.startswith(prefix) else value
    return digest[:7]


def update_config(*, on_progress: ProgressHook = _null_hook) -> list[CatalogChange]:
    """Re-download the ``_config.json`` manifest; return per-script changes.

    Compares the previously-cached manifest with the freshly-downloaded one and
    returns the version/hash differences (added / removed / changed scripts).
    """
    old = read_catalog_meta()
    download_file(constants.CONFIG_FILENAME, on_progress=on_progress)
    new = read_catalog_meta()
    changes: list[CatalogChange] = []
    for name in sorted(set(old) | set(new)):
        ov, oh = old.get(name, (None, None))
        nv, nh = new.get(name, (None, None))
        if (ov, oh) != (nv, nh):
            changes.append(CatalogChange(name, ov, nv, oh, nh))
    return changes
