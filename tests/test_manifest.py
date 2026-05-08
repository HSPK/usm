"""Unit tests for the manifest / index normaliser."""

from __future__ import annotations

import pytest

from usmo.manifest import LEGACY_VERSION, normalize


def test_normalize_v1_legacy() -> None:
    data = {
        "scripts": {
            "init": {"description": "init machine", "path": "init.sh"},
            "cu122": {"description": "cuda", "path": "cu122.sh"},
        }
    }
    idx = normalize(data, registry_name="legacy")
    assert idx.schema_version == 1
    assert idx.registry_name == "legacy"
    assert set(idx.packages) == {"init", "cu122"}
    init = idx["init"]
    assert init.latest == LEGACY_VERSION
    pv = init.get(None)
    assert pv.path == "init.sh"
    assert pv.sha256 is None
    assert pv.type == "script"


def test_normalize_v2() -> None:
    data = {
        "schema_version": 2,
        "registry": {"name": "default"},
        "packages": {
            "demo": {
                "description": "d",
                "latest": "1.0.0",
                "versions": {
                    "0.9.0": {"type": "script", "path": "demo.sh", "sha256": "a"},
                    "1.0.0": {"type": "script", "path": "demo.sh", "sha256": "b"},
                },
            }
        },
    }
    idx = normalize(data)
    pkg = idx["demo"]
    assert pkg.latest == "1.0.0"
    versions = [v.version for v in pkg.sorted_versions()]
    assert versions == ["0.9.0", "1.0.0"]
    assert pkg.get("0.9.0").sha256 == "a"


def test_normalize_v2_auto_latest() -> None:
    data = {
        "schema_version": 2,
        "packages": {
            "demo": {
                "description": "",
                "versions": {
                    "0.1.0": {"type": "script", "path": "p"},
                    "0.2.0": {"type": "script", "path": "p"},
                },
            }
        },
    }
    idx = normalize(data)
    assert idx["demo"].latest == "0.2.0"


def test_invalid_latest_rejected() -> None:
    data = {
        "schema_version": 2,
        "packages": {
            "demo": {
                "latest": "9.9.9",
                "versions": {"0.1.0": {"type": "script", "path": "p"}},
            }
        },
    }
    with pytest.raises(ValueError):
        normalize(data)


def test_unsupported_type_rejected() -> None:
    data = {
        "schema_version": 2,
        "packages": {
            "demo": {
                "latest": "0.1.0",
                "versions": {
                    "0.1.0": {"type": "wheel", "path": "p"},
                },
            }
        },
    }
    with pytest.raises(ValueError):
        normalize(data)


def test_missing_path_rejected() -> None:
    data = {
        "schema_version": 2,
        "packages": {
            "demo": {
                "latest": "0.1.0",
                "versions": {"0.1.0": {"type": "script"}},
            }
        },
    }
    with pytest.raises(ValueError):
        normalize(data)


def test_unsupported_schema_version_rejected() -> None:
    with pytest.raises(ValueError):
        normalize({"schema_version": 99, "packages": {}})
