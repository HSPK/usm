"""Pure SDK for the usm script catalog (UI-free: no click, no rich).

This package is split by responsibility into focused submodules:

* :mod:`~usmo.core.constants` – paths, URLs, the progress-hook type
* :mod:`~usmo.core.errors` – typed exceptions
* :mod:`~usmo.core.model` – the :class:`Script` dataclass
* :mod:`~usmo.core.catalog` – remote fetch, local cache, ``_config.json`` ops
* :mod:`~usmo.core.environments` – per-script virtualenvs and execution
* :mod:`~usmo.core.aliases` – ``~/.local/bin`` shim management
* :mod:`~usmo.core.updates` – version lookup and the auto-update probe
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
    updates,
)
from .constants import (
    ALIAS_SHIM_MARKER,
    AUTO_CHECK_ENV,
    CACHE_DIR,
    CACHE_ENV_DIR,
    CACHE_SCRIPT_DIR,
    CONFIG_FILENAME,
    DEFAULT_AUTO_CHECK_INTERVAL,
    HASH_PREFIX,
    LAST_CHECK_FILE,
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
from .updates import (
    VersionDiff,
    auto_check_interval,
    check_for_update,
    fetch_remote_script_versions,
    mark_checked,
    resolve_version,
    should_auto_check,
)

__all__ = [
    # submodules
    "aliases",
    "catalog",
    "constants",
    "environments",
    "errors",
    "manifest",
    "model",
    "updates",
    # constants & types
    "ALIAS_SHIM_MARKER",
    "AUTO_CHECK_ENV",
    "CACHE_DIR",
    "CACHE_ENV_DIR",
    "CACHE_SCRIPT_DIR",
    "CONFIG_FILENAME",
    "DEFAULT_AUTO_CHECK_INTERVAL",
    "ENV_MARKER_NAME",
    "HASH_PREFIX",
    "LAST_CHECK_FILE",
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
    # updates
    "VersionDiff",
    "auto_check_interval",
    "check_for_update",
    "fetch_remote_script_versions",
    "mark_checked",
    "resolve_version",
    "should_auto_check",
    # manifest
    "HashChange",
    "audit_manifest",
    "compute_script_hash",
    "sync_manifest",
    "_bump_version",
]
