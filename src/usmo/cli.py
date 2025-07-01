from pathlib import Path

import click
import rich

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/"


def download_script(script_name: str) -> Path:
    import requests

    script_url = f"{RESOURCE_BASE_URL}{script_name}.sh"
    rich.print(
        f"[bold green]Downloading script:[/bold green] {script_name} from {script_url}"
    )
    response = requests.get(script_url)
    if response.status_code == 200:
        script_path = CACHE_SCRIPT_DIR / f"{script_name}.sh"
        CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        with open(script_path, "wb") as file:
            file.write(response.content)
        script_path.chmod(script_path.stat().st_mode | 0o111)
        return script_path
    else:
        raise Exception(
            f"Failed to download script: {script_name}. Status code: {response.status_code}"
        )


def get_script_path(script_name: str, download: bool = True) -> Path:
    script_path = CACHE_SCRIPT_DIR / f"{script_name}.sh"
    if download and not script_path.exists():
        return download_script(script_name)
    elif not script_path.exists():
        raise FileNotFoundError(
            f"Script {script_name} not found in cache. Please download it first."
        )
    return script_path


@click.command()
@click.argument("script", type=str, required=True)
@click.argument("args", nargs=-1, type=str)
@click.option("-h", "--help", is_flag=True, help="Show this message and exit.")
@click.option("-V", "--version", is_flag=True, help="Show the version and exit.")
def cli(script, args, help, version):
    if help:
        click.echo(cli.get_help(click.Context(cli)))
    elif version:
        click.echo("Version 1.0.0")

    script_path = get_script_path(script)
    import subprocess

    command = ["bash", str(script_path)] + list(args)
    try:
        subprocess.run(command, check=True, text=True)
    except Exception as e:
        rich.print(f"[bold red]An error occurred:[/bold red] {str(e)}")
        raise click.ClickException(str(e))


if __name__ == "__main__":
    cli()
