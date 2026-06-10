#!/usr/bin/env python3
"""rsync wrapper with sensible defaults and auto-excludes.

Defaults: -avh --info=progress2 --partial --partial-dir=.rsync-tmp --human-readable
Auto-excludes (off via --no-default-excludes):
  .git/  .venv/  venv/  node_modules/  __pycache__/  *.pyc
  .DS_Store  .mypy_cache/  .pytest_cache/  .ruff_cache/

Examples:
  usm rsync ./project user@host:~/                # local -> remote
  usm rsync user@host:~/data ./data/              # remote -> local
  usm rsync -n ./src user@host:~/dev              # dry-run
  usm rsync --delete ./build user@host:/srv/app/  # mirror (also deletes)
  usm rsync -- -P -z ./src user@host:~/           # pass extra rsync flags after --
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys

import click

DEFAULT_FLAGS = [
    "-avh",
    "--human-readable",
    "--info=progress2",
    "--partial",
    "--partial-dir=.rsync-tmp",
]
DEFAULT_EXCLUDES = [
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
]


@click.command(
    context_settings={
        "help_option_names": ["-h", "--help"],
        "ignore_unknown_options": True,
    },
    help="rsync wrapper. Pass extra rsync flags after `--`.",
)
@click.option(
    "--no-default-excludes",
    is_flag=True,
    help="Don't add the default exclude list.",
)
@click.option(
    "--delete",
    is_flag=True,
    help="Mirror mode — delete files on the destination that aren't in the source.",
)
@click.option(
    "-n", "--dry-run", is_flag=True, help="Show what would change without doing it."
)
@click.option(
    "-i",
    "--ssh-key",
    type=click.Path(),
    help="Use this SSH identity (-e 'ssh -i KEY').",
)
@click.option("-p", "--ssh-port", type=int, help="SSH port (-e 'ssh -p PORT').")
@click.option(
    "-e",
    "--exclude",
    "extra_excludes",
    multiple=True,
    help="Extra --exclude pattern (repeatable).",
)
@click.option(
    "--print-cmd", is_flag=True, help="Print the resolved rsync command and exit."
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def cli(
    no_default_excludes,
    delete,
    dry_run,
    ssh_key,
    ssh_port,
    extra_excludes,
    print_cmd,
    args,
):
    if not shutil.which("rsync"):
        raise click.ClickException("rsync not on PATH. Install it first.")
    if len(args) < 2:
        raise click.UsageError(
            "Need at least one source and one destination. Example:\n"
            "  usm rsync ./project user@host:~/"
        )
    argv = ["rsync", *DEFAULT_FLAGS]
    if not no_default_excludes:
        for pat in DEFAULT_EXCLUDES:
            argv += ["--exclude", pat]
    for pat in extra_excludes:
        argv += ["--exclude", pat]
    if delete:
        argv.append("--delete")
    if dry_run:
        argv.append("--dry-run")
    if ssh_key or ssh_port:
        ssh = ["ssh"]
        if ssh_key:
            ssh += ["-i", ssh_key]
        if ssh_port:
            ssh += ["-p", str(ssh_port)]
        argv += ["-e", shlex.join(ssh)]
    argv += list(args)
    if print_cmd:
        click.echo(shlex.join(argv))
        return
    sys.exit(subprocess.call(argv))


if __name__ == "__main__":
    cli()
