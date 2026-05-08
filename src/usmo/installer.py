"""Install, upgrade, and uninstall packages from a registry."""

from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
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


def _package_dir(name: str, version: str) -> Path:
    return registry_mod.PACKAGES_DIR / name / version


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
    elif pv.type == "archive":
        with tarfile.open(downloaded, "r:*") as tar:
            _safe_extract(tar, install_dir)
        downloaded.unlink()
        # Try to read usm.toml, either at root or in the single top-level dir.
        candidates = [install_dir, *(p for p in install_dir.iterdir() if p.is_dir())]
        archive_root: Path | None = None
        for cand in candidates:
            if (cand / "usm.toml").exists():
                archive_root = cand
                break
        if archive_root is None:
            raise InstallError(
                f"Could not locate usm.toml in extracted archive for {name}=={pv.version}"
            )
        am = _read_archive_manifest(archive_root)
        entry_rel = pv.entry or am.get("entry")
        if not entry_rel:
            raise InstallError(
                f"Archive manifest for {name}=={pv.version} is missing 'entry'"
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

    upgraded_from = existing.version if existing else None
    state_mod.record(
        name,
        version=pv.version,
        registry=reg.id,
        type=pv.type,
        install_dir=install_dir,
        entry=entry_name,
        sha256=pv.sha256,
    )

    # Best-effort GC of older versions of the same package.
    parent = install_dir.parent
    for sibling in parent.iterdir():
        if sibling != install_dir and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)

    return InstallResult(
        name=name,
        version=pv.version,
        registry=reg.id,
        install_dir=install_dir,
        entry=install_dir / entry_name,
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
