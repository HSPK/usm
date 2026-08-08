"""Pure SDK for the usm script catalog (UI-free: no click, no rich).

This package is split by responsibility into focused submodules:

* :mod:`~usmo.core.constants` – paths, URLs, the progress-hook type
* :mod:`~usmo.core.errors` – typed exceptions
* :mod:`~usmo.core.model` – the :class:`Script` dataclass
* :mod:`~usmo.core.catalog` – remote fetch, local cache, ``_config.json`` ops
* :mod:`~usmo.core.environments` – per-script virtualenvs and execution
* :mod:`~usmo.core.aliases` – ``~/.local/bin`` shim management
* :mod:`~usmo.core.version` – installed-version lookup
* :mod:`~usmo.core.manifest` – manifest hashing / version bumping

The ``usmo.core`` namespace re-exports the full public API so callers can keep
importing from it directly (``from usmo.core import Script``).

Each name is bound on first attribute access rather than at import (PEP 562),
so importing this package is nearly free until the SDK is actually used --
which matters because the ``usm`` CLI imports it on every invocation.

One consequence worth knowing when testing: patch ``usmo.core.<name>``, not
``usmo.core.<submodule>.<name>``. The re-export caches the object it resolved,
so patching the submodule afterwards has no effect, and patching it *before*
the first access would leave the stub cached after the patch is undone.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# name -> submodule that defines it. Resolved on first attribute
# access (PEP 562) so importing usmo.core costs nothing until the
# SDK is actually used; `usm` itself only needs this for real work.
_EXPORTS = {
    "script_files_match": "catalog",
    "reload_script": "catalog",
    "reconcile_catalog": "environments",
    "ALIAS_SHIM_MARKER": "constants",
    "CACHE_DIR": "constants",
    "CACHE_ENV_DIR": "constants",
    "CACHE_SCRIPT_DIR": "constants",
    "CONFIG_FILENAME": "constants",
    "CatalogChange": "catalog",
    "DownloadError": "errors",
    "ENV_MARKER_NAME": "environments",
    "EnvBuildError": "errors",
    "ForeignAlias": "errors",
    "HASH_PREFIX": "constants",
    "HashChange": "manifest",
    "LOCAL_BIN_DIR": "constants",
    "MissingUv": "errors",
    "ProgressHook": "constants",
    "RESOURCE_BASE_URL": "constants",
    "Script": "model",
    "Scripts": "model",
    "UV_INSTALL_HINT": "constants",
    "UnknownCommand": "errors",
    "UsmError": "errors",
    "_build_env": "environments",
    "_bump_version": "manifest",
    "_env_python": "environments",
    "_null_hook": "constants",
    "alias_path": "aliases",
    "alias_status": "aliases",
    "audit_manifest": "manifest",
    "clean_cache": "catalog",
    "closest_names": "catalog",
    "compute_entry_hash": "manifest",
    "compute_script_hash": "manifest",
    "download_file": "catalog",
    "ensure_env": "environments",
    "ensure_script_file": "catalog",
    "env_ready": "environments",
    "has_cached_config": "catalog",
    "install_alias": "aliases",
    "iter_updates": "catalog",
    "load_scripts": "catalog",
    "local_bin_in_path": "aliases",
    "match_scripts": "catalog",
    "read_catalog_meta": "catalog",
    "resolve_script_path": "environments",
    "resolve_version": "version",
    "run_script": "environments",
    "short_hash": "catalog",
    "sync_manifest": "manifest",
    "uninstall_alias": "aliases",
    "update_config": "catalog",
}

_SUBMODULES = (
    "aliases",
    "catalog",
    "constants",
    "environments",
    "errors",
    "manifest",
    "model",
    "version",
)

if TYPE_CHECKING:  # pragma: no cover - typing/completion only
    from . import (
        aliases,
        catalog,
        constants,
        environments,
        errors,
        manifest,
        model,
        version,
    )
    from .aliases import (
        alias_path,
        alias_status,
        install_alias,
        local_bin_in_path,
        uninstall_alias,
    )
    from .catalog import (
        CatalogChange,
        clean_cache,
        closest_names,
        download_file,
        ensure_script_file,
        has_cached_config,
        iter_updates,
        load_scripts,
        match_scripts,
        read_catalog_meta,
        short_hash,
        update_config,
    )
    from .constants import (
        ALIAS_SHIM_MARKER,
        CACHE_DIR,
        CACHE_ENV_DIR,
        CACHE_SCRIPT_DIR,
        CONFIG_FILENAME,
        HASH_PREFIX,
        LOCAL_BIN_DIR,
        ProgressHook,
        RESOURCE_BASE_URL,
        UV_INSTALL_HINT,
        _null_hook,
    )
    from .environments import (
        ENV_MARKER_NAME,
        _build_env,
        _env_python,
        ensure_env,
        env_ready,
        resolve_script_path,
        run_script,
    )
    from .errors import (
        DownloadError,
        EnvBuildError,
        ForeignAlias,
        MissingUv,
        UnknownCommand,
        UsmError,
    )
    from .manifest import (
        HashChange,
        _bump_version,
        audit_manifest,
        compute_entry_hash,
        compute_script_hash,
        sync_manifest,
    )
    from .model import (
        Script,
        Scripts,
    )
    from .version import (
        resolve_version,
    )


def __getattr__(name: str):
    """Import the owning submodule on first use, then cache it."""
    if name in _SUBMODULES:
        value = importlib.import_module(f".{name}", __name__)
    elif name in _EXPORTS:
        module = importlib.import_module(f".{_EXPORTS[name]}", __name__)
        value = getattr(module, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_SUBMODULES))


__all__ = [
    "script_files_match",
    "reload_script",
    "reconcile_catalog",
    "aliases",
    "catalog",
    "constants",
    "environments",
    "errors",
    "manifest",
    "model",
    "version",
    "ALIAS_SHIM_MARKER",
    "CACHE_DIR",
    "CACHE_ENV_DIR",
    "CACHE_SCRIPT_DIR",
    "CONFIG_FILENAME",
    "ENV_MARKER_NAME",
    "HASH_PREFIX",
    "LOCAL_BIN_DIR",
    "RESOURCE_BASE_URL",
    "UV_INSTALL_HINT",
    "ProgressHook",
    "_null_hook",
    "DownloadError",
    "EnvBuildError",
    "ForeignAlias",
    "MissingUv",
    "UnknownCommand",
    "UsmError",
    "Script",
    "Scripts",
    "CatalogChange",
    "clean_cache",
    "closest_names",
    "download_file",
    "ensure_script_file",
    "has_cached_config",
    "iter_updates",
    "load_scripts",
    "match_scripts",
    "read_catalog_meta",
    "short_hash",
    "update_config",
    "ensure_env",
    "env_ready",
    "resolve_script_path",
    "run_script",
    "_build_env",
    "_env_python",
    "alias_path",
    "alias_status",
    "install_alias",
    "local_bin_in_path",
    "uninstall_alias",
    "resolve_version",
    "HashChange",
    "audit_manifest",
    "compute_entry_hash",
    "compute_script_hash",
    "sync_manifest",
    "_bump_version",
]
