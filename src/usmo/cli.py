import subprocess
from pathlib import Path

import click
import rich

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/scripts/"


SCRIPTS = {
    "init": {
        "description": "Initialize a new machine setup.",
        "path": "init.sh",
    },
    "blobmount": {
        "description": "Mount a blob storage as a filesystem.",
        "path": "blobmount.sh",
    },
    "cu122": {
        "description": "Setup CUDA 12.2 environment.",
        "path": "cu122.sh",
    },
    "cp": {
        "description": "Copy files with blob storage support.",
        "path": "cp.py",
    },
    "check_py": {
        "description": "Check Python3 installation and version.",
        "path": "check_py.sh",
    },
}


def download_script(script_filename: str) -> Path:
    import requests

    script_url = f"{RESOURCE_BASE_URL}{script_filename}"
    rich.print(
        f"[bold green]Downloading script:[/bold green] {script_filename} from {script_url}"
    )
    response = requests.get(script_url)
    if response.status_code == 200:
        script_path = CACHE_SCRIPT_DIR / script_filename
        CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        with open(script_path, "wb") as file:
            file.write(response.content)
        script_path.chmod(script_path.stat().st_mode | 0o111)
        return script_path
    else:
        raise Exception(
            f"Failed to download script: {script_filename}. "
            f"Status code: {response.status_code}"
        )


def may_download(
    script_name: str,
    download: bool = True,
    force_download: bool = False,
) -> Path:
    script_filename = SCRIPTS[script_name]["path"]
    script_path = CACHE_SCRIPT_DIR / script_filename
    if force_download or (download and not script_path.exists()):
        return download_script(script_filename)
    elif not script_path.exists():
        raise FileNotFoundError(
            f"Script {script_name} not found in cache. Please download it first."
        )
    return script_path


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
        allow_interspersed_args=False,
    )
)
@click.argument("script", type=str, required=True)
@click.argument("args", nargs=-1, type=str)
@click.option("-h", "--help", is_flag=True, help="Show this message and exit.")
@click.option(
    "--upgrade", "-U", is_flag=True, help="Upgrade the script before running."
)
@click.option("--debug", is_flag=True, help="Enable debug mode.")
def cli(script, args, help, upgrade, debug):
    if help:
        if script in SCRIPTS:
            rich.print(f"[bold]{script}[/bold]: {SCRIPTS[script]['description']}")
            rich.print("Usage:")
            rich.print(f"  usmo {script} [ARGS...]")
        else:
            rich.print("Available scripts:")
            for name, info in SCRIPTS.items():
                rich.print(f" - [bold]{name}[/bold]: {info['description']}")
        return

    if script not in SCRIPTS:
        rich.print(f"[bold red]Error:[/bold red] Unknown script '{script}'.")
        rich.print("Available scripts:")
        for name, info in SCRIPTS.items():
            rich.print(f" - [bold]{name}[/bold]: {info['description']}")
        raise click.ClickException(f"Unknown script '{script}'.")

    if debug:
        script_path = Path.cwd() / "scripts" / SCRIPTS[script]["path"]
    else:
        script_path = may_download(script, force_download=upgrade)

    if script_path.suffix == ".py":
        command = ["python3", str(script_path)] + list(args)
    else:
        command = ["bash", str(script_path)] + list(args)
    try:
        subprocess.run(command, check=True, text=True)
    except Exception as e:
        rich.print(f"[bold red]An error occurred:[/bold red] {str(e)}")
        raise click.ClickException(str(e))


if __name__ == "__main__":
    cli()
