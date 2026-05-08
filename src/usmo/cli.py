"""Top-level command-line interface for ``usm``.

The CLI is organised as a ``click.Group`` with a custom resolver:

* ``usm <subcommand> ...`` — built-in subcommands (``install``,
  ``uninstall``, ``upgrade``, ``installed``, ``available``, ``info``,
  ``search``, ``run``, ``registry``, ``publish``, ``list``, ``update``,
  ``clean``, ``version``).

* ``usm <package> [args...]`` — legacy / convenience form. If the
  argument is not a known subcommand it is treated as a package name:
  the package is installed on demand (latest version) and its entry
  script is executed with the remaining arguments.

This keeps every previous invocation (``usm init``, ``usm cu122``,
``usm check_py``…) working exactly as before, while exposing the new
versioned package model on top.
"""

from __future__ import annotations

import shutil
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Iterable

import click
import rich

from . import installer as installer_mod
from . import registry as registry_mod
from . import state as state_mod
from .manifest import Index, Package
from .runner import run_installed

# Reserved subcommand names that should never be interpreted as package names.
RESERVED = {
    "install",
    "uninstall",
    "upgrade",
    "installed",
    "available",
    "info",
    "search",
    "run",
    "registry",
    "publish",
    "list",
    "update",
    "clean",
    "version",
    "help",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _debug_local_path(debug: bool) -> Path | None:
    return (Path.cwd() / "scripts") if debug else None


def _load_config() -> registry_mod.RegistryConfig:
    return registry_mod.load_config()


def _iter_indices(
    config: registry_mod.RegistryConfig,
    *,
    refresh: bool = False,
    debug_local: Path | None = None,
) -> Iterable[tuple[registry_mod.Registry, Index]]:
    for reg in config.registries:
        try:
            idx = registry_mod.fetch_index(
                reg, refresh=refresh, debug_local=debug_local
            )
        except Exception as exc:
            rich.print(
                f"[yellow]warning:[/yellow] failed to load registry '{reg.id}': {exc}"
            )
            continue
        yield reg, idx


# ---------------------------------------------------------------------------
# Custom group with legacy dispatch
# ---------------------------------------------------------------------------


class UsmGroup(click.Group):
    """Click group that falls back to ``usm run`` for unknown commands."""

    def get_command(self, ctx, cmd_name):  # type: ignore[override]
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        if cmd_name in RESERVED:
            return None

        # Treat as a package name and dispatch to ``run`` with the name
        # injected as the first positional argument.
        @click.command(
            name=cmd_name,
            context_settings=dict(
                ignore_unknown_options=True,
                allow_extra_args=True,
                help_option_names=[],
            ),
        )
        @click.argument("args", nargs=-1, type=click.UNPROCESSED)
        @click.pass_context
        def _legacy(ctx_inner: click.Context, args: tuple[str, ...]) -> None:
            _legacy_dispatch(ctx_inner, cmd_name, list(args))

        return _legacy


def _legacy_dispatch(ctx: click.Context, name: str, args: list[str]) -> None:
    """Auto-install (if needed) and run a package by name."""
    parent = ctx.find_root()
    debug = bool(parent.params.get("debug"))
    upgrade = bool(parent.params.get("upgrade"))
    config = _load_config()
    debug_local = _debug_local_path(debug)

    pkg = state_mod.get(name)
    needs_install = pkg is None or upgrade or debug
    if needs_install:
        try:
            installer_mod.install(
                config,
                name,
                preferred_registry=None,
                refresh=upgrade,
                force=upgrade or debug,
                debug_local=debug_local,
            )
        except installer_mod.InstallError as exc:
            raise click.ClickException(str(exc)) from exc

    rc = run_installed(name, args)
    if rc != 0:
        ctx.exit(rc)


# ---------------------------------------------------------------------------
# Group definition + global options
# ---------------------------------------------------------------------------


@click.group(
    cls=UsmGroup,
    invoke_without_command=True,
    context_settings=dict(help_option_names=["-h", "--help"]),
)
@click.option(
    "--upgrade",
    "-U",
    is_flag=True,
    help="Force re-fetch of the registry index (and re-install) for legacy dispatch.",
)
@click.option(
    "--debug", is_flag=True, help="Use the local ./scripts directory as the registry."
)
@click.pass_context
def cli(ctx: click.Context, upgrade: bool, debug: bool) -> None:  # noqa: ARG001
    """usm — GitHub-backed package distribution for utility scripts."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd)


# ---------------------------------------------------------------------------
# Built-in subcommands
# ---------------------------------------------------------------------------


@cli.command("version")
def version_cmd() -> None:
    """Show the installed usm version."""
    try:
        from usmo._version import __version__ as ver
    except ImportError:
        try:
            ver = pkg_version("usmo")
        except Exception:
            ver = "unknown (editable install without build)"
    rich.print(f"[bold]usm[/bold] version {ver}")


@cli.command("clean")
def clean_cmd() -> None:
    """Remove all cached registry indices and installed packages."""
    removed = []
    for path in (registry_mod.INDEX_CACHE_DIR, registry_mod.PACKAGES_DIR):
        if path.exists():
            shutil.rmtree(path)
            removed.append(path)
    if state_mod.STATE_PATH.exists():
        state_mod.STATE_PATH.unlink()
        removed.append(state_mod.STATE_PATH)
    if removed:
        for p in removed:
            rich.print(f"[bold green]Removed:[/bold green] {p}")
    else:
        rich.print("[dim]Nothing to clean.[/dim]")


@cli.command("update")
@click.pass_context
def update_cmd(ctx: click.Context) -> None:
    """Refresh registry indices and reinstall already-installed packages."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    for reg, _ in _iter_indices(config, refresh=True, debug_local=debug_local):
        rich.print(f"[bold green]✓[/bold green] index refreshed: {reg.id}")
    installed = state_mod.load()
    if not installed:
        rich.print("[dim]No installed packages to refresh.[/dim]")
        return
    for name in sorted(installed):
        try:
            installer_mod.upgrade(config, name, debug_local=debug_local)
        except installer_mod.InstallError as exc:
            rich.print(f"[yellow]skip {name}:[/yellow] {exc}")


@cli.command("list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List packages available in the configured registries."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    installed = state_mod.load()

    rich.print("[bold]Available packages:[/bold]\n")
    seen: set[str] = set()
    for reg, index in _iter_indices(config, debug_local=debug_local):
        rich.print(f"[bold underline]Registry: {reg.id}[/bold underline]")
        for name, pkg in sorted(index.packages.items()):
            seen.add(name)
            inst = installed.get(name)
            status: str
            if inst is None:
                status = "[dim]not installed[/dim]"
            elif inst.version == pkg.latest:
                status = f"[green]installed {inst.version}[/green]"
            else:
                status = (
                    f"[yellow]installed {inst.version} (latest {pkg.latest})[/yellow]"
                )
            rich.print(
                f"  [bold]{name:18s}[/bold] {pkg.latest:10s} "
                f"{pkg.description:50s} {status}"
            )
        rich.print("")

    rich.print("[bold underline]Built-in commands:[/bold underline]")
    for cmd in sorted(RESERVED):
        rich.print(f"  [bold]{cmd}[/bold]")


@cli.command("available")
@click.argument("name")
@click.pass_context
def available_cmd(ctx: click.Context, name: str) -> None:
    """Show all available versions of a package."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    for reg, index in _iter_indices(config, debug_local=debug_local):
        if name not in index:
            continue
        pkg = index[name]
        rich.print(
            f"[bold]{name}[/bold] in [italic]{reg.id}[/italic]: {pkg.description}"
        )
        for v in pkg.sorted_versions():
            tag = " (latest)" if v.version == pkg.latest else ""
            sha = (v.sha256[:12] + "…") if v.sha256 else "no sha256"
            rich.print(f"  - {v.version:10s} [{v.type}] {sha}{tag}")
        return
    raise click.ClickException(f"Package '{name}' not found in any registry")


@cli.command("info")
@click.argument("name")
@click.pass_context
def info_cmd(ctx: click.Context, name: str) -> None:
    """Show detailed info about a package."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    inst = state_mod.get(name)
    for reg, index in _iter_indices(config, debug_local=debug_local):
        if name not in index:
            continue
        pkg: Package = index[name]
        latest = pkg.get(None)
        rich.print(f"[bold]{name}[/bold]")
        rich.print(f"  description : {pkg.description}")
        rich.print(f"  registry    : {reg.id} ({reg.base_url})")
        rich.print(f"  latest      : {pkg.latest}")
        rich.print(f"  type        : {latest.type}")
        rich.print(f"  path        : {latest.path}")
        if latest.sha256:
            rich.print(f"  sha256      : {latest.sha256}")
        if latest.depends:
            rich.print(f"  depends     : {', '.join(latest.depends)}")
        if latest.pip_requires:
            rich.print(f"  pip_requires: {', '.join(latest.pip_requires)}")
        if inst is not None:
            rich.print(f"  installed   : {inst.version} (in {inst.install_dir})")
            if inst.venv_dir:
                rich.print(f"  venv        : {inst.venv_dir}")
        else:
            rich.print("  installed   : [dim]no[/dim]")
        return
    raise click.ClickException(f"Package '{name}' not found in any registry")


@cli.command("search")
@click.argument("query")
@click.pass_context
def search_cmd(ctx: click.Context, query: str) -> None:
    """Search packages by name or description across all registries."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    q = query.lower()
    found = False
    for reg, index in _iter_indices(config, debug_local=debug_local):
        for name, pkg in sorted(index.packages.items()):
            if q in name.lower() or q in pkg.description.lower():
                found = True
                rich.print(
                    f"  [bold]{name:18s}[/bold] {pkg.latest:10s} "
                    f"[italic]{reg.id}[/italic] {pkg.description}"
                )
    if not found:
        rich.print(f"[dim]No matches for '{query}'.[/dim]")


@cli.command("installed")
def installed_cmd() -> None:
    """List packages installed on this machine."""
    installed = state_mod.load()
    if not installed:
        rich.print("[dim]No packages installed.[/dim]")
        return
    for name in sorted(installed):
        p = installed[name]
        rich.print(
            f"  [bold]{name:18s}[/bold] {p.version:10s} "
            f"[italic]{p.registry}[/italic] -> {p.install_dir}"
        )


@cli.command("install")
@click.argument("spec")
@click.option(
    "--registry",
    "registry_id",
    default=None,
    help="Prefer a specific registry id when resolving the package.",
)
@click.option("--force", is_flag=True, help="Reinstall even if already present.")
@click.option("--refresh", is_flag=True, help="Refresh the registry index first.")
@click.pass_context
def install_cmd(
    ctx: click.Context,
    spec: str,
    registry_id: str | None,
    force: bool,
    refresh: bool,
) -> None:
    """Install a package. Use NAME or NAME==VERSION."""
    name, _, version = spec.partition("==")
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    try:
        result = installer_mod.install(
            config,
            name,
            version=version or None,
            preferred_registry=registry_id,
            refresh=refresh,
            force=force,
            debug_local=debug_local,
        )
    except installer_mod.InstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.upgraded_from:
        rich.print(
            f"[bold green]Upgraded[/bold green] {name}: "
            f"{result.upgraded_from} -> {result.version}"
        )
    else:
        rich.print(
            f"[bold green]Installed[/bold green] {name}=={result.version} "
            f"from {result.registry}"
        )


@cli.command("uninstall")
@click.argument("name")
def uninstall_cmd(name: str) -> None:
    """Uninstall a previously installed package."""
    if installer_mod.uninstall(name):
        rich.print(f"[bold green]Removed[/bold green] {name}")
    else:
        rich.print(f"[dim]{name} is not installed[/dim]")


@cli.command("upgrade")
@click.argument("name", required=False)
@click.pass_context
def upgrade_cmd(ctx: click.Context, name: str | None) -> None:
    """Upgrade a package (or all installed packages) to the latest version."""
    config = _load_config()
    debug_local = _debug_local_path(bool(ctx.find_root().params.get("debug")))
    targets = [name] if name else sorted(state_mod.load().keys())
    if not targets:
        rich.print("[dim]No packages installed.[/dim]")
        return
    for n in targets:
        try:
            installer_mod.upgrade(config, n, debug_local=debug_local)
        except installer_mod.InstallError as exc:
            rich.print(f"[yellow]skip {n}:[/yellow] {exc}")


@cli.command("run")
@click.argument("name")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_cmd(ctx: click.Context, name: str, args: tuple[str, ...]) -> None:
    """Run an installed package's entry script."""
    rc = run_installed(name, list(args))
    if rc != 0:
        ctx.exit(rc)


# ---------------------------------------------------------------------------
# Registry management
# ---------------------------------------------------------------------------


@cli.group("registry")
def registry_grp() -> None:
    """Manage the list of registries usm searches."""


@registry_grp.command("list")
def registry_list_cmd() -> None:
    """Show the configured registries."""
    config = _load_config()
    rich.print(f"[bold]Default registry:[/bold] {config.default_registry}")
    for r in config.registries:
        marker = "*" if r.id == config.default_registry else " "
        rich.print(f"  {marker} {r.id:12s} {r.url}")


@registry_grp.command("add")
@click.argument("identifier")
@click.argument("url")
@click.option("--default", is_flag=True, help="Make this the default registry.")
def registry_add_cmd(identifier: str, url: str, default: bool) -> None:
    """Add a new registry."""
    config = _load_config()
    if any(r.id == identifier for r in config.registries):
        raise click.ClickException(f"Registry '{identifier}' already exists")
    config.registries.append(registry_mod.Registry(id=identifier, url=url))
    if default or len(config.registries) == 1:
        config.default_registry = identifier
    registry_mod.save_config(config)
    rich.print(f"[bold green]Added[/bold green] registry '{identifier}' -> {url}")


@registry_grp.command("remove")
@click.argument("identifier")
def registry_remove_cmd(identifier: str) -> None:
    """Remove a registry."""
    config = _load_config()
    if not any(r.id == identifier for r in config.registries):
        raise click.ClickException(f"Registry '{identifier}' not configured")
    config.registries = [r for r in config.registries if r.id != identifier]
    if not config.registries:
        # Always keep at least the built-in default available.
        config.registries.append(
            registry_mod.Registry(
                id=registry_mod.DEFAULT_REGISTRY_ID,
                url=registry_mod.DEFAULT_REGISTRY_URL,
            )
        )
    if config.default_registry == identifier:
        config.default_registry = config.registries[0].id
    registry_mod.save_config(config)
    rich.print(f"[bold green]Removed[/bold green] registry '{identifier}'")


@registry_grp.command("default")
@click.argument("identifier")
def registry_default_cmd(identifier: str) -> None:
    """Set the default registry."""
    config = _load_config()
    if not any(r.id == identifier for r in config.registries):
        raise click.ClickException(f"Registry '{identifier}' not configured")
    config.default_registry = identifier
    registry_mod.save_config(config)
    rich.print(f"[bold green]Default registry set to[/bold green] {identifier}")


# ---------------------------------------------------------------------------
# Publish helper
# ---------------------------------------------------------------------------


@cli.command("publish")
@click.argument(
    "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--name", required=True, help="Package name")
@click.option("--version", required=True, help="Package version (PEP 440)")
@click.option("--description", default="", help="Short description")
@click.option(
    "--type",
    "pkg_type",
    type=click.Choice(["script", "archive"]),
    default="script",
    show_default=True,
)
@click.option(
    "--pip-require",
    "pip_requires",
    multiple=True,
    help=(
        "PEP 508 requirement (repeat for multiple) to install in a "
        "per-package virtualenv. Implies the entry must be a .py script."
    ),
)
def publish_cmd(
    file_path: Path,
    name: str,
    version: str,
    description: str,
    pkg_type: str,
    pip_requires: tuple[str, ...],
) -> None:
    """Print a v2 index snippet for FILE_PATH suitable for committing to a registry repo."""
    sha = installer_mod.sha256_of(file_path)
    size = file_path.stat().st_size
    version_entry: dict = {
        "type": pkg_type,
        "path": file_path.name,
        "sha256": sha,
        "size": size,
    }
    if pip_requires:
        version_entry["pip_requires"] = list(pip_requires)
    snippet = {
        "schema_version": 2,
        "packages": {
            name: {
                "description": description,
                "latest": version,
                "versions": {version: version_entry},
            }
        },
    }
    import json

    rich.print(json.dumps(snippet, indent=2))


if __name__ == "__main__":
    cli()
