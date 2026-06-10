#!/usr/bin/env python3
"""Show what's listening on which port and kill processes by port.

Examples:
  usm port             # all LISTEN sockets
  usm port 8080        # who's on 8080 (pid + cmd + user)
  usm port kill 8080   # SIGTERM (then SIGKILL after 3s) anyone bound to 8080
"""

from __future__ import annotations

import os
import signal
import time

import click
import psutil
from rich.console import Console
from rich.table import Table

console = Console()

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM


def _listeners(
    port: int | None = None,
) -> list[tuple[psutil.Process | None, str, int, str]]:
    """Return (proc, laddr_ip, laddr_port, status) for matching LISTEN sockets."""
    rows: list[tuple[psutil.Process | None, str, int, str]] = []
    # Pairs of (pid_or_None, conn). System-wide call gives us conn.pid; the
    # per-process fallback yields the process we got the conn from.
    pairs: list[tuple[int | None, "psutil._common.sconn"]] = []
    try:
        for c in psutil.net_connections(kind="inet"):
            pairs.append((c.pid, c))
    except (psutil.AccessDenied, PermissionError):
        console.print(
            "[yellow]note:[/yellow] some sockets hidden — try with sudo for full visibility."
        )
        for proc in psutil.process_iter(["pid"]):
            try:
                for c in proc.net_connections(kind="inet"):
                    pairs.append((proc.pid, c))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    for owner_pid, c in pairs:
        if c.status != psutil.CONN_LISTEN:
            continue
        if not c.laddr:
            continue
        if port is not None and c.laddr.port != port:
            continue
        proc = None
        if owner_pid:
            try:
                proc = psutil.Process(owner_pid)
            except psutil.NoSuchProcess:
                pass
        rows.append((proc, c.laddr.ip, c.laddr.port, c.status))
    rows.sort(key=lambda r: (r[2], r[1]))
    return rows


def _proc_info(p: psutil.Process | None) -> tuple[str, str, str]:
    if p is None:
        return ("-", "-", "-")
    try:
        cmd = " ".join(p.cmdline()) or p.name()
        return (str(p.pid), p.username(), cmd)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ("?", "?", "?")


def _render(rows, title: str):
    if not rows:
        console.print("[dim]No matching LISTEN socket.[/dim]")
        return
    table = Table(title=title, show_lines=False, header_style="bold")
    table.add_column("PID", justify="right")
    table.add_column("USER")
    table.add_column("ADDR")
    table.add_column("PORT", justify="right")
    table.add_column("COMMAND", overflow="fold")
    for proc, ip, port, _ in rows:
        pid, user, cmd = _proc_info(proc)
        table.add_row(pid, user, ip, str(port), cmd)
    console.print(table)


class _PortGroup(click.Group):
    """Allow `usm port 8080` to dispatch to the `show` subcommand."""

    def resolve_command(self, ctx, args):
        if args and args[0].isdigit():
            show = self.get_command(ctx, "show")
            assert show is not None
            return "show", show, args
        return super().resolve_command(ctx, args)


@click.group(
    cls=_PortGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Inspect or free TCP/UDP ports.",
)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _render(_listeners(), title="All LISTEN sockets")


@cli.command("show", help="Show LISTEN socket(s) on PORT.")
@click.argument("port", type=int)
def cmd_show(port):
    _render(_listeners(port), title=f"Port {port}")


@cli.command("ls", help="List every LISTEN socket.")
def cmd_ls():
    _render(_listeners(), title="All LISTEN sockets")


@cli.command("kill", help="Kill every process bound to PORT.")
@click.argument("port", type=int)
@click.option("--force", is_flag=True, help="Send SIGKILL immediately.")
def cmd_kill(port, force):
    rows = _listeners(port)
    if not rows:
        console.print(f"[dim]nothing listening on {port}.[/dim]")
        return
    killed: list[int] = []
    sig = _SIGKILL if force else signal.SIGTERM
    for proc, _, _, _ in rows:
        if proc is None:
            continue
        try:
            os.kill(proc.pid, sig)
            killed.append(proc.pid)
            console.print(f"[green]✓[/green] sent {sig.name} to pid {proc.pid}")
        except OSError as e:
            console.print(f"[red]✗[/red] pid {proc.pid}: {e}")
    if force or not killed:
        return
    deadline = time.time() + 3
    while time.time() < deadline:
        if not _listeners(port):
            return
        time.sleep(0.1)
    for pid in killed:
        try:
            os.kill(pid, _SIGKILL)
            console.print(f"[yellow]…[/yellow] escalated to SIGKILL on pid {pid}")
        except OSError:
            pass


if __name__ == "__main__":
    cli()
