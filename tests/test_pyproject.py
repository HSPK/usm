"""Tests for archive packages that ship a ``pyproject.toml``.

We exercise the real ``pip install <archive_root>`` flow at least once
with a tiny self-contained project (no third-party deps) so the pipx-style
behaviour is verified end-to-end. For variants that would need network
access we monkey-patch ``provision_pyproject_venv`` and only assert the
plumbing (state, entry resolution, runner dispatch).
"""

from __future__ import annotations

import hashlib
import json
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


def _make_fake_venv_with_console_script(root: Path, script_name: str) -> None:
    """Create a fake venv directory layout with a console-script wrapper."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "python"
    py.write_text(f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n')
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cs = bin_dir / script_name
    cs.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "console-script:$0"
            echo "args=$*"
            """
        )
    )
    cs.chmod(cs.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def scripts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scripts"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# End-to-end with a real (dependency-free) pyproject project.
# ---------------------------------------------------------------------------


def _build_pyproject_archive(
    tmp_path: Path,
    out: Path,
    *,
    pkg_name: str = "ngreet",
    console_script: str = "ngreet",
    extra_deps: list[str] | None = None,
    extra_usm_toml: str = "",
) -> None:
    """Build a tar.gz archive with a minimal pyproject.toml-based project."""
    work = tmp_path / f"{pkg_name}-1.0.0"
    work.mkdir(parents=True, exist_ok=True)
    deps = json.dumps(extra_deps or [])
    pyproject = textwrap.dedent(
        f"""\
        [build-system]
        requires = ["setuptools>=61"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "{pkg_name}"
        version = "1.0.0"
        description = "tiny test pkg"
        requires-python = ">=3.10"
        dependencies = {deps}

        [project.scripts]
        {console_script} = "{pkg_name}.cli:main"

        [tool.setuptools.packages.find]
        where = ["src"]
        """
    )
    (work / "pyproject.toml").write_text(pyproject)

    src_pkg = work / "src" / pkg_name
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text("")
    (src_pkg / "cli.py").write_text(
        textwrap.dedent(
            """\
            import sys
            def main():
                print("hello from " + __name__)
                print("argv=" + ",".join(sys.argv[1:]))
                return 0
            """
        )
    )

    usm_toml = f'console_script = "{console_script}"\n' + extra_usm_toml
    (work / "usm.toml").write_text(usm_toml)

    with tarfile.open(out, "w:gz") as tar:
        tar.add(work, arcname=f"{pkg_name}-1.0.0")


def test_pyproject_archive_install_run_uninstall(
    tmp_path: Path, scripts_dir: Path
) -> None:
    """Real flow: build a project archive, install it, run its console-script."""
    from usmo import installer, registry, runner, state

    archive = scripts_dir / "ngreet-1.0.0.tar.gz"
    _build_pyproject_archive(tmp_path, archive)

    _write_index(
        scripts_dir,
        {
            "ngreet": {
                "description": "tiny test pkg",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": archive.name,
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )

    config = registry.load_config()
    res = installer.install(config, "ngreet", debug_local=scripts_dir)
    assert res.version == "1.0.0"

    pkg = state.get("ngreet")
    assert pkg is not None
    assert pkg.console_script == "ngreet"
    assert pkg.venv_dir is not None
    venv_root = Path(pkg.venv_dir)
    assert venv_root.exists()
    cs_path = installer.venv_console_script(venv_root, "ngreet")
    assert cs_path.exists(), f"console-script not installed at {cs_path}"

    # Run via the runner: the venv's ngreet executable should produce output.
    rc = runner.run_installed("ngreet", ["alice"])
    assert rc == 0

    assert installer.uninstall("ngreet") is True
    assert not venv_root.exists()
    assert state.get("ngreet") is None


# ---------------------------------------------------------------------------
# Plumbing-only tests with monkey-patched venv provisioning.
# ---------------------------------------------------------------------------


def test_console_script_without_pyproject_rejected(
    tmp_path: Path, scripts_dir: Path
) -> None:
    """Declaring console_script without pyproject.toml should fail."""
    from usmo import installer, registry

    work = tmp_path / "bad-1.0.0"
    work.mkdir()
    (work / "usm.toml").write_text('console_script = "bogus"\n')
    (work / "main.py").write_text("print('x')\n")
    archive = scripts_dir / "bad-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname="bad-1.0.0")

    _write_index(
        scripts_dir,
        {
            "bad": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": archive.name,
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )
    config = registry.load_config()
    with pytest.raises(installer.InstallError, match="no pyproject.toml"):
        installer.install(config, "bad", debug_local=scripts_dir)


def test_pyproject_archive_with_explicit_entry_file(
    tmp_path: Path,
    scripts_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archive with pyproject.toml may still pick a file ``entry``."""
    from usmo import installer, registry, state

    work = tmp_path / "filepkg-1.0.0"
    work.mkdir()
    (work / "pyproject.toml").write_text(
        '[project]\nname = "filepkg"\nversion = "1.0.0"\ndependencies = []\n'
    )
    (work / "usm.toml").write_text('entry = "main.py"\n')
    (work / "main.py").write_text("print('file-entry')\n")
    archive = scripts_dir / "filepkg-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname="filepkg-1.0.0")

    _write_index(
        scripts_dir,
        {
            "filepkg": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": archive.name,
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )

    seen: dict = {}

    def fake_provision(name: str, version: str, project_root: Path, **kw) -> Path:
        seen["project_root"] = project_root
        root = installer._venv_dir(name, version)
        if root.exists():
            shutil.rmtree(root)
        _make_fake_venv_with_console_script(root, "anything")
        return root

    monkeypatch.setattr(installer, "provision_pyproject_venv", fake_provision)

    config = registry.load_config()
    res = installer.install(config, "filepkg", debug_local=scripts_dir)
    assert (seen["project_root"] / "pyproject.toml").exists()
    pkg = state.get("filepkg")
    assert pkg is not None
    assert pkg.console_script is None
    assert pkg.entry == "filepkg-1.0.0/main.py"
    assert res.entry == Path(pkg.install_dir) / pkg.entry


def test_console_script_dispatch_via_runner(
    tmp_path: Path, scripts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner should invoke the venv's console-script directly."""
    from usmo import installer, registry, runner

    work = tmp_path / "csdemo-1.0.0"
    work.mkdir()
    (work / "pyproject.toml").write_text(
        '[project]\nname = "csdemo"\nversion = "1.0.0"\ndependencies = []\n'
    )
    (work / "usm.toml").write_text('console_script = "csdemo"\n')
    archive = scripts_dir / "csdemo-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname="csdemo-1.0.0")

    _write_index(
        scripts_dir,
        {
            "csdemo": {
                "description": "",
                "latest": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "type": "archive",
                        "path": archive.name,
                        "sha256": _sha256(archive),
                    }
                },
            }
        },
    )

    def fake_provision(name: str, version: str, project_root: Path, **kw) -> Path:
        root = installer._venv_dir(name, version)
        if root.exists():
            shutil.rmtree(root)
        _make_fake_venv_with_console_script(root, "csdemo")
        return root

    monkeypatch.setattr(installer, "provision_pyproject_venv", fake_provision)

    installer.install(registry.load_config(), "csdemo", debug_local=scripts_dir)
    assert runner.run_installed("csdemo", ["one", "two"]) == 0


def test_state_tolerates_old_entries_without_console_script(tmp_path: Path) -> None:
    """A state.json from before the console_script field still loads cleanly."""
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
                        "type": "archive",
                        "install_dir": "/somewhere",
                        "entry": "main.py",
                        "sha256": None,
                        "installed_at": "2024-01-01T00:00:00Z",
                        "venv_dir": "/elsewhere",
                    }
                },
            }
        )
    )
    loaded = state.load(p)
    assert loaded["old"].console_script is None
    assert loaded["old"].venv_dir == "/elsewhere"
