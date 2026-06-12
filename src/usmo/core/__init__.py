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
"""

from __future__ import annotations

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
from .constants import (
    ALIAS_SHIM_MARKER,
    CACHE_DIR,
    CACHE_ENV_DIR,
    CACHE_SCRIPT_DIR,
    CONFIG_FILENAME,
    HASH_PREFIX,
    LOCAL_BIN_DIR,
    RESOURCE_BASE_URL,
    UV_INSTALL_HINT,
    ProgressHook,
    _null_hook,
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
    download_file,
    ensure_script_file,
    has_cached_config,
    iter_updates,
    load_scripts,
    read_catalog_meta,
    short_hash,
    update_config,
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
    compute_script_hash,
    sync_manifest,
)
from .model import Script, Scripts
from .version import resolve_version

__all__ = [
    # submodules
    "aliases",
    "catalog",
    "constants",
    "environments",
    "errors",
    "manifest",
    "model",
    "version",
    # constants & types
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
    # errors
    "DownloadError",
    "EnvBuildError",
    "ForeignAlias",
    "MissingUv",
    "UnknownCommand",
    "UsmError",
    # model
    "Script",
    "Scripts",
    # catalog
    "CatalogChange",
    "clean_cache",
    "download_file",
    "ensure_script_file",
    "has_cached_config",
    "iter_updates",
    "load_scripts",
    "read_catalog_meta",
    "short_hash",
    "update_config",
    # environments
    "ensure_env",
    "env_ready",
    "resolve_script_path",
    "run_script",
    "_build_env",
    "_env_python",
    # aliases
    "alias_path",
    "alias_status",
    "install_alias",
    "local_bin_in_path",
    "uninstall_alias",
    # version
    "resolve_version",
    # manifest
    "HashChange",
    "audit_manifest",
    "compute_script_hash",
    "sync_manifest",
    "_bump_version",
]
