"""Installed-version lookup for the usmo package."""

from __future__ import annotations


def resolve_version() -> str:
    """Return the installed usmo version, or a sentinel when unknown."""
    try:
        from usmo._version import __version__ as ver

        return ver
    except ImportError:
        pass
    try:
        # Imported lazily: importlib.metadata costs ~17ms and is only needed
        # when the generated _version module is absent (editable installs).
        from importlib.metadata import version as pkg_version

        return pkg_version("usmo")
    except Exception:
        return "unknown (editable install without build)"
