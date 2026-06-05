"""Shared pytest fixtures for the usmo test suite."""

from __future__ import annotations

import pytest

from usmo import core


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect core's CACHE_* paths to a fresh tmp dir for the test."""
    cache_dir = tmp_path / "cache"
    scripts_dir = cache_dir / "scripts"
    last_check = cache_dir / ".last_check"
    monkeypatch.setattr(core, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(core, "CACHE_SCRIPT_DIR", scripts_dir)
    monkeypatch.setattr(core, "LAST_CHECK_FILE", last_check)
    yield cache_dir
