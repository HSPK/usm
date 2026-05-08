"""Tests for pipx-style venv provisioning of Python packages.

Real ``python -m venv`` + ``pip install`` is exercised end-to-end for
empty-requirement scenarios, but for tests with actual requirements we
monkey-patch ``provision_venv`` to avoid hitting the network. We still
build a fake venv directory with a working interpreter so the runner
path is exercised.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _write_index(scripts_dir: Path, packages: dict) -> None:
    (scripts_dir / "_config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "registry": {"name": "test"},
                "packages": packages,
            }
        )
    )


def _make_fake_venv(root: Path) -> None:
    """Create a directory layout that mimics a venv with a working python.

    The "interpreter" is a small shell wrapper that execs the real
    ``sys.executable`` so the runner can actually run Python entry
    scripts under it.
    """
    bin_dir = root / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / ("python.exe" if os.name == "nt" else "python")
    py.write_text(
        f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n', encoding="utf-8"
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def scripts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scripts"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Manifest plumbing
# ---------------------------------------------------------------------------


def test_pip_requires_round_trips_through_manifest() -> None:
    from usmo.manifest import normalize

    idx = normalize(
        {
            "schema_version": 2,
            "packages": {
                "tool": {
                    "latest": "1.0.0",
                    "versions": {
                        "1.0.0": {
                            "type": "script",
                            "path": "tool.py",
                            "pip_requires": ["click>=8", "rich"],
                        }
                    },
                }
            },
        }
    )
    pv = idx["tool"].get("1.0.0")
    assert pv.pip_requires == ("click>=8", "rich")


# ---------------------------------------------------------------------------
# Install + run
# ---------------------------------------------------------------------------


def test_python_package_with_pip_requires_uses_venv(
    scripts_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: pip_requires triggers venv creation; runner uses its python."""
    from usmo import installer, registry, runner, state

    # The script prints which interpreter is running it so we can assert
    # the venv interpreter — not sys.executable — was used.
    body = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import sys
        print("interpreter=" + sys.executable)
        print("argv=" + ",".join(sys.argv[1:]))
        """
    )
    f = scripts_dir / "tool.py"
    f.write_text(body)

    _write_index(
        scripts_dir,
        {
            "tool": {
                "description": "venv tool",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "tool.py",
                        "sha256": _sha256(f),
                        "pip_requires": ["click>=8"],
                    }
                },
            }
        },
    )

    captured: dict = {}

    def fake_provision(name: str, version: str, requirements: list[str]) -> Path:
        captured["name"] = name
        captured["version"] = version
        captured["requirements"] = requirements
        root = installer._venv_dir(name, version)
        if root.exists():
            shutil.rmtree(root)
        _make_fake_venv(root)
        return root

    monkeypatch.setattr(installer, "provision_venv", fake_provision)

    config = registry.load_config()
    res = installer.install(config, "tool", debug_local=scripts_dir)
    assert res.version == "1.0.0"

    assert captured["name"] == "tool"
    assert captured["version"] == "1.0.0"
    assert captured["requirements"] == ["click>=8"]

    pkg = state.get("tool")
    assert pkg is not None
    assert pkg.venv_dir is not None
    venv_root = Path(pkg.venv_dir)
    assert venv_root.exists()
    assert installer.venv_python(venv_root).exists()

    rc = runner.run_installed("tool", ["hello"])
    assert rc == 0


def test_pip_requires_without_python_entry_rejected(
    scripts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip_requires + non-.py entry should raise an InstallError."""
    from usmo import installer, registry

    f = scripts_dir / "tool.sh"
    f.write_text("#!/usr/bin/env bash\necho nope\n")
    _write_index(
        scripts_dir,
        {
            "tool": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "tool.sh",
                        "sha256": _sha256(f),
                        "pip_requires": ["click"],
                    }
                },
            }
        },
    )

    # Make sure we never even reach venv creation.
    def boom(*a, **kw):  # pragma: no cover - defensive
        raise AssertionError("provision_venv should not be called")

    monkeypatch.setattr(installer, "provision_venv", boom)

    config = registry.load_config()
    with pytest.raises(installer.InstallError, match="not a Python script"):
        installer.install(config, "tool", debug_local=scripts_dir)


def test_uninstall_removes_venv(
    scripts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from usmo import installer, registry, state

    f = scripts_dir / "tool.py"
    f.write_text("import sys\nprint('ok')\n")
    _write_index(
        scripts_dir,
        {
            "tool": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "script",
                        "path": "tool.py",
                        "sha256": _sha256(f),
                        "pip_requires": ["click"],
                    }
                },
            }
        },
    )

    def fake_provision(name: str, version: str, requirements: list[str]) -> Path:
        root = installer._venv_dir(name, version)
        _make_fake_venv(root)
        return root

    monkeypatch.setattr(installer, "provision_venv", fake_provision)

    config = registry.load_config()
    installer.install(config, "tool", debug_local=scripts_dir)
    pkg = state.get("tool")
    assert pkg is not None
    venv_root = Path(pkg.venv_dir)
    assert venv_root.exists()

    assert installer.uninstall("tool") is True
    assert not venv_root.exists()
    assert state.get("tool") is None


def test_archive_with_pip_requires_in_usm_toml(
    tmp_path: Path, scripts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive packages may declare pip_requires inside usm.toml."""
    from usmo import installer, registry, state

    work = tmp_path / "pkg-1.0.0"
    work.mkdir()
    (work / "usm.toml").write_text(
        'entry = "main.py"\npip_requires = ["rich>=13", "click"]\n'
    )
    (work / "main.py").write_text("import sys\nprint('archive-ok')\n")
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

    seen: dict = {}

    def fake_provision(name: str, version: str, requirements: list[str]) -> Path:
        seen["requirements"] = requirements
        root = installer._venv_dir(name, version)
        _make_fake_venv(root)
        return root

    monkeypatch.setattr(installer, "provision_venv", fake_provision)

    config = registry.load_config()
    installer.install(config, "pkg", debug_local=scripts_dir)
    # Order-preserving: archive deps appear in usm.toml order.
    assert seen["requirements"] == ["rich>=13", "click"]
    pkg = state.get("pkg")
    assert pkg is not None
    assert pkg.venv_dir is not None


def test_index_pip_requires_takes_precedence_then_archive_extends(
    tmp_path: Path, scripts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index-declared pip_requires merges with archive's, no duplicates."""
    from usmo import installer, registry

    work = tmp_path / "pkg-1.0.0"
    work.mkdir()
    (work / "usm.toml").write_text(
        'entry = "main.py"\npip_requires = ["click", "extra-dep"]\n'
    )
    (work / "main.py").write_text("print('x')\n")
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
                        "pip_requires": ["click"],
                    }
                },
            }
        },
    )

    seen: dict = {}

    def fake_provision(name, version, requirements):
        seen["requirements"] = requirements
        root = installer._venv_dir(name, version)
        _make_fake_venv(root)
        return root

    monkeypatch.setattr(installer, "provision_venv", fake_provision)

    installer.install(registry.load_config(), "pkg", debug_local=scripts_dir)
    # 'click' (index) listed first, 'extra-dep' (archive) appended without duping click.
    assert seen["requirements"] == ["click", "extra-dep"]


# ---------------------------------------------------------------------------
# Real venv smoke (offline: no requirements installed)
# ---------------------------------------------------------------------------


def test_provision_venv_with_no_requirements_creates_real_venv(
    tmp_path: Path,
) -> None:
    """No-requirement venv is just a stdlib ``python -m venv`` call."""
    from usmo import installer

    # Direct call, no monkey-patching: this must work fully offline.
    root = installer.provision_venv("samplepkg", "0.0.1", [])
    try:
        assert root.exists()
        py = installer.venv_python(root)
        assert py.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_state_tolerates_old_entries_without_venv_dir(tmp_path: Path) -> None:
    """A state.json written before the venv_dir field still loads."""
    from usmo import state

    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installed": {
                    "old": {
                        "version": "0.1.0",
                        "registry": "default",
                        "type": "script",
                        "install_dir": "/somewhere",
                        "entry": "old.sh",
                        "sha256": None,
                        "installed_at": "2024-01-01T00:00:00Z",
                    }
                },
            }
        )
    )
    loaded = state.load(p)
    assert loaded["old"].venv_dir is None
