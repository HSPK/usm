#!/usr/bin/env python3
"""Encrypted env store. Stash secrets locally, inject them when you need them.

  usm secret set OPENAI_API_KEY=sk-...
  usm secret set --group prod DB_URL=postgres://...
  usm secret ls                          # list keys (values hidden)
  usm secret ls --group prod
  usm secret get OPENAI_API_KEY
  usm secret rm OPENAI_API_KEY
  eval "$(usm secret export prod)"        # inject into current shell
  usm secret run prod -- python app.py    # spawn with secrets in env

The store lives at ~/.config/usm/secrets.json.enc, encrypted with a Fernet
key at ~/.config/usm/secret.key (chmod 600, auto-generated on first use).
A 'default' group is used when --group is omitted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
from cryptography.fernet import Fernet, InvalidToken
from rich.console import Console
from rich.table import Table

CONFIG_DIR = Path.home() / ".config" / "usm"
KEY_PATH = CONFIG_DIR / "secret.key"
STORE_PATH = CONFIG_DIR / "secrets.json.enc"
DEFAULT_GROUP = "default"

console = Console()


# Crypto -----------------------------------------------------------------


def _write_secure(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* with mode 0o600 (no umask race)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # os.fdopen takes ownership of fd; the `with` block closes it.
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_key() -> bytes:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        _write_secure(KEY_PATH, Fernet.generate_key())
        console.print(
            f"[dim]Generated new encryption key at {KEY_PATH}. "
            f"Back it up if you care about not losing your secrets.[/dim]"
        )
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    return KEY_PATH.read_bytes()


def _load() -> dict[str, dict[str, str]]:
    if not STORE_PATH.exists():
        return {}
    f = Fernet(_ensure_key())
    blob = STORE_PATH.read_bytes()
    try:
        plain = f.decrypt(blob)
    except InvalidToken as e:
        raise click.ClickException(
            f"Cannot decrypt {STORE_PATH} with key at {KEY_PATH}. "
            "Did the key change? Restore the original key or delete the store."
        ) from e
    try:
        return json.loads(plain)
    except json.JSONDecodeError:
        return {}


def _save(data: dict[str, dict[str, str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = Fernet(_ensure_key())
    _write_secure(STORE_PATH, f.encrypt(json.dumps(data, indent=2).encode()))


def _split_kv(arg: str) -> tuple[str, str]:
    if "=" not in arg:
        raise click.BadParameter(f"Expected KEY=VALUE, got {arg!r}.")
    k, _, v = arg.partition("=")
    if not k:
        raise click.BadParameter(f"Empty key in {arg!r}.")
    return k, v


def _envify(group: dict[str, str]) -> str:
    lines = []
    for k, v in group.items():
        esc = v.replace("'", "'\\''")
        lines.append(f"export {k}='{esc}'")
    return "\n".join(lines)


# CLI ---------------------------------------------------------------------


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Encrypted local env store.",
)
def cli():
    pass


@cli.command("init", help="Generate the encryption key (auto-called on first use).")
def cmd_init():
    _ensure_key()
    console.print(f"[green]✓[/green] key at {KEY_PATH}")
    if STORE_PATH.exists():
        console.print(f"[dim]store at {STORE_PATH}[/dim]")


@cli.command("set", help="Store one or more KEY=VALUE pairs.")
@click.option(
    "-g",
    "--group",
    default=DEFAULT_GROUP,
    show_default=True,
    help="Group / profile name.",
)
@click.argument("entries", nargs=-1, required=True)
def cmd_set(group, entries):
    data = _load()
    bucket = data.setdefault(group, {})
    for entry in entries:
        k, v = _split_kv(entry)
        bucket[k] = v
    _save(data)
    console.print(
        f"[green]✓[/green] stored {len(entries)} key(s) into group [bold]{group}[/bold]"
    )


@cli.command("get", help="Print one value to stdout.")
@click.option("-g", "--group", default=DEFAULT_GROUP, show_default=True)
@click.argument("key")
def cmd_get(group, key):
    data = _load()
    bucket = data.get(group, {})
    if key not in bucket:
        raise click.ClickException(f"{group}/{key} not found.")
    sys.stdout.write(bucket[key])
    if sys.stdout.isatty():
        sys.stdout.write("\n")


@cli.command("ls", help="List keys (values hidden unless --reveal).")
@click.option("-g", "--group", default=None, help="Limit to one group.")
@click.option("--reveal", is_flag=True, help="Show values in plaintext.")
def cmd_ls(group, reveal):
    data = _load()
    if not data:
        console.print("[dim]store is empty.[/dim]")
        return
    groups = [group] if group else sorted(data)
    for g in groups:
        bucket = data.get(g, {})
        if not bucket:
            continue
        table = Table(title=f"group: {g}", header_style="bold")
        table.add_column("KEY")
        table.add_column("VALUE" if reveal else "VALUE (hidden)")
        for k in sorted(bucket):
            v = bucket[k]
            shown = v if reveal else _mask(v)
            table.add_row(k, shown)
        console.print(table)


def _mask(v: str) -> str:
    if len(v) <= 8:
        return "•" * len(v)
    return v[:3] + "•" * 6 + v[-2:]


@cli.command("rm", help="Remove KEY(s) from a group.")
@click.option("-g", "--group", default=DEFAULT_GROUP, show_default=True)
@click.argument("keys", nargs=-1, required=True)
def cmd_rm(group, keys):
    data = _load()
    bucket = data.get(group, {})
    removed = []
    for k in keys:
        if bucket.pop(k, None) is not None:
            removed.append(k)
    if not bucket and group in data:
        del data[group]
    _save(data)
    console.print(
        f"[green]✓[/green] removed {len(removed)} key(s) from [bold]{group}[/bold]"
        + (
            f" — missing: {', '.join(set(keys) - set(removed))}"
            if len(removed) != len(keys)
            else ""
        )
    )


@cli.command(
    "export",
    help=(
        "Emit `export KEY=VAL` lines to stdout, suitable for "
        'eval "$(usm secret export [GROUP])".'
    ),
)
@click.argument("group", required=False, default=DEFAULT_GROUP)
def cmd_export(group):
    data = _load()
    bucket = data.get(group)
    if not bucket:
        raise click.ClickException(f"group {group!r} is empty or doesn't exist.")
    click.echo(_envify(bucket))


@cli.command(
    "run",
    context_settings={"ignore_unknown_options": True},
    help="Run a command with the group's secrets injected as env vars.",
)
@click.argument("group")
@click.argument("cmd", nargs=-1, required=True, type=click.UNPROCESSED)
def cmd_run(group, cmd):
    data = _load()
    bucket = data.get(group)
    if not bucket:
        raise click.ClickException(f"group {group!r} is empty or doesn't exist.")
    env = os.environ.copy()
    env.update(bucket)
    try:
        os.execvpe(cmd[0], list(cmd), env)
    except FileNotFoundError as e:
        raise click.ClickException(f"{cmd[0]}: {e}") from e


if __name__ == "__main__":
    cli()
