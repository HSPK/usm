"""Shared constants and the progress-hook type used across the SDK.

Path constants are intentionally module-level so tests can redirect the cache
by patching ``usmo.core.constants`` (every consumer reads them live via the
``constants`` module rather than binding a private copy).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
CACHE_ENV_DIR = CACHE_DIR / "envs"
LAST_CHECK_FILE = CACHE_DIR / ".last_check"
CONFIG_FILENAME = "_config.json"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/scripts/"
UV_INSTALL_HINT = "https://docs.astral.sh/uv/#installation"
LOCAL_BIN_DIR = Path.home() / ".local" / "bin"
ALIAS_SHIM_MARKER = "usm-managed alias shim"
AUTO_CHECK_ENV = "USM_AUTO_CHECK_INTERVAL"
DEFAULT_AUTO_CHECK_INTERVAL = 86400  # 24h, in seconds. 0 disables.
HASH_PREFIX = "sha256:"

ProgressHook = Callable[[str], None]


def _null_hook(_filename: str) -> None:
    pass
