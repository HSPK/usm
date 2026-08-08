"""Built-in commands and the dispatch registry.

Each built-in is a real :class:`click.Command`, which is what makes
``usm <builtin> --help`` work and what stops a stray ``-h`` from being
silently swallowed (``usm clean -h`` used to wipe the cache). The top-level
entry point hands the remaining argv straight to the matching command.
"""

from __future__ import annotations

import click

from usmo import ui

from .output import on_download

CONTEXT = {"help_option_names": ["-h", "--help"]}


def _core():
    """Import the SDK lazily so the landing page never pays for it."""
    from usmo import core

    return core


def _presenters():
    from . import presenters

    return presenters


def load_scripts(*, debug: bool = False, upgrade: bool = False):
    """Load the catalog, translating download failures into CLI errors."""
    core = _core()
    try:
        return core.load_scripts(
            debug=debug, force_download=upgrade, on_progress=on_download
        )
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc


def _flags(ctx: click.Context) -> dict:
    return ctx.obj or {"debug": False, "upgrade": False}


@click.command("list", context_settings=CONTEXT, short_help="List available commands.")
@click.argument("pattern", required=False)
@click.option("--cached", is_flag=True, help="Only commands already downloaded.")
@click.option("--missing", is_flag=True, help="Only commands not yet downloaded.")
@click.option("--names", is_flag=True, help="Print bare names, one per line.")
@click.pass_context
def cmd_list(ctx, pattern, cached, missing, names):
    """List available commands.

    PATTERN, when given, keeps only commands whose name or description
    contains it.
    """
    core, presenters = _core(), _presenters()
    flags = _flags(ctx)
    scripts = load_scripts(**flags)
    if pattern:
        scripts = core.match_scripts(scripts, pattern)
    if cached:
        scripts = {n: s for n, s in scripts.items() if s.cached_path.exists()}
    if missing:
        scripts = {n: s for n, s in scripts.items() if not s.cached_path.exists()}

    if names:
        for name in sorted(scripts):
            click.echo(name)
        return
    if not scripts:
        presenters.print_no_matches(pattern or "that filter", load_scripts(**flags))
        raise SystemExit(1)
    title = f"Commands matching '{pattern}'" if pattern else "Commands"
    presenters.print_scripts(scripts, title=title, highlight=pattern)
    if not pattern and not cached and not missing:
        presenters.print_builtins()


@click.command(
    "search", context_settings=CONTEXT, short_help="Find commands by name or text."
)
@click.argument("query")
@click.option("--names", is_flag=True, help="Print bare names, one per line.")
@click.pass_context
def cmd_search(ctx, query, names):
    """Search command names and descriptions for QUERY."""
    core, presenters = _core(), _presenters()
    all_scripts = load_scripts(**_flags(ctx))
    hits = core.match_scripts(all_scripts, query)
    if names:
        for name in sorted(hits):
            click.echo(name)
        return
    if not hits:
        presenters.print_no_matches(query, all_scripts)
        raise SystemExit(1)
    presenters.print_scripts(
        hits, title=f"{len(hits)} match(es) for '{query}'", highlight=query
    )


@click.command("update", context_settings=CONTEXT, short_help="Refresh the catalog.")
@click.argument("names", nargs=-1)
@click.option(
    "-a", "--all", "all_scripts", is_flag=True, help="Also pull every cached script."
)
@click.pass_context
def cmd_update(ctx, names, all_scripts):
    """Refresh the catalog, and optionally re-download scripts.

    With NAMES, those scripts are pulled even if they were not cached.
    """
    core, presenters = _core(), _presenters()
    had_cache = core.has_cached_config()
    try:
        changes = core.update_config(on_progress=on_download)
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc

    if names:
        try:
            list(
                core.iter_updates(
                    names=names, refresh_config=False, on_progress=on_download
                )
            )
        except core.UnknownCommand as exc:
            ui.fail(f"Unknown command '{exc.name}'.")
            ui.hint(f"Available: {', '.join(exc.available)}")
            raise click.ClickException(str(exc)) from exc
        except core.DownloadError as exc:
            raise click.ClickException(str(exc)) from exc
        presenters.print_named_update(tuple(names), changes)
        return

    presenters.print_catalog_changes(changes, cold=not had_cache)
    if not all_scripts:
        if changes and had_cache:
            ui.hint("Run usm update --all to pull the new scripts.")
        return

    try:
        results = list(
            core.iter_updates(names=None, refresh_config=False, on_progress=on_download)
        )
    except core.DownloadError as exc:
        raise click.ClickException(str(exc)) from exc
    pulled = [n for n, updated in results if updated]
    if pulled:
        ui.ok(f"Pulled {ui.plural(len(pulled), 'cached script')}.")
    else:
        ui.hint("No cached scripts to pull.")


@click.command(
    "install", context_settings=CONTEXT, short_help="Install a script as an alias."
)
@click.argument("script")
@click.argument("alias")
@click.pass_context
def cmd_install(ctx, script, alias):
    """Install SCRIPT as ALIAS in ~/.local/bin."""
    import shutil
    import sys

    core, _ = _core(), None
    scripts = load_scripts(**_flags(ctx))
    if script not in scripts:
        ui.fail(f"Unknown script '{script}'.")
        close = core.closest_names(script, scripts)
        if close:
            ui.hint(f"Did you mean: {', '.join(close)}?")
        raise click.ClickException(f"Unknown script '{script}'.")

    path, status = core.alias_status(alias)
    if status == "foreign":
        ui.warn(f"{path} already exists and is not a usm alias.")
        if not click.confirm("Overwrite it?", default=False):
            raise click.ClickException("aborted.")

    usm_bin = shutil.which("usm") or sys.argv[0]
    core.install_alias(script, alias, usm_bin=usm_bin)
    verb = "Updated" if status == "ours" else "Installed"
    ui.ok(
        f"{verb} {ui.identifier(alias)} {ui.STEP} usm {script}  "
        f"{ui.muted(ui.shorten_path(path))}"
    )
    if not core.local_bin_in_path():
        ui.warn(
            f"{ui.shorten_path(core.LOCAL_BIN_DIR)} is not on your PATH. "
            "Add it so the alias is found:"
        )
        ui.hint(
            '  export PATH="$HOME/.local/bin:$PATH"   '
            "(append to ~/.bashrc or ~/.zshrc, then restart the shell)"
        )


@click.command(
    "uninstall", context_settings=CONTEXT, short_help="Remove an installed alias."
)
@click.argument("alias")
def cmd_uninstall(alias):
    """Remove the usm-managed alias ALIAS."""
    core = _core()
    try:
        removed = core.uninstall_alias(alias)
    except core.ForeignAlias as exc:
        raise click.ClickException(
            f"{exc.path} is not a usm-managed alias; not removing it."
        ) from exc
    if removed is None:
        ui.hint(f"No usm alias '{alias}' in {ui.shorten_path(core.LOCAL_BIN_DIR)}.")
    else:
        ui.ok(f"Removed {ui.identifier(alias)}  {ui.muted(ui.shorten_path(removed))}")


@click.command("clean", context_settings=CONTEXT, short_help="Remove the script cache.")
def cmd_clean():
    """Delete cached scripts and their virtualenvs."""
    removed = _core().clean_cache()
    if removed:
        ui.ok("Removed cached scripts and environments.")
    else:
        ui.hint("Nothing to clean - no cached scripts or environments.")


@click.command("version", context_settings=CONTEXT, short_help="Show the usm version.")
def cmd_version():
    """Print the installed usm version."""
    ui.print(f"usm version {_core().resolve_version()}")


COMMANDS: dict[str, click.Command] = {
    "list": cmd_list,
    "search": cmd_search,
    "update": cmd_update,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "clean": cmd_clean,
    "version": cmd_version,
}
