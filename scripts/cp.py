import click


def check_blobfuse2_mountpoints():
    # filter all processes which command contain 'blobfuse2'
    import psutil

    mountpoints = set()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if cmdline and "blobfuse2" in cmdline[0]:
                for i, arg in enumerate(cmdline):
                    if arg == "--mount-path" and i + 1 < len(cmdline):
                        mountpoints.add(cmdline[i + 1])
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
    sources = args[:-1]
    destination = args[-1]
