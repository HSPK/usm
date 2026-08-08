"""Top-level usmo package: re-exports the SDK from :mod:`usmo.core`.

Resolved lazily (PEP 562): the ``usm`` CLI imports this package on every
invocation, but only some commands need the SDK, so pulling it in eagerly
made every run pay for the catalog, alias and environment modules.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing/completion only
    from usmo import core  # noqa: F401
    from usmo.core import (  # noqa: F401
        CACHE_DIR,
        CACHE_ENV_DIR,
        CACHE_SCRIPT_DIR,
        CONFIG_FILENAME,
        HASH_PREFIX,
        UV_INSTALL_HINT,
        DownloadError,
        EnvBuildError,
        HashChange,
        MissingUv,
        Script,
        Scripts,
        UnknownCommand,
        UsmError,
        audit_manifest,
        clean_cache,
        compute_script_hash,
        download_file,
        ensure_env,
        ensure_script_file,
        iter_updates,
        load_scripts,
        resolve_script_path,
        resolve_version,
        run_script,
        sync_manifest,
    )


def __getattr__(name: str):
    if name == "core":
        value = importlib.import_module("usmo.core")
    elif name in __all__:
        value = getattr(importlib.import_module("usmo.core"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | {"core", "cli"})


__all__ = [
    "CACHE_DIR",
    "CACHE_ENV_DIR",
    "CACHE_SCRIPT_DIR",
    "CONFIG_FILENAME",
    "HASH_PREFIX",
    "UV_INSTALL_HINT",
    "DownloadError",
    "EnvBuildError",
    "HashChange",
    "MissingUv",
    "Script",
    "Scripts",
    "UnknownCommand",
    "UsmError",
    "audit_manifest",
    "clean_cache",
    "compute_script_hash",
    "download_file",
    "ensure_env",
    "ensure_script_file",
    "iter_updates",
    "load_scripts",
    "resolve_script_path",
    "resolve_version",
    "run_script",
    "sync_manifest",
]
