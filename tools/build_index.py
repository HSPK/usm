"""Generate a v2 ``_config.json`` index from a local ``scripts/`` directory.

Usage::

    python tools/build_index.py [--scripts-dir scripts] [--output scripts/_config.json]

The script merges any pre-existing version metadata in ``_config.json``
with newly-computed sha256/size values for every script that exists on
disk. Each script becomes a package whose ``latest`` version is the
highest one declared in ``versions.toml`` (if present) or ``0.1.0`` by
default.

A simple ``versions.toml`` next to the index can pin descriptions,
version numbers and command names per-script. Keys are matched against
the file stem (e.g. ``inject_alias``) and the published name (e.g.
``inject-alias``)::

    [inject_alias]
    name = "inject-alias"
    description = "Insert or update the managed usm alias block."
    version = "0.1.0"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_VERSION = "0.1.0"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_meta(meta_path: Path) -> dict[str, dict]:
    if not meta_path.exists():
        return {}
    return tomllib.loads(meta_path.read_text(encoding="utf-8"))


def _load_existing(out_path: Path) -> dict[str, dict]:
    """Return existing per-package descriptions/versions if available."""
    if not out_path.exists():
        return {}
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if data.get("schema_version") == 2:
        return data.get("packages") or {}
    out: dict[str, dict] = {}
    for name, info in (data.get("scripts") or {}).items():
        out[name] = {
            "description": info.get("description", ""),
            "latest": DEFAULT_VERSION,
            "versions": {
                DEFAULT_VERSION: {
                    "type": "script",
                    "path": info.get("path"),
                }
            },
        }
    return out


def _resolve_name(stem: str, per_file_meta: dict) -> str:
    if per_file_meta.get("name"):
        return per_file_meta["name"]
    # Default: file stem; underscore preserved (matches existing names).
    return stem


def build(
    scripts_dir: Path,
    out_path: Path,
    *,
    meta_path: Path | None = None,
    registry_name: str = "default",
) -> None:
    meta = _load_meta(meta_path or scripts_dir / "versions.toml")
    existing = _load_existing(out_path)

    packages: dict[str, dict] = {}
    skip = {"_config.json", "versions.toml", "install.sh"}
    for entry in sorted(scripts_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name.startswith(".") or entry.name in skip:
            continue
        per_file_meta = meta.get(entry.stem) or {}
        name = _resolve_name(entry.stem, per_file_meta)
        # Allow looking up by published name as well.
        if not per_file_meta:
            per_file_meta = meta.get(name) or {}
        existing_pkg = existing.get(name) or existing.get(entry.stem)
        version = per_file_meta.get("version") or _guess_version(existing_pkg)
        description = (
            per_file_meta.get("description")
            or (existing_pkg or {}).get("description")
            or ""
        )
        sha = sha256_of(entry)
        size = entry.stat().st_size
        version_entry: dict = {
            "type": "script",
            "path": entry.name,
            "sha256": sha,
            "size": size,
        }
        pip_requires = per_file_meta.get("pip_requires") or []
        if pip_requires:
            version_entry["pip_requires"] = list(pip_requires)
        packages[name] = {
            "description": description,
            "latest": version,
            "versions": {version: version_entry},
        }

    payload = {
        "schema_version": 2,
        "registry": {"name": registry_name},
        "packages": packages,
    }
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {out_path} with {len(packages)} package(s)")


def _guess_version(existing_pkg: dict | None) -> str:
    if not existing_pkg:
        return DEFAULT_VERSION
    return existing_pkg.get("latest") or DEFAULT_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-dir", type=Path, default=Path("scripts"))
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (defaults to <scripts-dir>/_config.json).",
    )
    parser.add_argument(
        "--meta",
        type=Path,
        default=None,
        help="Path to versions.toml (defaults to <scripts-dir>/versions.toml).",
    )
    parser.add_argument("--registry-name", default="default")
    args = parser.parse_args(argv)

    out = args.output or (args.scripts_dir / "_config.json")
    build(
        args.scripts_dir,
        out,
        meta_path=args.meta,
        registry_name=args.registry_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
