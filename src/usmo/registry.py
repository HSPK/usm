"""Registry configuration and remote index fetching.

A registry is identified by a short ``id`` and is backed by a base URL
that hosts ``_config.json`` and individual package files.

User config lives at ``~/.config/usm/config.toml``::

    default_registry = "default"

    [[registries]]
    id = "default"
    url = "https://raw.githubusercontent.com/HSPK/usm/main/scripts/"

Multiple registries can be declared; ``usm`` searches them in declared
order when resolving a package name. The first registry that contains
the requested package wins.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from . import manifest

CACHE_DIR = Path.home() / ".cache" / "usm"
INDEX_CACHE_DIR = CACHE_DIR / "index"
PACKAGES_DIR = CACHE_DIR / "packages"
CONFIG_DIR = Path.home() / ".config" / "usm"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_REGISTRY_ID = "default"
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/HSPK/usm/main/scripts/"
INDEX_FILENAME = "_config.json"


@dataclass(frozen=True)
class Registry:
    id: str
    url: str

    @property
    def base_url(self) -> str:
        return self.url if self.url.endswith("/") else self.url + "/"

    def file_url(self, filename: str) -> str:
        return self.base_url + filename

    def cache_dir(self) -> Path:
        return INDEX_CACHE_DIR / self.id

    def index_path(self) -> Path:
        return self.cache_dir() / INDEX_FILENAME


@dataclass
class RegistryConfig:
    registries: list[Registry]
    default_registry: str

    def get(self, identifier: str) -> Registry:
        for r in self.registries:
            if r.id == identifier:
                return r
        raise KeyError(f"Registry '{identifier}' is not configured")

    def iter_search_order(self, preferred: str | None = None) -> Iterable[Registry]:
        seen: set[str] = set()
        if preferred:
            try:
                first = self.get(preferred)
                seen.add(first.id)
                yield first
            except KeyError:
                pass
        for r in self.registries:
            if r.id in seen:
                continue
            yield r
            seen.add(r.id)


def _default_config() -> RegistryConfig:
    return RegistryConfig(
        registries=[Registry(id=DEFAULT_REGISTRY_ID, url=DEFAULT_REGISTRY_URL)],
        default_registry=DEFAULT_REGISTRY_ID,
    )


def load_config(path: Path | None = None) -> RegistryConfig:
    """Load registry configuration, falling back to the built-in default."""
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        return _default_config()
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse {cfg_path}: {exc}") from exc

    raw_regs = data.get("registries") or []
    registries: list[Registry] = []
    for entry in raw_regs:
        if "id" not in entry or "url" not in entry:
            raise ValueError(f"Each registry entry needs 'id' and 'url'; got {entry!r}")
        registries.append(Registry(id=entry["id"], url=entry["url"]))
    if not registries:
        registries = [Registry(id=DEFAULT_REGISTRY_ID, url=DEFAULT_REGISTRY_URL)]
    default = data.get("default_registry") or registries[0].id
    if not any(r.id == default for r in registries):
        raise ValueError(
            f"default_registry='{default}' does not match any configured registry"
        )
    return RegistryConfig(registries=registries, default_registry=default)


def save_config(config: RegistryConfig, path: Path | None = None) -> Path:
    """Persist the registry configuration as TOML."""
    cfg_path = path or CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'default_registry = "{config.default_registry}"', ""]
    for r in config.registries:
        lines.append("[[registries]]")
        lines.append(f'id = "{r.id}"')
        lines.append(f'url = "{r.url}"')
        lines.append("")
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    return cfg_path


# ---------------------------------------------------------------------------
# Index fetching
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int = 30) -> bytes:
    import requests

    response = requests.get(url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"GET {url} failed with status {response.status_code}")
    return response.content


def fetch_index(
    registry: Registry,
    *,
    refresh: bool = False,
    debug_local: Path | None = None,
) -> manifest.Index:
    """Return the parsed index for ``registry``.

    When ``debug_local`` is given (e.g. a path to ``./scripts``), the
    index is read from that directory instead of being downloaded.
    """
    if debug_local is not None:
        return manifest.load_index(
            debug_local / INDEX_FILENAME, registry_name=registry.id
        )

    cache = registry.index_path()
    if refresh or not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_http_get(registry.file_url(INDEX_FILENAME)))
    return manifest.load_index(cache, registry_name=registry.id)


def fetch_file(registry: Registry, filename: str, dest: Path) -> Path:
    """Download a file from ``registry`` to ``dest`` and return the path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_http_get(registry.file_url(filename)))
    return dest
