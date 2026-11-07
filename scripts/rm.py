"""Delete Azure Blob files and directories after explicit confirmation."""

import os
import shlex
import subprocess
import urllib.parse
from pathlib import Path

import click

import cp


def _target_url(target: str, mountpoints: dict) -> str:
    if cp._is_https_blob(target):
        cp._parse_blob_url(target)
        return target

    resolved = Path(target).resolve()
    for mount in sorted(mountpoints, key=lambda p: len(Path(p).parts), reverse=True):
        try:
            relative = resolved.relative_to(Path(mount).resolve())
        except ValueError:
            continue
        path = "" if relative == Path(".") else relative.as_posix()
        return (
            mountpoints[mount]["url"].rstrip("/")
            + "/"
            + urllib.parse.quote(path, safe="/")
        )
    raise click.BadParameter(
        f"{target!r} is not an Azure Blob URL or a path under a blobfuse2 mount",
        param_hint="TARGET",
    )


def _display_url(url: str) -> str:
    return urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()


def _authorize_url(url: str, *, use_az: bool) -> str:
    if not use_az and not cp._has_sas(url):
        account, container = cp._parse_blob_url(url)
        sas = cp.generate_sas_token(account, container)
        parsed = urllib.parse.urlsplit(url)
        query = parsed.query + "&" + sas if parsed.query else sas
        return parsed._replace(query=query).geturl()
    return url


def _azcopy_env(*, use_az: bool) -> dict[str, str]:
    env = os.environ.copy()
    if use_az:
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
    return env


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("targets", nargs=-1, required=True, metavar="TARGET...")
@click.option(
    "--use-az",
    is_flag=True,
    help="Authenticate azcopy via Azure CLI login instead of generating SAS tokens.",
)
@click.option(
    "--dry-run",
    "-d",
    is_flag=True,
    help="Print commands without deleting, generating SAS tokens, or installing azcopy.",
)
def remove(targets: tuple[str, ...], use_az: bool, dry_run: bool) -> None:
    """Delete Azure Blob files/directories, recursively, after confirmation.

    TARGET may be an Azure Blob URL or a path under a blobfuse2 mount.
    Local paths are rejected. Directories include all their contents.
    """
    mountpoints = cp.check_blobfuse2_mountpoints()
    urls = [_target_url(target, mountpoints) for target in targets]

    click.echo("Azure Blob targets to delete (recursively, including all contents):")
    for url in urls:
        click.echo(f"  {_display_url(url)}")

    if dry_run:
        azcopy = cp._find_azcopy() or "azcopy"
        if use_az:
            click.echo("AZCOPY_AUTO_LOGIN_TYPE=AZCLI")
        for url in urls:
            click.echo(shlex.join([azcopy, "remove", url, "--recursive"]))
        return

    click.confirm("Delete these targets?", default=False, abort=True)

    env = _azcopy_env(use_az=use_az)
    try:
        authorized_urls = [_authorize_url(url, use_az=use_az) for url in urls]
        azcopy = cp.ensure_azcopy()
        for url in authorized_urls:
            result = subprocess.run([azcopy, "remove", url, "--recursive"], env=env)
            if result.returncode:
                click.echo(
                    f"Error: azcopy remove failed for {_display_url(url)} "
                    f"(exit code {result.returncode}).",
                    err=True,
                )
                click.get_current_context().exit(result.returncode)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"Azure CLI authentication failed (exit code {exc.returncode})."
        ) from exc
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    remove()
