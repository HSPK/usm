"""Registry index parsing and normalization.

Supports two on-disk schemas for ``_config.json``:

* **v1 (legacy)** – the original format used by usm:

    .. code-block:: json

        {"scripts": {"<name>": {"description": "...", "path": "..."}}}

* **v2** – the versioned package registry:

    .. code-block:: json

        {
          "schema_version": 2,
          "registry": {"name": "default"},
          "packages": {
            "<name>": {
              "description": "...",
              "latest": "1.0.0",
              "versions": {
                "1.0.0": {
                  "type": "script",
                  "path": "init.sh",
                  "sha256": "<hex>",
                  "size": 1234,
                  "requires_python": null,
                  "depends": []
                }
              }
            }
          }
        }

Internally we always work with the v2 representation. v1 documents are
upgraded in-memory by synthesising a single ``"0.0.0"`` version with no
hash – downloads from such a registry will succeed but emit a warning
because integrity cannot be verified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

LEGACY_VERSION = "0.0.0"
SUPPORTED_TYPES = {"script", "archive"}


@dataclass(frozen=True)
class PackageVersion:
    """One concrete release of a package in a registry index."""

    version: str
    type: str
    path: str
    sha256: str | None = None
    size: int | None = None
    requires_python: str | None = None
    depends: tuple[str, ...] = ()
    entry: str | None = None  # for type=="archive": entry script inside the archive
    # PEP 508 requirement strings to install in a per-package virtualenv. When
    # non-empty, ``usm`` provisions a venv at ``~/.cache/usm/venvs/<name>/<ver>``
    # (much like pipx) and runs the entry script with that venv's interpreter.
    pip_requires: tuple[str, ...] = ()

    def parsed_version(self) -> Version:
        try:
            return Version(self.version)
        except InvalidVersion:
            # Allow non-PEP440 strings to compare lexicographically.
            return Version("0!0+" + self.version.replace("-", "."))


@dataclass
class Package:
    name: str
    description: str
    latest: str
    versions: dict[str, PackageVersion] = field(default_factory=dict)

    def get(self, version: str | None) -> PackageVersion:
        ver = version or self.latest
        if ver not in self.versions:
            available = ", ".join(sorted(self.versions))
            raise KeyError(
                f"Version '{ver}' not found for package '{self.name}'. "
                f"Available: {available}"
            )
        return self.versions[ver]

    def sorted_versions(self) -> list[PackageVersion]:
        return sorted(self.versions.values(), key=lambda v: v.parsed_version())


@dataclass
class Index:
    """Parsed registry index."""

    schema_version: int
    registry_name: str
    packages: dict[str, Package]

    def __contains__(self, name: str) -> bool:
        return name in self.packages

    def __getitem__(self, name: str) -> Package:
        return self.packages[name]


def _coerce_version_entry(name: str, version: str, raw: Any) -> PackageVersion:
    if not isinstance(raw, dict):
        raise ValueError(
            f"Invalid version entry for {name}@{version}: expected object, got {type(raw).__name__}"
        )
    vtype = raw.get("type", "script")
    if vtype not in SUPPORTED_TYPES:
        raise ValueError(
            f"Unsupported package type '{vtype}' for {name}@{version}; "
            f"supported types are: {sorted(SUPPORTED_TYPES)}"
        )
    path = raw.get("path")
    if not path:
        raise ValueError(f"Missing 'path' for {name}@{version}")
    depends = tuple(raw.get("depends", ()) or ())
    pip_requires = tuple(raw.get("pip_requires", ()) or ())
    return PackageVersion(
        version=version,
        type=vtype,
        path=path,
        sha256=raw.get("sha256"),
        size=raw.get("size"),
        requires_python=raw.get("requires_python"),
        depends=depends,
        entry=raw.get("entry"),
        pip_requires=pip_requires,
    )


def normalize(data: dict[str, Any], *, registry_name: str = "default") -> Index:
    """Return an ``Index`` regardless of whether ``data`` is v1 or v2."""
    if not isinstance(data, dict):
        raise ValueError("Index document must be a JSON object")

    if "scripts" in data and "packages" not in data:
        # v1 legacy
        packages: dict[str, Package] = {}
        for name, info in (data.get("scripts") or {}).items():
            if not isinstance(info, dict) or "path" not in info:
                raise ValueError(f"Invalid v1 entry for '{name}'")
            ver = PackageVersion(
                version=LEGACY_VERSION,
                type="script",
                path=info["path"],
                sha256=info.get("sha256"),
            )
            packages[name] = Package(
                name=name,
                description=info.get("description", ""),
                latest=LEGACY_VERSION,
                versions={LEGACY_VERSION: ver},
            )
        return Index(schema_version=1, registry_name=registry_name, packages=packages)

    if data.get("schema_version") not in (2, None):
        raise ValueError(f"Unsupported schema_version: {data.get('schema_version')}")

    raw_pkgs = data.get("packages") or {}
    packages = {}
    for name, info in raw_pkgs.items():
        if not isinstance(info, dict):
            raise ValueError(f"Invalid package entry for '{name}'")
        versions_raw = info.get("versions") or {}
        if not versions_raw:
            raise ValueError(f"Package '{name}' has no versions")
        versions = {
            ver: _coerce_version_entry(name, ver, body)
            for ver, body in versions_raw.items()
        }
        latest = info.get("latest")
        if latest is None:
            latest = max(versions.values(), key=lambda v: v.parsed_version()).version
        if latest not in versions:
            raise ValueError(
                f"Package '{name}' declares latest='{latest}' but that version is missing"
            )
        packages[name] = Package(
            name=name,
            description=info.get("description", ""),
            latest=latest,
            versions=versions,
        )

    reg_meta = data.get("registry") or {}
    return Index(
        schema_version=2,
        registry_name=reg_meta.get("name", registry_name),
        packages=packages,
    )


def load_index(path: Path, *, registry_name: str = "default") -> Index:
    """Load and normalise an index from a JSON file."""
    return normalize(
        json.loads(path.read_text(encoding="utf-8")), registry_name=registry_name
    )
