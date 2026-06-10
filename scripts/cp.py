import datetime
import os
import shlex
import subprocess
import urllib.parse
from pathlib import Path

import click
import yaml


def _config_file_from_cmdline(cmdline: list[str]) -> str | None:
    """Pick the blobfuse2 --config-file value out of a cmdline, in either form."""
    it = iter(enumerate(cmdline))
    for i, tok in it:
        if tok == "--config-file" and i + 1 < len(cmdline):
            return cmdline[i + 1]
        if tok.startswith("--config-file="):
            return tok.split("=", 1)[1]
        if tok in ("-c",) and i + 1 < len(cmdline):
            return cmdline[i + 1]
    return None


def _mount_dir_from_cmdline(cmdline: list[str]) -> str | None:
    """blobfuse2 `mount <DIR> [opts]` — DIR is the first positional after the subcommand."""
    if len(cmdline) < 3:
        return None
    if cmdline[1] != "mount":
        return None
    return cmdline[2]


def check_blobfuse2_mountpoints():
    import psutil

    mountpoints = {}
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmdline or "blobfuse2" not in cmdline[0]:
            continue
        try:
            mount_dir = _mount_dir_from_cmdline(cmdline)
            config_file = _config_file_from_cmdline(cmdline)
            if not mount_dir or not config_file:
                continue
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            azstorage = (
                (config.get("azstorage") or {}) if isinstance(config, dict) else {}
            )
            account_name = azstorage.get("account-name")
            container_name = azstorage.get("container")
            if not account_name or not container_name:
                continue
            mountpoints[mount_dir] = {
                "url": f"https://{account_name}.blob.core.windows.net/{container_name}/",
                "account_name": account_name,
                "container_name": container_name,
            }
        except (OSError, yaml.YAMLError, KeyError, IndexError):
            continue
    return mountpoints


def generate_sas_token(account_name, container_name, expiry_days: int = 7):
    expiry_date = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=expiry_days)
    ).strftime("%Y-%m-%dT%H:%MZ")
    command = [
        "az",
        "storage",
        "container",
        "generate-sas",
        "--account-name",
        account_name,
        "--name",
        container_name,
        "--permissions",
        "rwdlac",
        "--expiry",
        expiry_date,
        "--auth-mode",
        "login",
        "--as-user",
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=True)
    if result.returncode == 0:
        sas_token = result.stdout.strip().strip('"')
        return sas_token
    else:
        raise Exception(f"Failed to generate SAS token: {result.stderr}")


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.argument("args", nargs=-1, type=str)
@click.option(
    "--use-sas-token",
    "-s",
    is_flag=True,
    default=True,
    help="Use SAS token for authentication when copying from blobfuse2 mountpoints.",
)
@click.option(
    "--dry-run",
    "-d",
    is_flag=True,
    default=False,
    help="Only print the commands that would be executed, without actually running them.",
)
def copy(args, use_sas_token: bool = True, dry_run: bool = False):
    def run_cmd(cmd):
        if dry_run:
            click.echo(shlex.join(str(c) for c in cmd))
        else:
            subprocess.run(cmd)

    if len(args) < 2:
        click.echo("Usage: usm cp [SOURCE] [DESTINATION]")
        return

    paths = [Path(p).resolve() for p in args]
    mountpoints = check_blobfuse2_mountpoints()

    def is_blobfuse_path(p):
        return any(str(p).startswith(mp) for mp in mountpoints.keys())

    def maybe_path(p):
        for mp, mp_info in mountpoints.items():
            if str(p).startswith(mp):
                url = mp_info["url"]
                relative_path = str(p)[len(mp) :].lstrip("/")
                p = url + urllib.parse.quote(relative_path, safe="/")
                if use_sas_token:
                    sas_token = generate_sas_token(
                        mp_info["account_name"], mp_info["container_name"]
                    )
                    p += "?" + sas_token
        return p

    if not any(is_blobfuse_path(p) for p in paths):
        click.echo(
            "No blobfuse2 mountpoints detected in the provided paths. Handing over to native cp."
        )

        run_cmd(["cp", "-r"] + list(args))
        return

    os.environ["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
    sources = paths[:-1]
    destination = paths[-1]
    if is_blobfuse_path(destination):
        click.echo("Copying files using azcopy...")

        for src in sources:
            run_cmd(
                [
                    "azcopy",
                    "copy",
                    maybe_path(src),
                    maybe_path(destination),
                    "--recursive",
                ]
            )
    else:
        for src in sources:
            if is_blobfuse_path(src):
                click.echo(
                    f"Copying from blobfuse2 mountpoint {src} to local path {destination} using azcopy..."
                )

                run_cmd(
                    [
                        "azcopy",
                        "copy",
                        maybe_path(src),
                        str(destination),
                        "--recursive",
                    ]
                )
            else:
                run_cmd(["cp", "-r", str(src), str(destination)])


if __name__ == "__main__":
    copy()
