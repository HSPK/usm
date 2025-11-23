import click
import yaml
from pathlib import Path


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

                mountpoints[cmdline[2]] = (
                    f"https://{account_name}.blob.core.windows.net/{container_name}/"
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return mountpoints


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.argument("args", nargs=-1, type=str)
def copy(args):
    if len(args) < 2:
        click.echo("Usage: usmo cp [SOURCE] [DESTINATION]")
        return

    paths = [Path(p).resolve() for p in args]
    mountpoints = check_blobfuse2_mountpoints()

    def is_blobfuse_path(p):
        return any(str(p).startswith(mp) for mp in mountpoints.keys())

    def maybe_path(p):
        for mp, url in mountpoints.items():
            if str(p).startswith(mp):
                relative_path = str(p)[len(mp) :].lstrip("/")
                return url + relative_path
        return p

    if not any(is_blobfuse_path(p) for p in paths):
        click.echo(
            "No blobfuse2 mountpoints detected in the provided paths. Handing over to native cp."
        )
        import subprocess

        subprocess.run(["cp", "-r"] + list(args))
        return

    sources = paths[:-1]
    destination = paths[-1]
    if is_blobfuse_path(destination):
        click.echo("Copying files using azcopy...")
        import subprocess

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
                import subprocess

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
                import subprocess

                subprocess.run(["cp", "-r", str(src), str(destination)])


if __name__ == "__main__":
    copy()
