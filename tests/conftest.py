"""Shared pytest fixtures for the usmo test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from usmo import core

# Make scripts/ importable (so tests can `import openai_proxy`).
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect core's CACHE_* paths to a fresh tmp dir for the test."""
    cache_dir = tmp_path / "cache"
    scripts_dir = cache_dir / "scripts"
    envs_dir = cache_dir / "envs"
    last_check = cache_dir / ".last_check"
    # SDK functions read these live from usmo.core.constants; tests read them
    # via the usmo.core facade. Patch both so they stay consistent.
    for name, value in [
        ("CACHE_DIR", cache_dir),
        ("CACHE_SCRIPT_DIR", scripts_dir),
        ("CACHE_ENV_DIR", envs_dir),
        ("LAST_CHECK_FILE", last_check),
    ]:
        monkeypatch.setattr(core.constants, name, value)
        monkeypatch.setattr(core, name, value)
    yield cache_dir
