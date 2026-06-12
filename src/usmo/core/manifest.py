"""Manifest hashing and version bumping (used by the pre-commit hook)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import constants


def compute_script_hash(path: Path) -> str:
    """Return ``sha256:<hex>`` for the bytes of *path*."""
    return constants.HASH_PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()


def _bump_version(version: str | None, level: str = "patch") -> str:
    """Bump *version* by *level* ('patch'/'minor'/'major').

    Missing or non-semver versions return '1.0.0'.
    """
    if not version:
        return "1.0.0"
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "1.0.0"
    major, minor, patch = (int(p) for p in parts)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass(frozen=True)
class HashChange:
    name: str
    old_hash: str | None
    new_hash: str
    old_version: str | None
    new_version: str


def audit_manifest(
    config_path: Path,
    scripts_dir: Path | None = None,
    *,
    names: Iterable[str] | None = None,
    bump: str = "patch",
    force: bool = False,
) -> tuple[dict, list[HashChange]]:
    """Inspect the manifest and return ``(updated_data, changes)``.

    Each script entry is hashed and compared with its declared ``hash``;
    drift (or a missing ``version``) triggers a version bump. ``names``
    restricts the operation to the given script keys, and ``force=True``
    bumps even when the hash already matches (used when the user explicitly
    requests a version bump).

    Raises ``KeyError`` if any name in *names* is not declared in the
    manifest.
    """
    scripts_dir = scripts_dir or config_path.parent
    data = json.loads(config_path.read_text())
    entries = data.get("scripts", {})
    targets = set(names) if names else None

    if targets is not None:
        unknown = targets - set(entries)
        if unknown:
            raise KeyError(
                f"Unknown script(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(entries))}"
            )

    changes: list[HashChange] = []
    for name, entry in entries.items():
        if targets is not None and name not in targets:
            continue
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        target = scripts_dir / entry["path"]
        if not target.exists():
            continue

        new_hash = compute_script_hash(target)
        old_hash = entry.get("hash")
        old_version = entry.get("version")
        hash_matches = new_hash == old_hash

        if hash_matches and old_version and not force:
            continue

        new_version = _bump_version(old_version, bump)
        entry["version"] = new_version
        entry["hash"] = new_hash
        changes.append(HashChange(name, old_hash, new_hash, old_version, new_version))

    return data, changes


def sync_manifest(
    config_path: Path,
    scripts_dir: Path | None = None,
    *,
    names: Iterable[str] | None = None,
    bump: str = "patch",
    force: bool = False,
    check_only: bool = False,
) -> list[HashChange]:
    """Update the manifest in place; return the list of changes.

    With ``check_only=True``, the file is not touched. Other keyword args
    forward to :func:`audit_manifest`.
    """
    data, changes = audit_manifest(
        config_path, scripts_dir, names=names, bump=bump, force=force
    )
    if changes and not check_only:
        config_path.write_text(json.dumps(data, indent=2) + "\n")
    return changes
