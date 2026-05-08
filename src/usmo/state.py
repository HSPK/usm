"""Persistent state for installed packages.

Stored as JSON at ``~/.cache/usm/state.json``::

    {
      "schema_version": 1,
      "installed": {
        "<pkg>": {
          "version": "1.0.0",
          "registry": "default",
          "type": "script",
          "install_dir": "/abs/path",
          "entry": "init.sh",
          "sha256": "...",
          "installed_at": "2026-01-01T00:00:00Z"
        }
      }
    }
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .registry import CACHE_DIR

STATE_PATH = CACHE_DIR / "state.json"
STATE_SCHEMA_VERSION = 1


@dataclass
class InstalledPackage:
    name: str
    version: str
    registry: str
    type: str
    install_dir: str
    entry: str
    sha256: str | None = None
    installed_at: str = ""


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path | None = None) -> dict[str, InstalledPackage]:
    p = path or STATE_PATH
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported state schema_version: {data.get('schema_version')}"
        )
    out: dict[str, InstalledPackage] = {}
    for name, info in (data.get("installed") or {}).items():
        out[name] = InstalledPackage(name=name, **info)
    return out


def save(state: dict[str, InstalledPackage], path: Path | None = None) -> Path:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "installed": {
            name: {k: v for k, v in asdict(pkg).items() if k != "name"}
            for name, pkg in state.items()
        },
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def record(
    name: str,
    *,
    version: str,
    registry: str,
    type: str,
    install_dir: Path,
    entry: str,
    sha256: str | None,
    path: Path | None = None,
) -> InstalledPackage:
    """Add or replace an entry in the state file and return it."""
    state = load(path)
    pkg = InstalledPackage(
        name=name,
        version=version,
        registry=registry,
        type=type,
        install_dir=str(install_dir),
        entry=entry,
        sha256=sha256,
        installed_at=_now(),
    )
    state[name] = pkg
    save(state, path)
    return pkg


def remove(name: str, path: Path | None = None) -> InstalledPackage | None:
    state = load(path)
    pkg = state.pop(name, None)
    save(state, path)
    return pkg


def get(name: str, path: Path | None = None) -> InstalledPackage | None:
    return load(path).get(name)
