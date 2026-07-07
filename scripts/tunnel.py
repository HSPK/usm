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
  usm tunnel enable local-8080-bastion              # autostart via launchd/systemd
  usm tunnel disable local-8080-bastion
  usm tunnel rm local-8080-bastion                  # delete the definition
"""

from __future__ import annotations

import json
import os
import plistlib
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
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"
UNIT_PREFIX = "usm-tunnel-"
LABEL_PREFIX = "com.github.hspk.usm.tunnel."
SUPERVISE_ENV = "USM_TUNNEL_SUPERVISE_ID"
STARTUP_GRACE_SECS = 1.5
RESTART_DELAY_SECS = 5.0

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
    supervisor_pid: Optional[int] = None
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
            return _service_is_active(self.id)
        managed_pid = self.supervisor_pid or self.pid
        if not managed_pid:
            return False
        return _pid_alive(managed_pid)


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# service helpers ----------------------------------------------------------


def _unit_name(tid: str) -> str:
    return f"{UNIT_PREFIX}{tid}.service"


def _unit_path(tid: str) -> Path:
    return SYSTEMD_USER_DIR / _unit_name(tid)


def _launchd_label(tid: str) -> str:
    return f"{LABEL_PREFIX}{tid}"


def _launchd_path(tid: str) -> Path:
    return LAUNCHD_USER_DIR / f"{_launchd_label(tid)}.plist"


def _enabled_kind(tid: str) -> str | None:
    if sys.platform == "darwin" and _launchd_path(tid).exists():
        return "launchd"
    if sys.platform != "darwin" and _unit_path(tid).exists():
        return "systemd"
    return None


def _is_enabled(tid: str) -> bool:
    return _enabled_kind(tid) is not None


def _default_service_kind() -> str:
    return "launchd" if sys.platform == "darwin" else "systemd"


def _path_value(usm_bin: str) -> str:
    uv_bin = shutil.which("uv")
    extra_paths = [os.path.dirname(usm_bin)]
    if uv_bin:
        extra_paths.append(os.path.dirname(uv_bin))
    return ":".join(
        dict.fromkeys(
            extra_paths
            + [
                f"{Path.home()}/.local/bin",
                f"{Path.home()}/.cargo/bin",
                "/opt/homebrew/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            ]
        )
    )


def _require_service_backend(kind: str) -> None:
    if kind == "launchd":
        _require_launchd()
        return
    _require_systemd()


def _service_is_active(tid: str) -> bool:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_is_active(tid)
    if kind == "systemd":
        return _systemd_is_active(tid)
    return False


def _service_main_pid(tid: str) -> Optional[int]:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_main_pid(tid)
    if kind == "systemd":
        return _systemd_main_pid(tid)
    return None


def _service_start(tid: str) -> subprocess.CompletedProcess:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_start(tid)
    if kind == "systemd":
        return _systemctl("start", _unit_name(tid))
    raise click.ClickException(f"{tid} is not enabled.")


def _service_stop(tid: str) -> subprocess.CompletedProcess:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_bootout(tid, missing_ok=True)
    if kind == "systemd":
        return _systemctl("stop", _unit_name(tid))
    raise click.ClickException(f"{tid} is not enabled.")


def _service_restart(tid: str) -> subprocess.CompletedProcess:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        stopped = _launchd_bootout(tid, missing_ok=True)
        if stopped.returncode != 0:
            return stopped
        return _launchd_bootstrap(tid)
    if kind == "systemd":
        return _systemctl("restart", _unit_name(tid))
    raise click.ClickException(f"{tid} is not enabled.")


def _service_disable(tid: str) -> None:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        _launchd_bootout(tid, missing_ok=True)
        _launchd_path(tid).unlink(missing_ok=True)
        return
    if kind == "systemd":
        _systemctl("disable", "--now", _unit_name(tid))
        _unit_path(tid).unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return


def _service_target_name(tid: str) -> str:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_label(tid)
    if kind == "systemd":
        return _unit_name(tid)
    return "-"


def _service_path(tid: str) -> Path | None:
    kind = _enabled_kind(tid)
    if kind == "launchd":
        return _launchd_path(tid)
    if kind == "systemd":
        return _unit_path(tid)
    return None


# systemd user-unit helpers ------------------------------------------------


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
    path_value = _path_value(usm_bin)
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
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# launchd user-agent helpers -----------------------------------------------


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_service(tid: str) -> str:
    return f"{_launchd_domain()}/{_launchd_label(tid)}"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _require_launchd() -> None:
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise click.ClickException(
            "Autostart needs launchd on macOS or systemd on Linux. "
            "No supported service manager is available on this system."
        )


def _launchd_bootstrap(tid: str) -> subprocess.CompletedProcess:
    p = _launchctl("bootstrap", _launchd_domain(), str(_launchd_path(tid)))
    if p.returncode != 0 and _launchd_is_loaded(tid):
        return subprocess.CompletedProcess(p.args, 0, p.stdout, p.stderr)
    return p


def _launchd_start(tid: str) -> subprocess.CompletedProcess:
    if _launchd_is_loaded(tid):
        if _launchd_is_active(tid):
            return subprocess.CompletedProcess(
                ["launchctl", "print", _launchd_service(tid)], 0, "", ""
            )
        return _launchctl("kickstart", "-k", _launchd_service(tid))
    return _launchd_bootstrap(tid)


def _launchd_bootout(
    tid: str, *, missing_ok: bool = False
) -> subprocess.CompletedProcess:
    p = _launchctl("bootout", _launchd_domain(), str(_launchd_path(tid)))
    if missing_ok and p.returncode != 0 and not _launchd_is_loaded(tid):
        return subprocess.CompletedProcess(p.args, 0, p.stdout, p.stderr)
    return p


def _launchd_print(tid: str) -> subprocess.CompletedProcess:
    return _launchctl("print", _launchd_service(tid))


def _launchd_is_loaded(tid: str) -> bool:
    return _launchd_print(tid).returncode == 0


def _launchd_main_pid(tid: str) -> Optional[int]:
    p = _launchd_print(tid)
    if p.returncode != 0:
        return None
    m = re.search(r"\bpid\s*=\s*(\d+)", p.stdout)
    if not m:
        return None
    return int(m.group(1))


def _launchd_is_active(tid: str) -> bool:
    return _launchd_main_pid(tid) is not None


def _render_plist(t: Tunnel, usm_bin: str) -> bytes:
    path_value = _path_value(usm_bin)
    data = {
        "Label": _launchd_label(t.id),
        "ProgramArguments": [usm_bin, "tunnel", "up", t.id],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": int(RESTART_DELAY_SECS),
        "EnvironmentVariables": {"PATH": path_value},
        "StandardOutPath": str(t.log_path()),
        "StandardErrorPath": str(t.log_path()),
    }
    return plistlib.dumps(data, sort_keys=False)


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


def _supervisor_argv() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve())]


def _supervisor_env(tid: str) -> dict[str, str]:
    env = os.environ.copy()
    env[SUPERVISE_ENV] = tid
    return env


def _clear_runtime(t: Tunnel) -> None:
    t.pid = None
    t.supervisor_pid = None
    t.started_at = None


def _save_runtime(
    tid: str,
    *,
    pid: int | None,
    supervisor_pid: int | None,
    started_at: float | None,
) -> None:
    t = _load(tid)
    t.pid = pid
    t.supervisor_pid = supervisor_pid
    t.started_at = started_at
    t.save()


def _write_start_log(log, argv: list[str], *, label: str = "start") -> None:
    log.write(f"\n--- {label} {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
    log.write(("$ " + " ".join(argv) + "\n").encode())


def _supervise(tid: str) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stop_requested = False
    child: subprocess.Popen | None = None

    def request_stop(signum, frame) -> None:  # noqa: ARG001
        nonlocal stop_requested, child
        stop_requested = True
        if child and child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass

    if os.name == "posix":
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    first_attempt = True
    while not stop_requested:
        t = _load(tid)
        argv = _build_argv(t)
        started_at = time.time()
        with open(t.log_path(), "ab", buffering=0) as log:
            _write_start_log(log, argv, label="start" if first_attempt else "restart")
            try:
                child = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            except FileNotFoundError:
                log.write(f"{argv[0]} not found on PATH.\n".encode())
                _save_runtime(
                    tid, pid=None, supervisor_pid=None, started_at=None
                )
                return 127

            _save_runtime(
                tid,
                pid=child.pid,
                supervisor_pid=os.getpid(),
                started_at=started_at,
            )
            returncode = child.wait()
            child = None
            uptime = time.time() - started_at

            if stop_requested:
                _save_runtime(tid, pid=None, supervisor_pid=None, started_at=None)
                log.write(b"supervisor stopped.\n")
                return 0
            _save_runtime(tid, pid=None, supervisor_pid=os.getpid(), started_at=None)
            if first_attempt and uptime < STARTUP_GRACE_SECS:
                _save_runtime(tid, pid=None, supervisor_pid=None, started_at=None)
                log.write(f"ssh exited during startup (code {returncode}).\n".encode())
                return returncode or 1

            log.write(
                f"ssh exited (code {returncode}); restarting in "
                f"{RESTART_DELAY_SECS:g}s.\n".encode()
            )

        first_attempt = False
        deadline = time.time() + RESTART_DELAY_SECS
        while not stop_requested and time.time() < deadline:
            time.sleep(0.2)

    _save_runtime(tid, pid=None, supervisor_pid=None, started_at=None)
    return 0


def _start(t: Tunnel, *, new: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    argv = _supervisor_argv()
    _clear_runtime(t)
    t.save()

    log = open(t.log_path(), "ab", buffering=0)
    popen_kwargs: dict = {
        "env": _supervisor_env(t.id),
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
    finally:
        log.close()

    t.supervisor_pid = proc.pid
    t.started_at = time.time()
    t.save()

    time.sleep(STARTUP_GRACE_SECS)

    if proc.poll() is not None:
        tail = _tail(t.log_path(), 12)
        if new:
            _delete(t)
        else:
            _clear_runtime(t)
            t.save()
        console.print(
            f"[red]✗[/red] ssh exited immediately (code {proc.returncode}). Recent log:"
        )
        for line in tail:
            console.print(f"  [dim]{line}[/dim]")
        raise click.ClickException("Tunnel failed to start.")

    t = _load(t.id)
    pid_info = f"pid {t.pid}" if t.pid else f"supervisor pid {t.supervisor_pid}"
    console.print(f"[green]✓[/green] Started tunnel [bold]{t.id}[/bold] ({pid_info})")
    console.print(f"  {t.route()}")


def _kill_pid(t: Tunnel) -> bool:
    """SIGTERM then SIGKILL the recorded pid. Returns True if it was alive."""
    pid = t.supervisor_pid or t.pid
    if not pid:
        return False
    if not _pid_alive(pid):
        return False
    if os.name == "posix":
        term = lambda sig: os.killpg(pid, sig)
    else:
        term = lambda sig: os.kill(pid, sig)
    try:
        term(signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        term(_SIGKILL)
    except OSError:
        try:
            os.kill(pid, _SIGKILL)
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
        enabled_kind = _enabled_kind(t.id)
        if enabled_kind:
            pid = _service_main_pid(t.id)
            alive = bool(pid) and _service_is_active(t.id)
        else:
            pid = t.pid if (t.pid and t.alive()) else None
            if not pid and t.supervisor_pid and t.alive():
                pid = t.supervisor_pid
            alive = bool(pid)
        up = _fmt_uptime(time.time() - t.started_at) if alive and t.started_at else "-"
        status = "[green]running[/green]" if alive else "[dim]stopped[/dim]"
        boot = f"[cyan]{enabled_kind}[/cyan]" if enabled_kind else "[dim]-[/dim]"
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
            p = _service_stop(t.id)
            if p.returncode == 0:
                _clear_runtime(t)
                t.save()
                console.print(
                    f"[green]✓[/green] {t.id}: stopped "
                    f"({_service_target_name(t.id)})"
                )
            else:
                console.print(
                    f"[red]✗[/red] {t.id}: {p.stderr.strip() or 'service stop failed'}"
                )
            continue
        was_alive = _kill_pid(t)
        _clear_runtime(t)
        t.save()
        console.print(
            f"[green]✓[/green] {t.id}: {'stopped' if was_alive else 'already stopped'}"
        )


@cli.command("start", short_help="Start a stopped tunnel by id.")
@click.argument("tid")
def cmd_start(tid):
    t = _load(tid)
    if _is_enabled(tid):
        p = _service_start(tid)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "service start failed.")
        console.print(
            f"[green]✓[/green] Started {tid} via {_service_target_name(tid)}."
        )
        return
    if t.alive():
        running_pid = t.pid or t.supervisor_pid
        raise click.ClickException(f"{tid} is already running (pid {running_pid}).")
    _start(t)


@cli.command("restart", short_help="Restart a tunnel by id.")
@click.argument("tid")
def cmd_restart(tid):
    t = _load(tid)
    if _is_enabled(tid):
        p = _service_restart(tid)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "service restart failed.")
        console.print(
            f"[green]✓[/green] Restarted {tid} via {_service_target_name(tid)}."
        )
        return
    _kill_pid(t)
    _clear_runtime(t)
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
            _service_disable(t.id)
        _kill_pid(t)
        _delete(t)
        console.print(f"[green]✓[/green] removed {t.id}")


@cli.command(
    "enable",
    short_help="Install a user service so the tunnel autostarts.",
    help=(
        "Install a user service for this tunnel so it starts at login and "
        "auto-restarts when ssh exits. Uses launchd on macOS and systemd --user "
        "on Linux. Replaces any standalone process."
    ),
)
@click.argument("tid")
def cmd_enable(tid):
    t = _load(tid)
    kind = _enabled_kind(tid) or _default_service_kind()
    _require_service_backend(kind)
    usm_bin = shutil.which("usm")
    if not usm_bin:
        raise click.ClickException(
            "'usm' not found on PATH; install it (e.g. `uv tool install usmo`) first."
        )
    _kill_pid(t)
    _clear_runtime(t)
    t.started_at = time.time()
    t.save()
    if kind == "launchd":
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)
        _launchd_path(tid).write_bytes(_render_plist(t, usm_bin))
        stopped = _launchd_bootout(tid, missing_ok=True)
        if stopped.returncode != 0:
            raise click.ClickException(
                stopped.stderr.strip() or "launchctl bootout failed."
            )
        p = _launchd_bootstrap(tid)
    else:
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        _unit_path(tid).write_text(_render_unit(t, usm_bin))
        _systemctl("daemon-reload", check=True)
        p = _systemctl("enable", "--now", _unit_name(tid))
    if p.returncode != 0:
        raise click.ClickException(p.stderr.strip() or "service enable failed.")
    console.print(
        f"[green]✓[/green] Enabled & started [bold]{tid}[/bold] "
        f"({_service_target_name(tid)})."
    )
    if kind == "systemd" and not _linger_enabled():
        console.print(
            "  [yellow]note:[/yellow] to start at boot without logging in, run "
            f"[bold]sudo loginctl enable-linger {_current_user()}[/bold]"
        )


@cli.command(
    "disable",
    short_help="Remove the user service (keeps definition).",
)
@click.argument("tid")
def cmd_disable(tid):
    _load(tid)
    if not _is_enabled(tid):
        console.print(f"[dim]{tid} is not enabled.[/dim]")
        return
    kind = _enabled_kind(tid) or _default_service_kind()
    _require_service_backend(kind)
    _service_disable(tid)
    t = _load(tid)
    _clear_runtime(t)
    t.save()
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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(t.log_path(), "ab", buffering=0)
    _write_start_log(log, argv)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    t.pid = os.getpid()
    t.supervisor_pid = None
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
    enabled_kind = _enabled_kind(tid)
    service_path = _service_path(tid)
    data = asdict(t)
    data["alive"] = t.alive()
    data["enabled"] = enabled_kind is not None
    data["service_kind"] = enabled_kind
    data["service_path"] = str(service_path) if service_path else None
    data["unit_path"] = str(_unit_path(tid)) if enabled_kind == "systemd" else None
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
    if tid := os.environ.pop(SUPERVISE_ENV, None):
        sys.exit(_supervise(tid))
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
