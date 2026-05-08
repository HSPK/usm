"""Pytest configuration: redirect cache/config dirs to a temp path per session."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_usm_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Force every test to use a fresh ``HOME`` so caches/config don't leak."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reload modules that captured Path.home() at import time.
    import usmo.registry as registry
    import usmo.state as state

    importlib.reload(registry)
    importlib.reload(state)
    # installer/cli capture references to those modules' attributes,
    # so they need to be reloaded too.
    import usmo.installer as installer
    import usmo.runner as runner
    import usmo.cli as cli

    importlib.reload(installer)
    importlib.reload(runner)
    importlib.reload(cli)
    yield
