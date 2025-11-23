import datetime
import click
import yaml
from pathlib import Path
import subprocess
import os


def check_blobfuse2_mountpoints():
    # filter all processes which command contain 'blobfuse2'
    import psutil

    mountpoints = {}
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if cmdline and "blobfuse2" in cmdline[0]:
                config_file = cmdline[4]
                config = yaml.safe_load(open(config_file))
                account_name = config["azstorage"]["account-name"]
                container_name = config["azstorage"]["container"]

                mountpoints[cmdline[2]] = {
                    "url": f"https://{account_name}.blob.core.windows.net/{container_name}/",
                    "account_name": account_name,
                    "container_name": container_name,
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
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
    default=False,
    help="Use SAS token for authentication when copying from blobfuse2 mountpoints.",
)
def copy(args, use_sas_token: bool = False):
    if len(args) < 2:
        click.echo("Usage: usmo cp [SOURCE] [DESTINATION]")
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
                p = url + relative_path
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

        subprocess.run(["cp", "-r"] + list(args))
        return

    os.environ["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
    sources = paths[:-1]
    destination = paths[-1]
    if is_blobfuse_path(destination):
        click.echo("Copying files using azcopy...")

        for src in sources:
            subprocess.run(
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

                subprocess.run(
                    [
                        "azcopy",
                        "copy",
                        maybe_path(src),
                        str(destination),
                        "--recursive",
                    ]
                )
            else:
                subprocess.run(["cp", "-r", str(src), str(destination)])


if __name__ == "__main__":
    copy()
