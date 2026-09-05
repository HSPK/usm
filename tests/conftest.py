"""Shared pytest fixtures for the usmo test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from usmo import core

# Make scripts/ importable (so tests can `import openai_proxy`).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
pytest_plugins = ["azsync_support"]


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect core's CACHE_* paths to a fresh tmp dir for the test."""
    cache_dir = tmp_path / "cache"
    scripts_dir = cache_dir / "scripts"
    envs_dir = cache_dir / "envs"
    # SDK functions read these live from usmo.core.constants; tests read them
    # via the usmo.core facade. Patch both so they stay consistent.
    for name, value in [
        ("CACHE_DIR", cache_dir),
        ("CACHE_SCRIPT_DIR", scripts_dir),
        ("CACHE_ENV_DIR", envs_dir),
    ]:
        monkeypatch.setattr(core.constants, name, value)
        monkeypatch.setattr(core, name, value)
    yield cache_dir


#: Real locations that a test must never write into. A service-managing tool
#: writes unit files for a living, so an ineffective monkeypatch does not
#: fail loudly -- it quietly installs something on the developer's machine.
#: This happened once (a moved symbol left a fixture patching the wrong
#: module), and `usm doctor` found the leftover unit days later.
_REAL_SERVICE_DIRS = (
    Path.home() / ".config" / "systemd" / "user",
    Path.home() / "Library" / "LaunchAgents",
)


def _listing(path: Path) -> set[str]:
    try:
        return {p.name for p in path.iterdir()}
    except OSError:
        return set()


@pytest.fixture(autouse=True)
def no_writes_to_the_real_system():
    """Fail any test that creates a unit file on the actual machine."""
    before = {path: _listing(path) for path in _REAL_SERVICE_DIRS}
    yield
    for path, names in before.items():
        new = _listing(path) - names
        if new:
            for name in new:
                (path / name).unlink(missing_ok=True)
            raise AssertionError(
                f"test wrote {sorted(new)} into {path} — redirect it with a "
                "fixture instead of touching the real machine"
            )
