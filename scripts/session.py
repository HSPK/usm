#!/usr/bin/env python3
"""Inspect and manage logged-in user sessions on this host.

Examples:
  usm session              # who's logged in (w-style dashboard)
  usm session ssh          # only SSH sessions
  usm session mux          # tmux / screen sessions
  usm session history kim  # recent logins for user 'kim'
  usm session kill pts/3   # end one session (privileges needed for others)
  usm session logout kim   # end all of a user's sessions
  usm session watch        # live-updating dashboard

Manage actions (kill/logout/lock/unlock) act on other users only with
sufficient privileges; run under sudo if a command reports permission denied.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

import click
import psutil
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:  # pragma: no cover - non-POSIX
    _SIGKILL = signal.SIGTERM


# Formatting helpers --------------------------------------------------------


def fmt_dur(secs: float | None) -> str:
    """Compact duration: 5s / 4m / 2h03m / 3d04h."""
    if secs is None:
        return "?"
    secs = int(secs)
    if secs < 0:
        return "0s"
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def fmt_login(epoch: float) -> str:
    if not epoch:
        return "-"
    t = time.localtime(epoch)
    now = time.localtime()
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%b%d %H:%M", t)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(argv: list[str], *, timeout: int = 10, text_in: str | None = None):
    """Run a command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, input=text_in
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# Session model -------------------------------------------------------------


@dataclass
class Sess:
    user: str
    line: str  # terminal, e.g. "pts/3"
    kind: str  # ssh | tmux | tty | local
    remote: str  # remote IP/host (ssh) or ""
    started: float
    pid: int  # foreground pid (best effort)
    idle: float | None
    what: str  # foreground command


def _classify(host: str) -> tuple[str, str]:
    """Map a utmp host field to (kind, remote)."""
    if not host or host in (":0", ":1"):
        return "tty", ""
    if host.startswith(("tmux(", "screen")):
        return "tmux", ""
    return "ssh", host


def _tty_idle(terminal: str) -> float | None:
    """Idle seconds = now - atime of the terminal device (like `w`)."""
    try:
        return max(0.0, time.time() - os.stat(f"/dev/{terminal}").st_atime)
    except OSError:
        return None


def _foreground_by_tty() -> dict[str, dict]:
    """Map ``/dev/<tty>`` -> the most recently started process on that tty.

    Approximates the WHAT column of ``w`` (the active foreground command).
    """
    out: dict[str, dict] = {}
    for p in psutil.process_iter(["pid", "name", "terminal", "cmdline", "create_time"]):
        tty = p.info["terminal"]
        if not tty:
            continue
        cur = out.get(tty)
        if cur is None or p.info["create_time"] > cur["create_time"]:
            cmd = " ".join(p.info["cmdline"] or []) or (p.info["name"] or "")
            out[tty] = {
                "pid": p.info["pid"],
                "cmd": cmd,
                "create_time": p.info["create_time"],
            }
    return out


def sessions() -> list[Sess]:
    """Current logins from utmp (psutil), enriched with idle + foreground cmd."""
    fg = _foreground_by_tty()
    out: list[Sess] = []
    for u in psutil.users():
        kind, remote = _classify(u.host or "")
        info = fg.get(f"/dev/{u.terminal}", {})
        out.append(
            Sess(
                user=u.name,
                line=u.terminal,
                kind=kind,
                remote=remote,
                started=u.started or 0.0,
                pid=info.get("pid") or (u.pid or 0),
                idle=_tty_idle(u.terminal),
                what=info.get("cmd", ""),
            )
        )
    out.sort(key=lambda s: (s.user, s.line))
    return out


def _current_tty() -> str | None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            return os.ttyname(stream.fileno()).removeprefix("/dev/")
        except (OSError, ValueError):
            continue
    return None


_KIND_STYLE = {
    "ssh": "[green]ssh[/green]",
    "tmux": "[cyan]tmux[/cyan]",
    "tty": "[dim]tty[/dim]",
    "local": "[dim]local[/dim]",
}


# Rendering -----------------------------------------------------------------


def _sessions_table(rows: list[Sess], *, current: str | None) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="dim",
        pad_edge=False,
        padding=(0, 2, 0, 0),
        expand=False,
    )
    table.add_column("", no_wrap=True)
    table.add_column("user", style="bold", no_wrap=True)
    table.add_column("tty", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("from", no_wrap=True, overflow="fold")
    table.add_column("login", no_wrap=True)
    table.add_column("idle", no_wrap=True, justify="right")
    table.add_column("pid", no_wrap=True, justify="right", style="dim")
    table.add_column("what", no_wrap=True, overflow="ellipsis", max_width=46)
    for s in rows:
        marker = "[bold green]➤[/bold green]" if s.line == current else " "
        table.add_row(
            marker,
            s.user,
            s.line,
            _KIND_STYLE.get(s.kind, s.kind),
            s.remote or "[dim]—[/dim]",
            fmt_login(s.started),
            fmt_dur(s.idle),
            str(s.pid or "—"),
            s.what or "[dim]—[/dim]",
        )
    return table


def _render_dashboard() -> None:
    rows = sessions()
    current = _current_tty()
    if not rows:
        console.print("[dim]No active logins.[/dim]")
        return
    console.print(_sessions_table(rows, current=current))
    users = sorted({s.user for s in rows})
    ssh_n = sum(1 for s in rows if s.kind == "ssh")
    console.print(
        f"\n[dim]{len(rows)} session(s) · {len(users)} user(s) · "
        f"{ssh_n} ssh · ➤ = this session[/dim]"
    )


# CLI -----------------------------------------------------------------------

COMMAND_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Inspect", ("ls", "ssh", "mux", "history", "me")),
    ("Manage", ("kill", "logout", "msg", "lock", "unlock")),
    ("Live", ("watch",)),
]


class GroupedGroup(click.Group):
    """A click group that renders its commands in labelled sections."""

    def format_commands(self, ctx: click.Context, formatter) -> None:
        listed: set[str] = set()
        for title, names in COMMAND_SECTIONS:
            rows = []
            for name in names:
                cmd = self.get_command(ctx, name)
                if cmd is None or cmd.hidden:
                    continue
                listed.add(name)
                rows.append((name, cmd.get_short_help_str(78)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        extra = [
            (n, c)
            for n in sorted(self.list_commands(ctx))
            if n not in listed
            and (c := self.get_command(ctx, n)) is not None
            and not c.hidden
        ]
        if extra:
            with formatter.section("Other"):
                formatter.write_dl([(n, c.get_short_help_str(78)) for n, c in extra])


@click.group(
    cls=GroupedGroup,
    invoke_without_command=True,
    help=__doc__.splitlines()[0],
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        _render_dashboard()
        console.print("[dim]Run [bold]usm session -h[/bold] for all commands.[/dim]")


@cli.command("ls")
def cmd_ls() -> None:
    """Show all current logins (the default view)."""
    _render_dashboard()


@cli.command("ssh")
def cmd_ssh() -> None:
    """Show only SSH sessions."""
    rows = [s for s in sessions() if s.kind == "ssh"]
    if not rows:
        console.print("[dim]No SSH sessions.[/dim]")
        return
    console.print(_sessions_table(rows, current=_current_tty()))
    console.print(f"\n[dim]{len(rows)} SSH session(s).[/dim]")


@cli.command("mux")
def cmd_mux() -> None:
    """List tmux / screen sessions on this host."""
    printed = False
    if _have("tmux"):
        rc, out, _ = _run(["tmux", "ls"])
        if rc == 0 and out.strip():
            printed = True
            console.print("[bold]tmux[/bold]")
            for line in out.splitlines():
                console.print(f"  {line}")
    if _have("screen"):
        rc, out, _ = _run(["screen", "-ls"])
        lines = [ln for ln in out.splitlines() if "\t" in ln]
        if lines:
            printed = True
            console.print("[bold]screen[/bold]")
            for line in lines:
                console.print(f"  {line.strip()}")
    if not printed:
        console.print("[dim]No tmux or screen sessions.[/dim]")


@cli.command("history")
@click.argument("user", required=False)
@click.option("-n", "--lines", default=15, show_default=True, help="Entries to show.")
@click.option("--failed", is_flag=True, help="Show failed logins (lastb; needs root).")
def cmd_history(user: str | None, lines: int, failed: bool) -> None:
    """Recent login history (last) or failed attempts (--failed)."""
    tool = "lastb" if failed else "last"
    if not _have(tool):
        raise click.ClickException(f"'{tool}' is not available on this host.")
    argv = [tool, "-F", "-n", str(lines)]
    if user and not failed:
        argv.append(user)
    rc, out, err = _run(argv, timeout=15)
    if rc != 0:
        msg = err.strip() or f"{tool} failed."
        if failed and ("permission" in msg.lower() or "denied" in msg.lower()):
            msg += "  (try: sudo usm session history --failed)"
        raise click.ClickException(msg)
    text = out.strip()
    if not text:
        console.print("[dim]No records.[/dim]")
        return
    console.print(text)


@cli.command("me")
def cmd_me() -> None:
    """Show details about your own session."""
    tty = _current_tty()
    rows = sessions()
    mine = next((s for s in rows if s.line == tty), None)
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    table.add_row("user", os.environ.get("USER") or (mine.user if mine else "?"))
    table.add_row("tty", tty or "[dim]not a tty[/dim]")
    table.add_row("pid", str(os.getpid()))
    if mine:
        table.add_row("type", mine.kind)
        table.add_row("from", mine.remote or "[dim]local[/dim]")
        table.add_row("login", fmt_login(mine.started))
        table.add_row("idle", fmt_dur(mine.idle))
    for var in ("SSH_CONNECTION", "SSH_CLIENT"):
        if os.environ.get(var):
            table.add_row(var.lower(), os.environ[var])
            break
    console.print(table)


# Manage --------------------------------------------------------------------


def _norm_tty(value: str) -> str:
    s = value.strip().removeprefix("/dev/")
    return f"pts/{s}" if s.isdigit() else s


def _pids_on_tty(tty: str) -> list[int]:
    dev = f"/dev/{tty}"
    pids = []
    for p in psutil.process_iter(["pid", "terminal"]):
        if p.info["terminal"] == dev:
            pids.append(p.info["pid"])
    return pids


def _loginctl_session_for_tty(tty: str) -> str | None:
    if not _have("loginctl"):
        return None
    rc, out, _ = _run(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[4] == tty:
            return parts[0]
    return None


def _confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    if not click.confirm(prompt, default=False):
        raise click.Abort()


@cli.command("kill")
@click.argument("tty")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", "-f", is_flag=True, help="Allow killing your own session.")
def cmd_kill(tty: str, yes: bool, force: bool) -> None:
    """End a single session identified by its TTY (e.g. pts/3)."""
    tty = _norm_tty(tty)
    if tty == _current_tty() and not force:
        raise click.ClickException(
            "refusing to kill your own session; pass --force to override."
        )
    sid = _loginctl_session_for_tty(tty)
    if sid:
        _confirm(f"Terminate session {sid} on {tty}?", yes)
        rc, _out, err = _run(["loginctl", "terminate-session", sid])
        if rc != 0:
            raise click.ClickException(
                (err.strip() or "terminate-session failed.")
                + "  (try: sudo usm session kill ...)"
            )
        console.print(f"[green]✓[/green] terminated session [bold]{sid}[/bold] ({tty})")
        return

    pids = _pids_on_tty(tty)
    if not pids:
        raise click.ClickException(f"no processes found on {tty}.")
    _confirm(f"Send SIGHUP to {len(pids)} process(es) on {tty}?", yes)
    _signal_pids(pids)
    console.print(f"[green]✓[/green] signalled {len(pids)} process(es) on {tty}")


def _signal_pids(pids: list[int]) -> None:
    denied = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGHUP)
        except PermissionError:
            denied = True
        except ProcessLookupError:
            continue
    time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, _SIGKILL)
        except PermissionError:
            denied = True
        except ProcessLookupError:
            continue
    if denied:
        console.print(
            "[yellow]note:[/yellow] some processes were not owned by you — "
            "rerun under sudo to terminate them."
        )


@cli.command("logout")
@click.argument("user")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def cmd_logout(user: str, yes: bool) -> None:
    """End all sessions belonging to a user."""
    mine = sessions()
    targets = [s for s in mine if s.user == user]
    if not targets:
        raise click.ClickException(f"no active sessions for user '{user}'.")
    _confirm(f"End all {len(targets)} session(s) for '{user}'?", yes)
    if _have("loginctl"):
        rc, _out, err = _run(["loginctl", "terminate-user", user])
        if rc == 0:
            console.print(
                f"[green]✓[/green] terminated all sessions for [bold]{user}[/bold]"
            )
            return
        console.print(
            f"[yellow]loginctl:[/yellow] {err.strip()} — falling back to signals."
        )
    pids: list[int] = []
    for s in targets:
        pids.extend(_pids_on_tty(s.line))
    _signal_pids(sorted(set(pids)))
    console.print(f"[green]✓[/green] signalled sessions for [bold]{user}[/bold]")


@cli.command("msg")
@click.argument("target")
@click.argument("text", nargs=-1, required=True)
def cmd_msg(target: str, text: tuple[str, ...]) -> None:
    """Send a message to a session (TARGET = 'all', a user, or a TTY)."""
    body = " ".join(text)
    if target == "all":
        if not _have("wall"):
            raise click.ClickException("'wall' is not available.")
        rc, _out, err = _run(["wall"], text_in=body)
        if rc != 0:
            raise click.ClickException(err.strip() or "wall failed.")
        console.print("[green]✓[/green] broadcast to all terminals")
        return
    if not _have("write"):
        raise click.ClickException("'write' is not available.")
    argv = ["write", target]
    rc, _out, err = _run(argv, text_in=body + "\n")
    if rc != 0:
        raise click.ClickException(
            (err.strip() or "write failed.") + "  (target must be a logged-in user)"
        )
    console.print(f"[green]✓[/green] message sent to [bold]{target}[/bold]")


@cli.command("lock")
@click.argument("user")
def cmd_lock(user: str) -> None:
    """Lock a user account (passwd -l; needs root)."""
    _passwd_toggle(user, lock=True)


@cli.command("unlock")
@click.argument("user")
def cmd_unlock(user: str) -> None:
    """Unlock a user account (passwd -u; needs root)."""
    _passwd_toggle(user, lock=False)


def _passwd_toggle(user: str, *, lock: bool) -> None:
    if not _have("passwd"):
        raise click.ClickException("'passwd' is not available.")
    flag = "-l" if lock else "-u"
    rc, _out, err = _run(["passwd", flag, user])
    if rc != 0:
        msg = err.strip() or "passwd failed."
        if "permission" in msg.lower() or os.geteuid() != 0:
            verb = "lock" if lock else "unlock"
            msg += f"  (try: sudo usm session {verb} {user})"
        raise click.ClickException(msg)
    console.print(
        f"[green]✓[/green] {'locked' if lock else 'unlocked'} account [bold]{user}[/bold]"
    )


@cli.command("watch")
@click.option(
    "--interval", "-i", default=2.0, show_default=True, help="Refresh seconds."
)
def cmd_watch(interval: float) -> None:
    """Live-updating session dashboard (Ctrl-C to stop)."""

    def render() -> Table:
        rows = sessions()
        current = _current_tty()
        if not rows:
            t = Table(box=None)
            t.add_column("")
            t.add_row("[dim]No active logins.[/dim]")
            return t
        return _sessions_table(rows, current=current)

    try:
        with Live(render(), console=console, screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(max(0.5, interval))
                live.update(render())
    except KeyboardInterrupt:
        pass


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
