#!/usr/bin/env python3
"""SSH inventory for people who work across many machines.

Manages only a marker-fenced block in ``~/.ssh/config``; hand-written config
outside that block is preserved byte-for-byte. Unmanaged parsing is deliberately
simple and tolerant: it reads basic ``Host``/``HostName``/``User``/``Port``/
``IdentityFile`` stanzas, ignores ``Include``, and skips wildcards it cannot
resolve.

Examples
--------
  usm host add gpu user@gpu.example.com:2222 --identity ~/.ssh/id_ed25519 --tag gpu
  usm host ls --all
  usm host check gpu
  usm host exec --tag gpu -- nvidia-smi
  usm host connect gpu -- -A
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import click
from usmo import ui

BEGIN_MARKER = "# >>> usm host >>>"
END_MARKER = "# <<< usm host <<<"
CONTEXT = {"help_option_names": ["-h", "--help"]}
DEFAULT_PARALLEL = 8
OUTPUT_LIMIT = 4000


@dataclass
class HostEntry:
    alias: str
    hostname: str = ""
    user: str = ""
    port: str = ""
    identity: str = ""
    jump: str = ""
    forward_agent: bool = False
    options: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    managed: bool = True

    @property
    def target(self) -> str:
        left = f"{self.user}@{self.hostname}" if self.user else self.hostname
        return f"{left}:{self.port}" if self.port else left


def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def config_path() -> Path:
    return ssh_dir() / "config"


def read_config_text() -> str:
    path = config_path()
    if not path.exists():
        return ""
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(
            "Could not read ~/.ssh/config as UTF-8; fix it manually first."
        ) from exc


def validate_plain(value: str, label: str) -> None:
    if not value or any(ch in value for ch in "\r\n\0"):
        raise click.ClickException(f"Invalid {label}: must be one line.")


def validate_alias(alias: str) -> None:
    validate_plain(alias, "alias")
    if len(alias) > 255:
        raise click.ClickException("Invalid alias: must be 255 characters or fewer.")
    if any(ch.isspace() for ch in alias) or alias.startswith("-"):
        raise click.ClickException(
            "Invalid alias: whitespace and leading '-' are not allowed."
        )
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", alias):
        raise click.ClickException(
            "Invalid alias: use only letters, digits, '.', '_', ':', and '-'."
        )
    if any(ch in alias for ch in "*?[]"):
        raise click.ClickException("Invalid alias: wildcards are not managed aliases.")


def parse_target(target: str) -> tuple[str, str, str]:
    validate_plain(target, "target")
    user = ""
    host_port = target
    if "@" in target:
        user, host_port = target.split("@", 1)
        validate_plain(user, "user")
    host = host_port
    port = ""
    if host_port.count(":") == 1:
        host, port = host_port.rsplit(":", 1)
        if port and not port.isdigit():
            raise click.ClickException("Invalid target: port must be numeric.")
    if not host:
        raise click.ClickException("Invalid target: missing host.")
    validate_plain(host, "host")
    return user, host, port


def split_managed_block(content: str) -> tuple[str, str, bool]:
    begin = content.find(BEGIN_MARKER)
    end = content.find(END_MARKER)
    if begin == -1 and end == -1:
        return content, "", False
    if begin == -1 or end == -1 or end < begin:
        raise click.ClickException(
            "Found an incomplete usm host block; fix ~/.ssh/config manually first."
        )
    if content.find(BEGIN_MARKER, begin + len(BEGIN_MARKER)) != -1:
        raise click.ClickException(
            "Found duplicate usm host block markers; fix ~/.ssh/config manually first."
        )
    block_end = end + len(END_MARKER)
    if block_end < len(content) and content[block_end : block_end + 1] == "\n":
        block_end += 1
    return content[:begin] + content[block_end:], content[begin:block_end], True


def parse_block(block: str) -> dict[str, HostEntry]:
    entries: dict[str, HostEntry] = {}
    current: HostEntry | None = None
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line in {BEGIN_MARKER, END_MARKER}:
            continue
        if line.startswith("# usm-tags:") and current:
            current.tags = [
                t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()
            ]
            continue
        if line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        value = value.strip()
        if key.lower() == "host":
            if value:
                current = HostEntry(alias=value)
                entries[current.alias] = current
            continue
        if current is None:
            continue
        lower = key.lower()
        if lower == "hostname":
            current.hostname = value
        elif lower == "user":
            current.user = value
        elif lower == "port":
            current.port = value
        elif lower == "identityfile":
            current.identity = value
        elif lower == "proxyjump":
            current.jump = value
        elif lower == "forwardagent":
            current.forward_agent = value.lower() in {"yes", "true", "1"}
        else:
            current.options.append(f"{key} {value}".rstrip())
    return entries


def read_config() -> tuple[str, dict[str, HostEntry]]:
    content = read_config_text()
    _, block, _ = split_managed_block(content)
    return content, parse_block(block)


def render_block(entries: dict[str, HostEntry]) -> str:
    lines = [BEGIN_MARKER]
    for alias in sorted(entries):
        entry = entries[alias]
        lines.append(f"Host {entry.alias}")
        lines.append(f"    HostName {entry.hostname}")
        if entry.user:
            lines.append(f"    User {entry.user}")
        if entry.port:
            lines.append(f"    Port {entry.port}")
        if entry.identity:
            lines.append(f"    IdentityFile {entry.identity}")
        if entry.jump:
            lines.append(f"    ProxyJump {entry.jump}")
        if entry.forward_agent:
            lines.append("    ForwardAgent yes")
        if entry.tags:
            lines.append(f"    # usm-tags: {','.join(entry.tags)}")
        lines.extend(f"    {opt}" for opt in entry.options)
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def atomic_write_config(content: str) -> None:
    cfg = config_path()
    directory = cfg.parent
    if cfg.is_symlink():
        raise click.ClickException(
            "Refusing to edit ~/.ssh/config because it is a symlink."
        )
    if cfg.exists() and not (cfg.stat().st_mode & 0o200):
        raise click.ClickException("Refusing to edit read-only ~/.ssh/config.")
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for stale in directory.glob(f".{cfg.name}.usm.*.tmp"):
        stale.unlink(missing_ok=True)
    old_mode = (cfg.stat().st_mode & 0o777) if cfg.exists() else 0o600
    write_mode = old_mode & 0o600 or 0o600
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_name("config.usm.bak"))
    tmp = cfg.with_name(f".{cfg.name}.usm.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, write_mode)
        tmp.replace(cfg)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(cfg, write_mode)


def write_entries(entries: dict[str, HostEntry]) -> None:
    content = read_config_text()
    begin = content.find(BEGIN_MARKER)
    end = content.find(END_MARKER)
    outside, _, had_block = split_managed_block(content)
    block = render_block(entries)
    if had_block:
        block_end = end + len(END_MARKER)
        if block_end < len(content) and content[block_end : block_end + 1] == "\n":
            block_end += 1
        before = content[:begin]
        after = content[block_end:]
        updated = before + block + after if entries else before + after
    elif outside and not outside.endswith("\n"):
        updated = outside + "\n\n" + block
    elif outside:
        updated = outside + ("" if outside.endswith("\n\n") else "\n") + block
    else:
        updated = block
    atomic_write_config(updated)


def unmanaged_entries(content: str) -> dict[str, HostEntry]:
    outside, _, _ = split_managed_block(content)
    entries: dict[str, HostEntry] = {}
    current: list[HostEntry] = []
    for raw in outside.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key in {"include", "match"}:
            current = []
            continue
        if key == "host":
            aliases = value.split()
            usable = [a for a in aliases if not any(ch in a for ch in "*?[]")]
            current = []
            for alias in usable:
                entry = HostEntry(alias=alias, managed=False)
                entries[entry.alias] = entry
                current.append(entry)
            continue
        if not current:
            continue
        if key == "hostname":
            for entry in current:
                entry.hostname = value
        elif key == "user":
            for entry in current:
                entry.user = value
        elif key == "port":
            for entry in current:
                entry.port = value
        elif key == "identityfile":
            for entry in current:
                entry.identity = value
    return entries


def all_entries(include_unmanaged: bool = False) -> dict[str, HostEntry]:
    content, managed = read_config()
    if include_unmanaged:
        merged = unmanaged_entries(content)
        merged.update(managed)
        return merged
    return managed


def filter_entries(
    entries: dict[str, HostEntry], pattern: str | None
) -> list[HostEntry]:
    items = list(entries.values())
    if pattern:
        items = [
            e for e in items if fnmatch.fnmatch(e.alias, pattern) or pattern in e.alias
        ]
    return sorted(items, key=lambda e: e.alias)


def entry_for(alias: str, *, include_unmanaged: bool = False) -> HostEntry:
    validate_alias(alias)
    entries = all_entries(include_unmanaged=include_unmanaged)
    if alias not in entries:
        raise click.ClickException(f"Unknown host alias: {alias}")
    return entries[alias]


def ssh_target(entry: HostEntry) -> str:
    return entry.alias


def ssh_base_argv(entry: HostEntry) -> list[str]:
    argv = ["ssh"]
    if entry.identity:
        argv += ["-i", os.path.expanduser(entry.identity)]
    if entry.port:
        argv += ["-p", entry.port]
    if entry.jump:
        argv += ["-J", entry.jump]
    for opt in entry.options:
        argv += ["-o", opt]
    return argv


def tcp_probe(entry: HostEntry, timeout: float) -> tuple[bool, str]:
    host = entry.hostname or entry.alias
    port = int(entry.port or "22")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp ok"
    except socket.timeout:
        return False, "tcp timeout"
    except OSError as exc:
        return False, f"tcp failed: {exc}"


def ssh_probe(entry: HostEntry, timeout: float) -> tuple[bool, str]:
    argv = ssh_base_argv(entry) + [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(timeout)}",
        ssh_target(entry),
        "true",
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=timeout + 1, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "ssh timeout"
    if result.returncode == 0:
        return True, "ssh ok"
    msg = (
        decode_output(result.stderr or result.stdout or b"ssh failed")
        .strip()
        .splitlines()
    )
    return False, msg[0] if msg else "ssh failed"


def check_one(entry: HostEntry, timeout: float) -> dict[str, Any]:
    start = time.monotonic()
    tcp_ok, tcp_msg = tcp_probe(entry, timeout)
    ssh_ok = False
    ssh_msg = "skipped"
    if tcp_ok:
        ssh_ok, ssh_msg = ssh_probe(entry, timeout)
    return {
        "alias": entry.alias,
        "target": entry.target,
        "tcp": tcp_ok,
        "ssh": ssh_ok,
        "ok": tcp_ok and ssh_ok,
        "message": ssh_msg if tcp_ok else tcp_msg,
        "duration": round(time.monotonic() - start, 3),
    }


def dump_json(data: Any) -> None:
    ui.console().out(json.dumps(data, indent=2, sort_keys=True))


def print_host_table(rows: Iterable[dict[str, Any]], *, title: str = "Hosts") -> None:
    columns = [
        ui.Column("alias", min_width=6, style="bold cyan"),
        ui.Column("target", min_width=12, ratio=1),
        ui.Column("port", hide_below=70),
        ui.Column("identity", ratio=1, hide_below=88),
        ui.Column("tags", hide_below=70),
        ui.Column("reach", hide_below=78),
    ]
    table = ui.table(*columns, title=title)
    for row in rows:
        table.add_row(*ui.row_for(columns, row))
    ui.print(table)


def truncate(text: str, raw: bool) -> str:
    if raw or len(text) <= OUTPUT_LIMIT:
        return text
    return (
        text[:OUTPUT_LIMIT]
        + f"\n… truncated; rerun with --raw for full output ({len(text)} bytes)"
    )


def decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command(
    entry: HostEntry, command: tuple[str, ...], raw: bool
) -> dict[str, Any]:
    start = time.monotonic()
    argv = ssh_base_argv(entry) + [ssh_target(entry), *command]
    try:
        result = subprocess.run(argv, capture_output=True, check=False)
        code = result.returncode
        out = decode_output(result.stdout) + decode_output(result.stderr)
    except subprocess.TimeoutExpired as exc:
        code = 124
        out = (
            decode_output(exc.stdout) + decode_output(exc.stderr) + "command timed out"
        )
    except OSError as exc:
        code = 127
        out = str(exc)
    duration = time.monotonic() - start
    return {
        "alias": entry.alias,
        "ok": code == 0,
        "exit_code": code,
        "duration": round(duration, 3),
        "output": truncate(out, raw),
    }


@click.group(context_settings=CONTEXT)
def cli() -> None:
    """Manage the usm SSH inventory."""


@cli.command("ls", context_settings=CONTEXT)
@click.argument("pattern", required=False)
@click.option(
    "--all", "include_all", is_flag=True, help="Include unmanaged Host stanzas."
)
@click.option("--check", is_flag=True, help="Probe reachability while listing.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option(
    "--timeout", default=3.0, show_default=True, type=float, help="Probe timeout."
)
def ls_cmd(
    pattern: str | None, include_all: bool, check: bool, as_json: bool, timeout: float
) -> None:
    """List managed hosts."""
    rows = []
    for entry in filter_entries(all_entries(include_all), pattern):
        probe = check_one(entry, timeout) if check else None
        rows.append(
            {
                "Alias": entry.alias,
                "Target": entry.target,
                "Port": entry.port or "-",
                "Identity": ui.shorten_path(entry.identity) if entry.identity else "-",
                "Tags": ",".join(entry.tags) or "-",
                "Reach": ("ok" if probe and probe["ok"] else "failed")
                if probe
                else "-",
                "alias": entry.alias,
                "target": entry.target,
                "port": entry.port or None,
                "identity": entry.identity or None,
                "tags": entry.tags,
                "managed": entry.managed,
                "reachable": None if probe is None else probe["ok"],
            }
        )
    if as_json:
        dump_json([{k: v for k, v in row.items() if k[:1].islower()} for row in rows])
    else:
        print_host_table(rows)


@cli.command(context_settings=CONTEXT)
@click.argument("alias")
@click.argument("target")
@click.option("--identity", type=str, help="IdentityFile path.")
@click.option("--jump", type=str, help="ProxyJump alias or target.")
@click.option(
    "--option",
    "options",
    multiple=True,
    help="Extra ssh_config option, e.g. 'Key Value'.",
)
@click.option("--forward-agent", is_flag=True, help="Enable ForwardAgent.")
@click.option("--tag", "tags", multiple=True, help="Tag for group operations.")
def add(
    alias: str,
    target: str,
    identity: str | None,
    jump: str | None,
    options: tuple[str, ...],
    forward_agent: bool,
    tags: tuple[str, ...],
) -> None:
    """Add or update a managed host."""
    validate_alias(alias)
    user, host, port = parse_target(target)
    for value, label in [(identity, "identity"), (jump, "jump")]:
        if value:
            validate_plain(value, label)
    clean_options = []
    for opt in options:
        validate_plain(opt, "option")
        if " " not in opt.strip():
            raise click.ClickException("Invalid option: use 'Key Value'.")
        clean_options.append(opt.strip())
    clean_tags = []
    for tag in tags:
        validate_plain(tag, "tag")
        if any(ch.isspace() or ch == "," for ch in tag):
            raise click.ClickException(
                "Invalid tag: whitespace and commas are not allowed."
            )
        clean_tags.append(tag)
    _, entries = read_config()
    action = "updated" if alias in entries else "added"
    entries[alias] = HostEntry(
        alias,
        host,
        user,
        port,
        identity or "",
        jump or "",
        forward_agent,
        clean_options,
        clean_tags,
    )
    write_entries(entries)
    ui.ok(f"host {action}: {alias}")


@cli.command(context_settings=CONTEXT)
@click.argument("alias")
def rm(alias: str) -> None:
    """Remove a managed host."""
    validate_alias(alias)
    content, entries = read_config()
    if alias not in entries:
        if alias in unmanaged_entries(content):
            raise click.ClickException(
                f"Refusing to remove {alias}: it is defined outside the usm host block."
            )
        raise click.ClickException(f"Unknown managed host alias: {alias}")
    del entries[alias]
    write_entries(entries)
    ui.ok(f"host removed: {alias}")


@cli.command(context_settings=CONTEXT)
@click.argument("alias")
def show(alias: str) -> None:
    """Show the effective managed config for an alias."""
    entry = entry_for(alias, include_unmanaged=True)
    ui.print(
        ui.detail(
            [
                ("alias", entry.alias),
                ("target", entry.target),
                ("managed", "yes" if entry.managed else "no"),
                ("hostname", entry.hostname or "-"),
                ("user", entry.user or "-"),
                ("port", entry.port or "-"),
                (
                    "identity",
                    ui.shorten_path(entry.identity) if entry.identity else "-",
                ),
                ("jump", entry.jump or "-"),
                ("forward agent", "yes" if entry.forward_agent else "no"),
                ("tags", ",".join(entry.tags) or "-"),
                ("options", "; ".join(entry.options) or "-"),
            ]
        )
    )


@cli.command(
    context_settings={
        **CONTEXT,
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.argument("alias")
@click.argument("ssh_args", nargs=-1, type=click.UNPROCESSED)
def connect(alias: str, ssh_args: tuple[str, ...]) -> None:
    """Exec ssh for an alias."""
    entry = entry_for(alias, include_unmanaged=True)
    extra = list(ssh_args)
    if extra and extra[0] == "--":
        extra = extra[1:]
    argv = ssh_base_argv(entry) + extra + [ssh_target(entry)]
    os.execvp("ssh", argv)


@cli.command("copy-id", context_settings=CONTEXT)
@click.argument("alias")
def copy_id(alias: str) -> None:
    """Install the default public key on a host."""
    entry = entry_for(alias, include_unmanaged=True)
    argv = ["ssh-copy-id"]
    if entry.identity:
        argv += ["-i", os.path.expanduser(entry.identity)]
    if entry.port:
        argv += ["-p", entry.port]
    if entry.jump:
        argv += ["-o", f"ProxyJump={entry.jump}"]
    argv.append(ssh_target(entry))
    if shutil.which("ssh-copy-id"):
        raise SystemExit(subprocess.call(argv))
    ui.warn("ssh-copy-id not found; falling back to a POSIX shell installer")
    pub = Path(
        os.path.expanduser(
            entry.identity + ".pub" if entry.identity else "~/.ssh/id_rsa.pub"
        )
    )
    key = pub.read_text(encoding="utf-8").strip()
    cmd = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    result = subprocess.run(
        ssh_base_argv(entry) + [ssh_target(entry), cmd],
        input=key + "\n",
        text=True,
        check=False,
    )
    raise SystemExit(result.returncode)


def selected_hosts(
    tag: str | None, include_all: bool, aliases: tuple[str, ...]
) -> list[HostEntry]:
    entries = all_entries(False)
    if tag:
        selected = sorted(
            [e for e in entries.values() if tag in e.tags], key=lambda e: e.alias
        )
    elif include_all:
        selected = sorted(entries.values(), key=lambda e: e.alias)
    elif aliases:
        selected = [entry_for(a) for a in aliases]
    else:
        raise click.ClickException("Select hosts with --tag, --all, or aliases.")
    if not selected:
        raise click.ClickException("No hosts selected.")
    return selected


@cli.command("exec", context_settings={**CONTEXT, "ignore_unknown_options": True})
@click.option("--tag", type=str, help="Run on hosts with tag.")
@click.option("--all", "include_all", is_flag=True, help="Run on all managed hosts.")
@click.option(
    "--parallel",
    default=DEFAULT_PARALLEL,
    show_default=True,
    type=click.IntRange(1, 128),
)
@click.option(
    "--fail-fast", is_flag=True, help="Stop scheduling after the first failure."
)
@click.option("--raw", is_flag=True, help="Do not truncate captured output.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def exec_cmd(
    tag: str | None,
    include_all: bool,
    parallel: int,
    fail_fast: bool,
    raw: bool,
    as_json: bool,
    args: tuple[str, ...],
) -> None:
    """Run a command on several hosts concurrently."""
    parts = list(args)
    if "--" in parts:
        idx = parts.index("--")
        aliases = tuple(parts[:idx])
        command = tuple(parts[idx + 1 :])
    elif not tag and not include_all:
        known = all_entries(False)
        split_at = 0
        for part in parts:
            if part not in known:
                break
            split_at += 1
        aliases = tuple(parts[:split_at])
        command = tuple(parts[split_at:])
    else:
        aliases = ()
        command = tuple(parts)
    if not command:
        raise click.ClickException(
            "Missing command; use: usm host exec [hosts...] -- COMMAND"
        )
    hosts = selected_hosts(tag, include_all, aliases)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        pending = {pool.submit(run_command, host, command, raw): host for host in hosts}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                pending.pop(fut)
                result = fut.result()
                results.append(result)
                if fail_fast and not result["ok"]:
                    for other in pending:
                        other.cancel()
                    pending.clear()
                    break
    results.sort(key=lambda r: r["alias"])
    if as_json:
        dump_json(results)
        raise SystemExit(1 if any(not r["ok"] for r in results) else 0)
    columns = [
        ui.Column("host", style="bold cyan"),
        ui.Column("state"),
        ui.Column("code", justify="right"),
        ui.Column("time", justify="right"),
    ]
    table = ui.table(*columns, title="Results")
    for result in results:
        table.add_row(
            result["alias"],
            "ok" if result["ok"] else "failed",
            str(result["exit_code"]),
            ui.compact_duration(result["duration"]),
        )
    ui.print(table)
    for result in results:
        if result["output"]:
            ui.title(result["alias"])
            ui.print(result["output"].rstrip())
    raise SystemExit(1 if any(not r["ok"] for r in results) else 0)


@cli.command(context_settings=CONTEXT)
@click.argument("aliases", nargs=-1)
@click.option(
    "--timeout", default=3.0, show_default=True, type=float, help="Probe timeout."
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def check(aliases: tuple[str, ...], timeout: float, as_json: bool) -> None:
    """Probe TCP and ssh reachability."""
    hosts = (
        [entry_for(a, include_unmanaged=True) for a in aliases]
        if aliases
        else list(all_entries(False).values())
    )
    rows = [check_one(host, timeout) for host in sorted(hosts, key=lambda e: e.alias)]
    if as_json:
        dump_json(rows)
        return
    columns = [
        ui.Column("host", style="bold cyan"),
        ui.Column("TCP"),
        ui.Column("SSH"),
        ui.Column("time", justify="right"),
        ui.Column("message", ratio=1, hide_below=70),
    ]
    table = ui.table(*columns, title="Reachability")
    for row in rows:
        table.add_row(
            row["alias"],
            "ok" if row["tcp"] else "failed",
            "ok" if row["ssh"] else "failed",
            ui.compact_duration(row["duration"]),
            row["message"],
        )
    ui.print(table)


if __name__ == "__main__":
    cli()
