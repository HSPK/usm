"""Move Azure Blob files/directories by verified copy followed by deletion."""

import json
import shlex
import subprocess
import urllib.parse

import click

import cp
import rm


def _validate_paths(source: str, destination: str) -> None:
    identities = []
    for url in (source, destination):
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path).rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)
        if (
            parsed.fragment
            or "*" in path
            or "?" in path
            or any(part in (".", "..", "") for part in path.split("/")[1:])
            or any(key.lower() in ("snapshot", "versionid") for key in query)
        ):
            raise click.UsageError(
                "Move requires explicit paths without wildcards, dot segments, "
                "fragments, snapshots, or version IDs."
            )
        host = parsed.netloc.lower().replace(".dfs.core.", ".blob.core.")
        identities.append((host, path))

    source_host, source_path = identities[0]
    destination_host, destination_path = identities[1]
    if source_path.count("/") < 2:
        raise click.UsageError("Move a file or directory, not an entire container.")
    if source_host == destination_host and (
        source_path == destination_path
        or source_path.startswith(destination_path + "/")
        or destination_path.startswith(source_path + "/")
    ):
        raise click.UsageError("Source and destination must not be equal or overlap.")


def _verify_copy(stdout: str) -> int:
    summary = None
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            if event.get("MessageType") in ("Error", "Prompt"):
                raise ValueError("copy reported an error")
            if event.get("MessageType") == "EndOfJob":
                content = event.get("MessageContent")
                summary = json.loads(content) if isinstance(content, str) else content
        if not isinstance(summary, dict) or summary.get("JobStatus") != "Completed":
            raise ValueError("missing completed job summary")
        counters = []
        for key in ("TransfersCompleted", "TransfersFailed", "TransfersSkipped"):
            raw = summary.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise ValueError("invalid transfer counter")
            value = int(raw)
            if value < 0:
                raise ValueError("negative transfer counter")
            counters.append(value)
        completed, failed, skipped = counters
        if failed or skipped or not completed or summary.get("ErrorMsg"):
            raise ValueError("copy was empty, failed, or skipped transfers")
    except (ValueError, TypeError) as exc:
        raise click.ClickException(
            "Copy could not be verified as fully successful; source retained."
        ) from exc
    return completed


def _check_exit(result: subprocess.CompletedProcess, operation: str) -> None:
    if result.returncode:
        code = result.returncode
        click.echo(
            f"Error: azcopy {operation} failed (exit code {code}); "
            "move incomplete, source may remain.",
            err=True,
        )
        click.get_current_context().exit(128 - code if code < 0 else code)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source")
@click.argument("destination")
@click.option(
    "--use-az",
    is_flag=True,
    help="Authenticate azcopy via Azure CLI login instead of generating SAS tokens.",
)
@click.option(
    "--dry-run",
    "-d",
    is_flag=True,
    help="Print commands without moving, generating SAS tokens, or installing azcopy.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Allow overwriting conflicting destination files (default: refuse).",
)
def move(source: str, destination: str, use_az: bool, dry_run: bool, overwrite: bool):
    """Move an Azure Blob file/directory to its final DESTINATION path.

    Both paths must be Azure URLs or paths under blobfuse2 mounts.
    Copies recursively, verifies completion, then deletes the source.
    This is not atomic: keep source and destination unchanged during the move.
    """
    mounts = cp.check_blobfuse2_mountpoints()
    source_url = rm._target_url(source, mounts)
    destination_url = rm._target_url(destination, mounts)
    _validate_paths(source_url, destination_url)

    click.echo(f"Source:      {rm._display_url(source_url)}")
    click.echo(f"Destination: {rm._display_url(destination_url)}")
    click.echo("Copy recursively, verify all transfers, then delete the source.")
    if overwrite:
        click.echo("Conflicting destination files WILL be overwritten.")

    copy_flags = [
        "--recursive",
        "--as-subdir=false",
        f"--overwrite={'true' if overwrite else 'false'}",
        "--output-type=json",
        "--output-level=essential",
    ]
    if dry_run:
        azcopy = cp._find_azcopy() or "azcopy"
        if use_az:
            click.echo("AZCOPY_AUTO_LOGIN_TYPE=AZCLI")
        click.echo(
            shlex.join([azcopy, "copy", source_url, destination_url, *copy_flags])
        )
        click.echo("# Only after a verified, fully successful copy:")
        click.echo(shlex.join([azcopy, "remove", source_url, "--recursive"]))
        return

    click.confirm(
        "Move this file/directory and delete the source?", default=False, abort=True
    )
    env = rm._azcopy_env(use_az=use_az)
    try:
        source_url = rm._authorize_url(source_url, use_az=use_az)
        destination_url = rm._authorize_url(destination_url, use_az=use_az)
        azcopy = cp.ensure_azcopy()
        click.echo("Copying with azcopy; source will be retained unless verified...")
        result = subprocess.run(
            [azcopy, "copy", source_url, destination_url, *copy_flags],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        _check_exit(result, "copy")
        completed = _verify_copy(result.stdout)
        click.echo(f"Verified {completed} completed transfers. Deleting source...")
        result = subprocess.run([azcopy, "remove", source_url, "--recursive"], env=env)
        _check_exit(result, "remove")
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"Azure CLI authentication failed (exit code {exc.returncode}); "
            "source retained."
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Move incomplete: {exc}") from exc
    click.echo("Move completed.")


if __name__ == "__main__":
    move()
