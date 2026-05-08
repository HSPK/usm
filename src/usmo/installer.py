"""Install, upgrade, and uninstall packages from a registry."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import venv
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import rich

from . import registry as registry_mod
from . import state as state_mod
from .manifest import Index, PackageVersion


class InstallError(Exception):
    """Raised when installation fails."""


@dataclass
class InstallResult:
    name: str
    version: str
    registry: str
    install_dir: Path
    entry: Path
    upgraded_from: str | None = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_package(
    config: registry_mod.RegistryConfig,
    name: str,
    *,
    preferred_registry: str | None = None,
    debug_local: Path | None = None,
    refresh: bool = False,
) -> tuple[registry_mod.Registry, Index]:
    """Find the first registry that contains ``name``.

    Returns the registry and its parsed index. Raises ``InstallError``
    if the package cannot be found anywhere.
    """
    last_err: Exception | None = None
    for reg in config.iter_search_order(preferred_registry):
        try:
            index = registry_mod.fetch_index(
                reg, refresh=refresh, debug_local=debug_local
            )
        except Exception as exc:  # network / parse errors
            last_err = exc
            rich.print(
                f"[yellow]warning:[/yellow] failed to fetch index for '{reg.id}': {exc}"
            )
            continue
        if name in index:
            return reg, index
    if last_err is not None:
        raise InstallError(
            f"Package '{name}' not found in any registry; last error: {last_err}"
        )
    raise InstallError(f"Package '{name}' not found in any registry")


# ---------------------------------------------------------------------------
# Hash verification
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path, expected: str | None, *, ctx: str) -> None:
    actual = sha256_of(path)
    if expected is None:
        rich.print(
            f"[yellow]warning:[/yellow] no sha256 in registry for {ctx}; "
            f"actual={actual}"
        )
        return
    if actual.lower() != expected.lower():
        raise InstallError(
            f"sha256 mismatch for {ctx}: expected {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


VENVS_DIR_NAME = "venvs"


def _package_dir(name: str, version: str) -> Path:
    return registry_mod.PACKAGES_DIR / name / version


def _venv_dir(name: str, version: str) -> Path:
    return registry_mod.CACHE_DIR / VENVS_DIR_NAME / name / version


def venv_python(venv_root: Path) -> Path:
    """Return the path to the Python interpreter inside ``venv_root``."""
    if os.name == "nt":  # pragma: no cover - Windows
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _create_venv(venv_root: Path) -> None:
    """Create (or recreate) a virtualenv with pip available."""
    if venv_root.exists():
        shutil.rmtree(venv_root)
    venv_root.parent.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(
        with_pip=True,
        clear=True,
        symlinks=(os.name != "nt"),
    )
    builder.create(str(venv_root))


def _pip_install(venv_root: Path, requirements: list[str]) -> None:
    """Install ``requirements`` into the venv at ``venv_root``."""
    if not requirements:
        return
    py = venv_python(venv_root)
    if not py.exists():
        raise InstallError(
            f"venv interpreter is missing at {py}; venv creation likely failed"
        )
    cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--quiet",
        *requirements,
    ]
    rich.print(
        f"[bold green]Installing[/bold green] dependencies into venv: "
        f"{', '.join(requirements)}"
    )
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise InstallError(
            f"pip install failed (exit {exc.returncode}) for: {requirements}"
        ) from exc
    except OSError as exc:
        raise InstallError(f"failed to invoke pip: {exc}") from exc


def provision_venv(name: str, version: str, requirements: list[str]) -> Path:
    """Create a fresh venv for ``name==version`` and install ``requirements``.

    Returns the path to the venv root directory. Exposed as a module-level
    function so tests can monkey-patch it without touching ``install``.
    """
    root = _venv_dir(name, version)
    _create_venv(root)
    _pip_install(root, requirements)
    return root


def provision_pyproject_venv(
    name: str, version: str, project_root: Path, *, extras: list[str] | None = None
) -> Path:
    """Create a venv and ``pip install`` the project at ``project_root``.

    This is the pipx-style flow: the project's own ``pyproject.toml``
    (PEP 621) declares ``[project].dependencies`` which pip resolves
    automatically. Console-scripts declared under ``[project.scripts]``
    become available in the venv's ``bin/`` directory.
    """
    if not (project_root / "pyproject.toml").exists():
        raise InstallError(
            f"{project_root} has no pyproject.toml; cannot install as a Python project"
        )
    root = _venv_dir(name, version)
    _create_venv(root)
    py = venv_python(root)
    spec = str(project_root)
    if extras:
        spec = f"{spec}[{','.join(extras)}]"
    cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--quiet",
        spec,
    ]
    rich.print(
        f"[bold green]Installing[/bold green] {name}=={version} "
        f"into venv from pyproject.toml"
    )
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise InstallError(
            f"pip install of {project_root} failed (exit {exc.returncode})"
        ) from exc
    except OSError as exc:
        raise InstallError(f"failed to invoke pip: {exc}") from exc
    return root


def venv_console_script(venv_root: Path, script: str) -> Path:
    """Return the path to a console-script ``script`` inside ``venv_root``."""
    if os.name == "nt":  # pragma: no cover - Windows
        return venv_root / "Scripts" / f"{script}.exe"
    return venv_root / "bin" / script


def _gc_old_venvs(name: str, keep_version: str) -> None:
    parent = _venv_dir(name, keep_version).parent
    if not parent.exists():
        return
    for sibling in parent.iterdir():
        if sibling.name != keep_version and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)


def _read_archive_manifest(archive_root: Path) -> dict:
    manifest_path = archive_root / "usm.toml"
    if not manifest_path.exists():
        raise InstallError(f"Archive is missing usm.toml at its root ({archive_root})")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract guarding against path traversal (CVE-2007-4559)."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError as exc:
            raise InstallError(f"Archive contains unsafe path '{member.name}'") from exc
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            try:
                link_target.relative_to(dest_resolved)
            except ValueError as exc:
                raise InstallError(
                    f"Archive contains unsafe link target '{member.linkname}'"
                ) from exc
    tar.extractall(dest, filter="data")  # noqa: S202 - guarded above


def install(
    config: registry_mod.RegistryConfig,
    name: str,
    *,
    version: str | None = None,
    preferred_registry: str | None = None,
    refresh: bool = False,
    force: bool = False,
    debug_local: Path | None = None,
) -> InstallResult:
    """Install ``name`` (optionally pinned to ``version``)."""
    reg, index = resolve_package(
        config,
        name,
        preferred_registry=preferred_registry,
        debug_local=debug_local,
        refresh=refresh,
    )
    pkg = index[name]
    pv: PackageVersion = pkg.get(version)

    existing = state_mod.get(name)
    if existing and existing.version == pv.version and not force:
        install_dir = Path(existing.install_dir)
        rich.print(
            f"[dim]{name}=={pv.version} already installed[/dim] (use --force to reinstall)"
        )
        return InstallResult(
            name=name,
            version=pv.version,
            registry=existing.registry,
            install_dir=install_dir,
            entry=install_dir / existing.entry,
            upgraded_from=None,
        )

    install_dir = _package_dir(name, pv.version)
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    if debug_local is not None:
        src = debug_local / pv.path
        if not src.exists():
            raise InstallError(f"Local file not found: {src}")
        downloaded = install_dir / Path(pv.path).name
        downloaded.write_bytes(src.read_bytes())
    else:
        downloaded = install_dir / Path(pv.path).name
        rich.print(
            f"[bold green]Downloading[/bold green] {name}=={pv.version} "
            f"from {reg.file_url(pv.path)}"
        )
        registry_mod.fetch_file(reg, pv.path, downloaded)

    _verify(downloaded, pv.sha256, ctx=f"{name}=={pv.version}")

    if pv.type == "script":
        entry_path = downloaded
        if not entry_path.suffix == ".json":
            entry_path.chmod(entry_path.stat().st_mode | 0o111)
        entry_name = entry_path.name
        archive_pip_requires: tuple[str, ...] = ()
        archive_root = None
        archive_console_script: str | None = None
        archive_has_pyproject = False
    elif pv.type == "archive":
        with tarfile.open(downloaded, "r:*") as tar:
            _safe_extract(tar, install_dir)
        downloaded.unlink()
        # Try to read usm.toml, either at root or in the single top-level dir.
        candidates = [install_dir, *(p for p in install_dir.iterdir() if p.is_dir())]
        archive_root = None
        for cand in candidates:
            if (cand / "usm.toml").exists():
                archive_root = cand
                break
        if archive_root is None:
            raise InstallError(
                f"Could not locate usm.toml in extracted archive for {name}=={pv.version}"
            )
        am = _read_archive_manifest(archive_root)
        archive_has_pyproject = (archive_root / "pyproject.toml").exists()
        archive_console_script = am.get("console_script")
        archive_pip_requires = tuple(am.get("pip_requires", ()) or ())

        # Resolve the entry. ``console_script`` only makes sense when the
        # archive ships a pyproject.toml (its entry-points come from there).
        if archive_console_script and not archive_has_pyproject:
            raise InstallError(
                f"{name}=={pv.version}: usm.toml declares "
                f"console_script='{archive_console_script}' but the archive "
                f"has no pyproject.toml"
            )

        if archive_console_script:
            entry_name = ""  # stored separately via console_script
            entry_path = archive_root  # placeholder; not used directly
        else:
            entry_rel = pv.entry or am.get("entry")
            if not entry_rel:
                raise InstallError(
                    f"Archive manifest for {name}=={pv.version} is missing "
                    f"'entry' (or 'console_script' for pyproject archives)"
                )
            entry_path = archive_root / entry_rel
            if not entry_path.exists():
                raise InstallError(
                    f"Declared entry '{entry_rel}' not found inside archive for {name}"
                )
            entry_path.chmod(entry_path.stat().st_mode | 0o111)
            # Store entry relative to install_dir so state.json stays portable-ish.
            entry_name = str(entry_path.relative_to(install_dir))
    else:  # pragma: no cover - guarded by manifest validation
        raise InstallError(f"Unknown package type: {pv.type}")

    # Merge pip requirements declared in the index with those declared in the
    # archive's usm.toml. Index entries win (more authoritative), archive ones
    # are appended for any extra deps the archive needs.
    requirements: list[str] = list(pv.pip_requires)
    for req in archive_pip_requires:
        if req not in requirements:
            requirements.append(req)

    venv_root: Path | None = None
    if archive_has_pyproject:
        # pipx-style: install the project itself into the venv. PEP 621
        # ``[project].dependencies`` are picked up automatically by pip.
        # ``pip_requires`` from the index/usm.toml are added on top, useful
        # for optional / out-of-band requirements not declared in pyproject.
        assert archive_root is not None
        venv_root = provision_pyproject_venv(name, pv.version, archive_root)
        if requirements:
            _pip_install(venv_root, requirements)
        if archive_console_script:
            cs_path = venv_console_script(venv_root, archive_console_script)
            if not cs_path.exists():
                raise InstallError(
                    f"{name}=={pv.version}: console_script "
                    f"'{archive_console_script}' was not installed by pip "
                    f"(check [project.scripts] in pyproject.toml)"
                )
    elif requirements:
        if entry_path.suffix != ".py":
            raise InstallError(
                f"{name}=={pv.version} declares pip_requires but entry "
                f"'{entry_name}' is not a Python script (.py)"
            )
        venv_root = provision_venv(name, pv.version, requirements)

    upgraded_from = existing.version if existing else None
    state_mod.record(
        name,
        version=pv.version,
        registry=reg.id,
        type=pv.type,
        install_dir=install_dir,
        entry=entry_name,
        sha256=pv.sha256,
        venv_dir=venv_root,
        console_script=archive_console_script,
    )

    # Best-effort GC of older versions of the same package.
    parent = install_dir.parent
    for sibling in parent.iterdir():
        if sibling != install_dir and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)
    _gc_old_venvs(name, pv.version)

    if archive_console_script and venv_root is not None:
        result_entry = venv_console_script(venv_root, archive_console_script)
    else:
        result_entry = install_dir / entry_name

    return InstallResult(
        name=name,
        version=pv.version,
        registry=reg.id,
        install_dir=install_dir,
        entry=result_entry,
        upgraded_from=upgraded_from,
    )


def uninstall(name: str) -> bool:
    """Remove ``name``. Returns True if anything was removed."""
    pkg = state_mod.get(name)
    if pkg is None:
        return False
    install_dir = Path(pkg.install_dir)
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
    # Also clean up the parent <name>/ if empty.
    parent = install_dir.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    if pkg.venv_dir:
        venv_path = Path(pkg.venv_dir)
        if venv_path.exists():
            shutil.rmtree(venv_path, ignore_errors=True)
        venv_parent = venv_path.parent
        if venv_parent.exists() and not any(venv_parent.iterdir()):
            venv_parent.rmdir()
    state_mod.remove(name)
    return True


def upgrade(
    config: registry_mod.RegistryConfig,
    name: str,
    *,
    debug_local: Path | None = None,
) -> InstallResult | None:
    """Upgrade ``name`` to the latest version, if a newer one exists."""
    pkg = state_mod.get(name)
    if pkg is None:
        raise InstallError(f"'{name}' is not installed")
    reg, index = resolve_package(
        config,
        name,
        preferred_registry=pkg.registry,
        debug_local=debug_local,
        refresh=True,
    )
    latest = index[name].get(None)
    if latest.version == pkg.version:
        rich.print(f"[dim]{name}=={pkg.version} is already up to date[/dim]")
        return None
    return install(
        config,
        name,
        version=latest.version,
        preferred_registry=reg.id,
        debug_local=debug_local,
        force=True,
    )
