from __future__ import annotations

import json
import shutil
import subprocess
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path

import click
import rich

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
CONFIG_FILENAME = "_config.json"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/scripts/"

# Built-in commands handled directly by the CLI (not dispatched as scripts).
BUILTIN_COMMANDS = {"list", "update", "clean", "version"}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _download_file(filename: str) -> Path:
    """Download a single file from the remote scripts directory."""
    import requests

    url = f"{RESOURCE_BASE_URL}{filename}"
    rich.print(f"[bold green]Downloading:[/bold green] {filename} from {url}")
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(
            f"Failed to download {filename}. Status code: {response.status_code}"
        )
    dest = CACHE_SCRIPT_DIR / filename
    CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    if not filename.endswith(".json"):
        dest.chmod(dest.stat().st_mode | 0o111)
    return dest


def load_config(
    *,
    debug: bool = False,
    force_download: bool = False,
) -> dict[str, dict]:
    """Return the *scripts* mapping from ``_config.json``."""
    if debug:
        config_path = Path.cwd() / "scripts" / CONFIG_FILENAME
    else:
        config_path = CACHE_SCRIPT_DIR / CONFIG_FILENAME
        if force_download or not config_path.exists():
            config_path = _download_file(CONFIG_FILENAME)

    data = json.loads(config_path.read_text())
    return data.get("scripts", {})


def may_download(
    scripts: dict[str, dict],
    script_name: str,
    *,
    force_download: bool = False,
) -> Path:
    script_filename = scripts[script_name]["path"]
    script_path = CACHE_SCRIPT_DIR / script_filename
    if force_download or not script_path.exists():
        return _download_file(script_filename)
    return script_path


# ---------------------------------------------------------------------------
# Built-in sub-commands
# ---------------------------------------------------------------------------

def _cmd_list(scripts: dict[str, dict]) -> None:
    """List all available commands."""
    rich.print("[bold]Available commands:[/bold]\n")
    rich.print("[bold underline]Scripts:[/bold underline]")
    for name, info in sorted(scripts.items()):
        cached = (CACHE_SCRIPT_DIR / info["path"]).exists()
        status = "[green]cached[/green]" if cached else "[dim]not cached[/dim]"
        rich.print(f"  [bold]{name:20s}[/bold] {info['description']:50s}  {status}")
    rich.print("\n[bold underline]Built-in:[/bold underline]")
    rich.print("  [bold]list[/bold]                 List all available commands.")
    rich.print("  [bold]update[/bold]               Re-download config and all cached scripts.")
    rich.print("  [bold]clean[/bold]                Remove the script cache directory.")
    rich.print("  [bold]version[/bold]              Show usm version.")


def _cmd_update(scripts: dict[str, dict]) -> None:
    """Re-download config and every script that is already cached."""
    _download_file(CONFIG_FILENAME)
    scripts = load_config(force_download=True)
    for name, info in scripts.items():
        cached = CACHE_SCRIPT_DIR / info["path"]
        if cached.exists():
            _download_file(info["path"])
            rich.print(f"  [green]✓[/green] {name}")
        else:
            rich.print(f"  [dim]–[/dim] {name} (not cached, skipped)")
    rich.print("[bold green]Update complete.[/bold green]")


def _cmd_clean() -> None:
    """Remove the entire script cache directory."""
    if CACHE_SCRIPT_DIR.exists():
        shutil.rmtree(CACHE_SCRIPT_DIR)
        rich.print(f"[bold green]Removed:[/bold green] {CACHE_SCRIPT_DIR}")
    else:
        rich.print("[dim]Cache directory does not exist – nothing to clean.[/dim]")


def _cmd_version() -> None:
    try:
        from usmo._version import __version__ as ver
    except ImportError:
        try:
            ver = pkg_version("usmo")
        except Exception:
            ver = "unknown (editable install without build)"
    rich.print(f"[bold]usm[/bold] version {ver}")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
        allow_interspersed_args=False,
    )
)
@click.argument("command", type=str, required=False, default=None)
@click.argument("args", nargs=-1, type=str)
@click.option("-h", "--help", "show_help", is_flag=True, help="Show this message and exit.")
@click.option(
    "--upgrade", "-U", is_flag=True, help="Upgrade the script before running."
)
@click.option("--debug", is_flag=True, help="Enable debug mode.")
def cli(command, args, show_help, upgrade, debug):
    # No command or bare --help → show overview.
    if command is None or (show_help and command is None):
        scripts = load_config(debug=debug, force_download=upgrade)
        _cmd_list(scripts)
        return

    # Built-in commands -------------------------------------------------
    if command == "version":
        _cmd_version()
        return
    if command == "clean":
        _cmd_clean()
        return

    # Commands below need the config.
    scripts = load_config(debug=debug, force_download=upgrade)

    if command == "list":
        _cmd_list(scripts)
        return
    if command == "update":
        _cmd_update(scripts)
        return

    # Help for a specific script ----------------------------------------
    if show_help:
        if command in scripts:
            rich.print(f"[bold]{command}[/bold]: {scripts[command]['description']}")
            rich.print("Usage:")
            rich.print(f"  usm {command} [ARGS...]")
        else:
            _cmd_list(scripts)
        return

    # Dispatch script ---------------------------------------------------
    if command not in scripts:
        rich.print(f"[bold red]Error:[/bold red] Unknown command '{command}'.")
        _cmd_list(scripts)
        raise click.ClickException(f"Unknown command '{command}'.")

    if debug:
        script_path = Path.cwd() / "scripts" / scripts[command]["path"]
    else:
        script_path = may_download(scripts, command, force_download=upgrade)

    if script_path.suffix == ".py":
        cmd = [sys.executable, str(script_path)] + list(args)
    else:
        cmd = ["bash", str(script_path)] + list(args)
    try:
        subprocess.run(cmd, check=True, text=True)
    except (subprocess.CalledProcessError, OSError) as e:
        rich.print(f"[bold red]An error occurred:[/bold red] {str(e)}")
        raise click.ClickException(str(e))


if __name__ == "__main__":
    cli()
