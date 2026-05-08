"""Tests for the install/uninstall/upgrade flow using ``debug_local`` mode."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _write_index(scripts_dir: Path, packages: dict) -> Path:
    payload = {
        "schema_version": 2,
        "registry": {"name": "test"},
        "packages": packages,
    }
    out = scripts_dir / "_config.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


@pytest.fixture()
def scripts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scripts"
    d.mkdir()
    return d


def _make_script(scripts_dir: Path, name: str, body: str) -> Path:
    f = scripts_dir / name
    f.write_text(body)
    return f


def test_install_and_run_script(scripts_dir: Path) -> None:
    from usmo import installer, registry, runner, state

    f = _make_script(scripts_dir, "hello.sh", "#!/usr/bin/env bash\necho hi from $1\n")
    _write_index(
        scripts_dir,
        {
            "hello": {
                "description": "say hi",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "hello.sh",
                        "sha256": _sha256(f),
                    }
                },
            }
        },
    )
    config = registry.load_config()
    res = installer.install(config, "hello", debug_local=scripts_dir)
    assert res.version == "1.0.0"
    assert res.entry.exists()

    pkg = state.get("hello")
    assert pkg is not None
    assert pkg.version == "1.0.0"

    # Re-install without --force is a no-op.
    res2 = installer.install(config, "hello", debug_local=scripts_dir)
    assert res2.install_dir == res.install_dir

    # Run the script and check exit code.
    rc = runner.run_installed("hello", ["world"])
    assert rc == 0


def test_sha256_mismatch_rejected(scripts_dir: Path) -> None:
    from usmo import installer, registry

    _make_script(scripts_dir, "bad.sh", "echo bad\n")
    _write_index(
        scripts_dir,
        {
            "bad": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "bad.sh",
                        "sha256": "0" * 64,
                    }
                },
            }
        },
    )
    config = registry.load_config()
    with pytest.raises(installer.InstallError, match="sha256 mismatch"):
        installer.install(config, "bad", debug_local=scripts_dir)


def test_install_specific_version_then_upgrade(scripts_dir: Path) -> None:
    from usmo import installer, registry, state

    f1 = _make_script(scripts_dir, "v1.sh", "echo v1\n")
    f2 = _make_script(scripts_dir, "v2.sh", "echo v2\n")
    _write_index(
        scripts_dir,
        {
            "tool": {
                "description": "",
                "latest": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "v1.sh",
                        "sha256": _sha256(f1),
                    },
                    "2.0.0": {
                        "type": "script",
                        "path": "v2.sh",
                        "sha256": _sha256(f2),
                    },
                },
            }
        },
    )
    config = registry.load_config()
    installer.install(config, "tool", version="1.0.0", debug_local=scripts_dir)
    assert state.get("tool").version == "1.0.0"

    res = installer.upgrade(config, "tool", debug_local=scripts_dir)
    assert res is not None
    assert res.version == "2.0.0"
    assert state.get("tool").version == "2.0.0"

    # Once at latest, upgrade is a no-op.
    assert installer.upgrade(config, "tool", debug_local=scripts_dir) is None


def test_uninstall_removes_state_and_files(scripts_dir: Path) -> None:
    from usmo import installer, registry, state

    f = _make_script(scripts_dir, "x.sh", "echo x\n")
    _write_index(
        scripts_dir,
        {
            "x": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "x.sh",
                        "sha256": _sha256(f),
                    }
                },
            }
        },
    )
    config = registry.load_config()
    res = installer.install(config, "x", debug_local=scripts_dir)
    assert res.install_dir.exists()
    assert installer.uninstall("x") is True
    assert state.get("x") is None
    assert not res.install_dir.exists()
    # Already gone -> False.
    assert installer.uninstall("x") is False


def test_install_archive_with_usm_toml(tmp_path: Path, scripts_dir: Path) -> None:
    from usmo import installer, registry, runner

    # Build a tar.gz archive containing usm.toml + an entry script.
    work = tmp_path / "pkg-1.0.0"
    work.mkdir()
    (work / "usm.toml").write_text('entry = "main.sh"\n')
    (work / "main.sh").write_text("#!/usr/bin/env bash\necho archive-ok\n")
    archive = scripts_dir / "pkg-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname="pkg-1.0.0")

    _write_index(
        scripts_dir,
        {
            "pkg": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": "pkg-1.0.0.tar.gz",
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )
    config = registry.load_config()
    res = installer.install(config, "pkg", debug_local=scripts_dir)
    assert res.entry.exists()
    assert res.entry.name == "main.sh"
    assert runner.run_installed("pkg", []) == 0


def test_archive_path_traversal_rejected(tmp_path: Path, scripts_dir: Path) -> None:
    from usmo import installer, registry

    archive = scripts_dir / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        # Add a member with a path that escapes the destination.
        info = tarfile.TarInfo(name="../escape.sh")
        data = b"echo pwned\n"
        info.size = len(data)
        import io

        tar.addfile(info, io.BytesIO(data))

    _write_index(
        scripts_dir,
        {
            "evil": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": "evil.tar.gz",
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )
    config = registry.load_config()
    with pytest.raises(installer.InstallError, match="unsafe path"):
        installer.install(config, "evil", debug_local=scripts_dir)


def test_resolve_unknown_package(scripts_dir: Path) -> None:
    from usmo import installer, registry

    _write_index(scripts_dir, {})
    config = registry.load_config()
    with pytest.raises(installer.InstallError, match="not found"):
        installer.install(config, "nope", debug_local=scripts_dir)
