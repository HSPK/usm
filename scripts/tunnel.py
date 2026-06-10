#!/usr/bin/env python3
"""Easier SSH tunnels — wraps ssh -L / -R / -D with persistent state.

Examples
--------
  usm tunnel local 8080:db.internal:5432 user@bastion
  usm tunnel local 8080 user@server                 # localhost:8080 -> server:8080
  usm tunnel local 8080:3000 user@server            # localhost:8080 -> server:3000
  usm tunnel remote 8080:3000 user@server           # server:8080 -> localhost:3000
  usm tunnel socks 1080 user@gateway                # SOCKS5 on localhost:1080
  usm tunnel ls
  usm tunnel stop local-8080-bastion                # keeps definition; start later
  usm tunnel start local-8080-bastion               # relaunch a stopped tunnel
  usm tunnel restart local-8080-bastion
  usm tunnel enable local-8080-bastion              # autostart at boot via systemd
  usm tunnel disable local-8080-bastion
  usm tunnel rm local-8080-bastion                  # delete the definition
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

STATE_DIR = Path.home() / ".cache" / "usm" / "tunnels"
LOG_DIR = STATE_DIR / "logs"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PREFIX = "usm-tunnel-"

KIND_FLAG = {"local": "-L", "remote": "-R", "socks": "-D"}
DEFAULT_SSH_OPTS = (
    "ExitOnForwardFailure=yes",
    "ServerAliveInterval=30",
    "ServerAliveCountMax=3",
    "StrictHostKeyChecking=accept-new",
)

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM

console = Console()


# Data model ---------------------------------------------------------------


@dataclass
class Tunnel:
    id: str
    kind: str
    bind_addr: str
    listen_port: int
    ssh_target: str
    target_host: Optional[str] = None
    target_port: Optional[int] = None
    ssh_port: Optional[int] = None
    identity: Optional[str] = None
    jumphost: Optional[str] = None
    extra_opts: list[str] = field(default_factory=list)
    pid: Optional[int] = None
    started_at: Optional[float] = None

    def spec_str(self) -> str:
        if self.kind == "socks":
            return f"{self.bind_addr}:{self.listen_port}"
        return (
            f"{self.bind_addr}:{self.listen_port}:{self.target_host}:{self.target_port}"
        )

    def route(self) -> str:
        if self.kind == "local":
            return (
                f"{self.bind_addr}:{self.listen_port} → {self.ssh_target} → "
                f"{self.target_host}:{self.target_port}"
            )
        if self.kind == "remote":
            return (
                f"{self.ssh_target}:{self.listen_port} → here → "
                f"{self.target_host}:{self.target_port}"
            )
        return f"SOCKS5 {self.bind_addr}:{self.listen_port} via {self.ssh_target}"

    def state_path(self) -> Path:
        return STATE_DIR / f"{self.id}.json"

    def log_path(self) -> Path:
        return LOG_DIR / f"{self.id}.log"

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps(asdict(self), indent=2))

    def alive(self) -> bool:
        if _is_enabled(self.id):
            return _systemd_is_active(self.id)
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True


# Helpers ------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower() or "host"


def _make_id(kind: str, port: int, ssh_target: str, custom: str | None) -> str:
    if custom:
        return _slug(custom)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    used = set()
    for p in STATE_DIR.glob("*.json"):
        try:
            used.add(int(p.stem))
        except ValueError:
            continue
    n = 0
    while n in used:
        n += 1
    return str(n)


def _parse_spec(spec: str, kind: str) -> dict:
    parts = spec.split(":")
    try:
        if kind == "socks":
            if len(parts) == 1:
                return {"bind_addr": "127.0.0.1", "listen_port": int(parts[0])}
            if len(parts) == 2:
                return {"bind_addr": parts[0], "listen_port": int(parts[1])}
            raise ValueError
        if len(parts) == 1:
            p = int(parts[0])
            return {
                "bind_addr": "127.0.0.1",
                "listen_port": p,
                "target_host": "localhost",
                "target_port": p,
            }
        if len(parts) == 2:
            return {
                "bind_addr": "127.0.0.1",
                "listen_port": int(parts[0]),
                "target_host": "localhost",
                "target_port": int(parts[1]),
            }
        if len(parts) == 3:
            return {
                "bind_addr": "127.0.0.1",
                "listen_port": int(parts[0]),
                "target_host": parts[1],
                "target_port": int(parts[2]),
            }
        if len(parts) == 4:
            return {
                "bind_addr": parts[0],
                "listen_port": int(parts[1]),
                "target_host": parts[2],
                "target_port": int(parts[3]),
            }
    except ValueError:
        pass
    raise click.BadParameter(
        f"Invalid {kind} spec: {spec!r}. "
        + (
            "Use PORT or BIND:PORT."
            if kind == "socks"
            else "Use PORT, LPORT:RPORT, LPORT:RHOST:RPORT, or BIND:LPORT:RHOST:RPORT."
        )
    )


def _build_argv(t: Tunnel) -> list[str]:
    argv = ["ssh", "-N", "-T"]
    for opt in DEFAULT_SSH_OPTS:
        argv += ["-o", opt]
    if t.identity:
        argv += ["-i", t.identity]
    if t.ssh_port:
        argv += ["-p", str(t.ssh_port)]
    if t.jumphost:
        argv += ["-J", t.jumphost]
    for opt in t.extra_opts:
        argv += ["-o", opt]
    argv += [KIND_FLAG[t.kind], t.spec_str(), t.ssh_target]
    return argv


# systemd user-unit helpers ------------------------------------------------


def _unit_name(tid: str) -> str:
    return f"{UNIT_PREFIX}{tid}.service"


def _unit_path(tid: str) -> Path:
    return SYSTEMD_USER_DIR / _unit_name(tid)


def _is_enabled(tid: str) -> bool:
    return _unit_path(tid).exists()


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _require_systemd() -> None:
    if os.name != "posix" or not shutil.which("systemctl"):
        raise click.ClickException(
            "Autostart needs systemd (user instance). Not available on this system."
        )
    p = _systemctl("--version")
    if p.returncode != 0:
        raise click.ClickException(
            f"systemctl --user not usable: {p.stderr.strip() or p.stdout.strip()}"
        )


def _systemd_is_active(tid: str) -> bool:
    p = _systemctl("is-active", _unit_name(tid))
    return p.stdout.strip() == "active"


def _systemd_main_pid(tid: str) -> Optional[int]:
    p = _systemctl("show", "-p", "MainPID", "--value", _unit_name(tid))
    try:
        pid = int(p.stdout.strip())
    except ValueError:
        return None
    return pid or None


def _linger_enabled() -> bool:
    if not shutil.which("loginctl"):
        return False
    try:
        out = subprocess.check_output(
            ["loginctl", "show-user", _current_user(), "-p", "Linger", "--value"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return False
    return out.lower() == "yes"


def _current_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name


def _render_unit(t: Tunnel, usm_bin: str) -> str:
    uv_bin = shutil.which("uv")
    extra_paths = [os.path.dirname(usm_bin)]
    if uv_bin:
        extra_paths.append(os.path.dirname(uv_bin))
    path_value = ":".join(
        dict.fromkeys(
            extra_paths
            + [
                f"{Path.home()}/.local/bin",
                f"{Path.home()}/.cargo/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            ]
        )
    )
    return (
        "[Unit]\n"
        f"Description=usm SSH tunnel {t.id}: {t.route()}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="PATH={path_value}"\n'
        f"ExecStart={usm_bin} tunnel up {t.id}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _tunnel_from_raw(raw: dict) -> Tunnel:
    allowed = {f.name for f in fields(Tunnel)}
    return Tunnel(**{k: v for k, v in raw.items() if k in allowed})


def _load_all() -> list[Tunnel]:
    if not STATE_DIR.exists():
        return []
    out: list[Tunnel] = []
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            out.append(_tunnel_from_raw(json.loads(path.read_text())))
        except (json.JSONDecodeError, TypeError, OSError):
            continue
    return out


def _load(tid: str) -> Tunnel:
    path = STATE_DIR / f"{tid}.json"
    if not path.exists():
        raise click.ClickException(f"No tunnel with id '{tid}'.")
    return _tunnel_from_raw(json.loads(path.read_text()))


def _delete(t: Tunnel) -> None:
    for p in (t.state_path(), t.log_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _start(t: Tunnel, *, new: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    argv = _build_argv(t)
    log = open(t.log_path(), "ab", buffering=0)
    log.write(f"\n--- start {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
    log.write(("$ " + " ".join(argv) + "\n").encode())

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"{argv[0]} not found on PATH. Install it first."
        ) from exc

    t.pid = proc.pid
    t.started_at = time.time()
    t.save()

    time.sleep(1.5)
    if proc.poll() is not None:
        tail = _tail(t.log_path(), 12)
        if new:
            _delete(t)
        else:
            t.pid = None
            t.started_at = None
            t.save()
        console.print(
            f"[red]✗[/red] ssh exited immediately (code {proc.returncode}). Recent log:"
        )
        for line in tail:
            console.print(f"  [dim]{line}[/dim]")
        raise click.ClickException("Tunnel failed to start.")

    console.print(f"[green]✓[/green] Started tunnel [bold]{t.id}[/bold] (pid {t.pid})")
    console.print(f"  {t.route()}")


def _kill_pid(t: Tunnel) -> bool:
    """SIGTERM then SIGKILL the recorded pid. Returns True if it was alive."""
    if not t.pid:
        return False
    try:
        os.kill(t.pid, 0)
    except OSError:
        return False
    try:
        os.kill(t.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(t.pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(t.pid, _SIGKILL)
    except OSError:
        pass
    return True


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _make_tunnel(kind: str, spec: str, ssh_target: str, opts: dict) -> Tunnel:
    parsed = _parse_spec(spec, kind)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tid = _make_id(kind, parsed["listen_port"], ssh_target, opts.get("name"))
    if (STATE_DIR / f"{tid}.json").exists():
        raise click.ClickException(
            f"A tunnel with id '{tid}' already exists. "
            f"Use 'usm tunnel start {tid}' to relaunch, "
            f"'usm tunnel rm {tid}' to delete, or pass --name to pick a new id."
        )
    return Tunnel(
        id=tid,
        kind=kind,
        ssh_target=ssh_target,
        identity=opts.get("identity"),
        ssh_port=opts.get("ssh_port"),
        jumphost=opts.get("jumphost"),
        extra_opts=list(opts.get("ssh_opts") or ()),
        **parsed,
    )


# CLI ----------------------------------------------------------------------


def _common_opts(f):
    f = click.option("--name", help="Custom tunnel id (default: next free integer).")(f)
    f = click.option(
        "-i",
        "--identity",
        type=click.Path(),
        help="SSH identity file (-i).",
    )(f)
    f = click.option("-p", "--ssh-port", type=int, help="SSH port (default 22).")(f)
    f = click.option("-J", "--jumphost", help="SSH jump host (-J).")(f)
    f = click.option(
        "-o",
        "--ssh-opt",
        "ssh_opts",
        multiple=True,
        help="Extra -o option (repeatable), e.g. -o ConnectTimeout=10.",
    )(f)
    return f


@click.group(
    help="Easier SSH tunnels — wraps ssh -L / -R / -D with persistent state.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def cli() -> None:
    pass


@cli.command(
    "local",
    short_help="Forward localhost → remote (ssh -L).",
    help="""Local port forwarding (ssh -L).

\b
SPEC forms:
  PORT                          localhost:PORT -> SSH_TARGET:PORT
  LPORT:RPORT                   localhost:LPORT -> SSH_TARGET:RPORT
  LPORT:RHOST:RPORT             localhost:LPORT -> RHOST:RPORT (resolved by SSH_TARGET)
  BIND:LPORT:RHOST:RPORT        BIND:LPORT -> RHOST:RPORT
""",
)
@click.argument("spec")
@click.argument("ssh_target")
@_common_opts
def cmd_local(spec, ssh_target, **opts):
    _start(_make_tunnel("local", spec, ssh_target, opts), new=True)


@cli.command(
    "remote",
    short_help="Expose local → remote (ssh -R).",
    help="""Remote port forwarding (ssh -R).

\b
SPEC forms:
  PORT                          SSH_TARGET:PORT -> here:PORT
  RPORT:LPORT                   SSH_TARGET:RPORT -> here:LPORT
  RPORT:LHOST:LPORT             SSH_TARGET:RPORT -> LHOST:LPORT (from here)
  BIND:RPORT:LHOST:LPORT        SSH_TARGET BIND:RPORT -> LHOST:LPORT
""",
)
@click.argument("spec")
@click.argument("ssh_target")
@_common_opts
def cmd_remote(spec, ssh_target, **opts):
    _start(_make_tunnel("remote", spec, ssh_target, opts), new=True)


@cli.command(
    "socks",
    short_help="SOCKS5 proxy (ssh -D).",
    help="SOCKS5 dynamic forwarding. SPEC is PORT or BIND:PORT.",
)
@click.argument("spec")
@click.argument("ssh_target")
@_common_opts
def cmd_socks(spec, ssh_target, **opts):
    _start(_make_tunnel("socks", spec, ssh_target, opts), new=True)


@cli.command("ls", short_help="List tunnels.")
@click.option(
    "--prune",
    is_flag=True,
    help="Delete state files for definitions that are stopped and not enabled.",
)
def cmd_ls(prune):
    tunnels = _load_all()
    if prune:
        gone = [t for t in tunnels if not t.alive() and not _is_enabled(t.id)]
        for t in gone:
            _delete(t)
        tunnels = [t for t in tunnels if t not in gone]
        if gone:
            console.print(f"[dim]Pruned {len(gone)} stopped tunnel(s).[/dim]")
    if not tunnels:
        console.print("[dim]No tunnels recorded.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Kind")
    table.add_column("Route")
    table.add_column("PID", justify="right")
    table.add_column("Up", justify="right")
    table.add_column("Status")
    table.add_column("Boot")
    for t in tunnels:
        enabled = _is_enabled(t.id)
        if enabled:
            pid = _systemd_main_pid(t.id)
            alive = bool(pid) and _systemd_is_active(t.id)
        else:
            pid = t.pid if (t.pid and t.alive()) else None
            alive = bool(pid)
        up = _fmt_uptime(time.time() - t.started_at) if alive and t.started_at else "-"
        status = "[green]running[/green]" if alive else "[dim]stopped[/dim]"
        boot = "[cyan]enabled[/cyan]" if enabled else "[dim]-[/dim]"
        table.add_row(t.id, t.kind, t.route(), str(pid or "-"), up, status, boot)
    console.print(table)


@cli.command("stop", short_help="Stop tunnel (keeps definition).")
@click.argument("target")
def cmd_stop(target):
    tunnels = _load_all() if target == "all" else [_load(target)]
    if not tunnels:
        console.print("[dim]Nothing to stop.[/dim]")
        return
    for t in tunnels:
        if _is_enabled(t.id):
            p = _systemctl("stop", _unit_name(t.id))
            if p.returncode == 0:
                console.print(f"[green]✓[/green] {t.id}: stopped (systemd)")
            else:
                console.print(
                    f"[red]✗[/red] {t.id}: {p.stderr.strip() or 'systemctl stop failed'}"
                )
            continue
        was_alive = _kill_pid(t)
        t.pid = None
        t.started_at = None
        t.save()
        console.print(
            f"[green]✓[/green] {t.id}: {'stopped' if was_alive else 'already stopped'}"
        )


@cli.command("start", short_help="Start a stopped tunnel by id.")
@click.argument("tid")
def cmd_start(tid):
    t = _load(tid)
    if _is_enabled(tid):
        p = _systemctl("start", _unit_name(tid))
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl start failed.")
        console.print(f"[green]✓[/green] Started {tid} via systemd.")
        return
    if t.alive():
        raise click.ClickException(f"{tid} is already running (pid {t.pid}).")
    _start(t)


@cli.command("restart", short_help="Restart a tunnel by id.")
@click.argument("tid")
def cmd_restart(tid):
    t = _load(tid)
    if _is_enabled(tid):
        p = _systemctl("restart", _unit_name(tid))
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl restart failed.")
        console.print(f"[green]✓[/green] Restarted {tid} via systemd.")
        return
    _kill_pid(t)
    t.pid = None
    t.started_at = None
    _start(t)


@cli.command("rm", short_help="Delete tunnel definition.")
@click.argument("target")
def cmd_rm(target):
    tunnels = _load_all() if target == "all" else [_load(target)]
    if not tunnels:
        console.print("[dim]Nothing to remove.[/dim]")
        return
    for t in tunnels:
        if _is_enabled(t.id):
            _systemctl("disable", "--now", _unit_name(t.id))
            _unit_path(t.id).unlink(missing_ok=True)
            _systemctl("daemon-reload")
        _kill_pid(t)
        _delete(t)
        console.print(f"[green]✓[/green] removed {t.id}")


@cli.command(
    "enable",
    short_help="Install systemd user unit so the tunnel autostarts.",
    help=(
        "Install a systemd --user unit for this tunnel so it starts at login "
        "(and at boot if `loginctl enable-linger` is set), and auto-restarts "
        "on failure. Replaces any standalone process."
    ),
)
@click.argument("tid")
def cmd_enable(tid):
    t = _load(tid)
    _require_systemd()
    usm_bin = shutil.which("usm")
    if not usm_bin:
        raise click.ClickException(
            "'usm' not found on PATH; install it (e.g. `uv tool install usmo`) first."
        )
    _kill_pid(t)
    t.pid = None
    t.started_at = time.time()
    t.save()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    _unit_path(tid).write_text(_render_unit(t, usm_bin))
    _systemctl("daemon-reload", check=True)
    p = _systemctl("enable", "--now", _unit_name(tid))
    if p.returncode != 0:
        raise click.ClickException(p.stderr.strip() or "systemctl enable --now failed.")
    console.print(
        f"[green]✓[/green] Enabled & started [bold]{tid}[/bold] "
        f"({_unit_path(tid).name})."
    )
    if not _linger_enabled():
        console.print(
            "  [yellow]note:[/yellow] to start at boot without logging in, run "
            f"[bold]sudo loginctl enable-linger {_current_user()}[/bold]"
        )


@cli.command(
    "disable",
    short_help="Remove the systemd user unit (keeps definition).",
)
@click.argument("tid")
def cmd_disable(tid):
    _load(tid)
    if not _is_enabled(tid):
        console.print(f"[dim]{tid} is not enabled.[/dim]")
        return
    _require_systemd()
    _systemctl("disable", "--now", _unit_name(tid))
    _unit_path(tid).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    console.print(f"[green]✓[/green] Disabled {tid}.")


@cli.command(
    "up",
    hidden=True,
    short_help="(internal) exec ssh in foreground for systemd.",
)
@click.argument("tid")
def cmd_up(tid):
    t = _load(tid)
    argv = _build_argv(t)
    t.pid = os.getpid()
    t.started_at = time.time()
    t.save()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError as exc:
        raise click.ClickException(f"{argv[0]} not found on PATH.") from exc


@cli.command("show", short_help="Show details of a tunnel.")
@click.argument("tid")
def cmd_show(tid):
    t = _load(tid)
    data = asdict(t)
    data["alive"] = t.alive()
    data["enabled"] = _is_enabled(tid)
    data["unit_path"] = str(_unit_path(tid)) if _is_enabled(tid) else None
    data["argv"] = _build_argv(t)
    console.print_json(json.dumps(data))


@cli.command("logs", short_help="Print log file of a tunnel.")
@click.argument("tid")
@click.option("-n", "--lines", type=int, default=50, show_default=True)
def cmd_logs(tid, lines):
    t = _load(tid)
    log = t.log_path()
    if not log.exists():
        console.print(f"[dim]No logs for {tid}.[/dim]")
        return
    for line in _tail(log, lines):
        click.echo(line)


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
