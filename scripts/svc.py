#!/usr/bin/env python3
"""Run any command as a managed service.

Tunnels, proxies, syncs and mounts each grew their own copy of "keep this
alive and start it at boot". This is that machinery on its own, pointed at
whatever you want: a dev server, a training job, a webhook receiver.

Examples:
  usm svc add web -- python -m http.server 8000   # define and start it
  usm svc ls                                      # what is running
  usm svc logs web -f                             # follow its output
  usm svc restart web
  usm svc enable web                              # also start it at boot
  usm svc rm web

A service is supervised by `usm svc run <id>`, which restarts the command
according to its policy and captures stdout/stderr to one rotating log. That
supervisor is what systemd/launchd is pointed at when you `enable`, and it is
what runs directly when you `start` on a host without either.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click
from usmo import ui

from usm_cli import grouped_class
from usm_daemon import (
    USM_CACHE_DIR,
    FileLock,
    ServiceManager,
    atomic_write,
    default_service_kind,
    pid_alive,
    read_json,
    sleep_until,
    slugify,
    usm_bin,
)

STATE_DIR = Path(os.environ.get("USM_SVC_STATE_DIR") or (USM_CACHE_DIR / "svc"))
SERVICE = ServiceManager("usm-svc-", "com.usm.svc.")

RESTART_POLICIES = ("always", "on-failure", "never")
DEFAULT_RESTART_SEC = 5.0
MAX_RESTART_SEC = 300.0
#: Below this, a start counts as a crash rather than a run; used to back off.
CRASH_WINDOW = 10.0
LOG_MAX_BYTES = 5 * 1024 * 1024

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


# ==========================================================================
# The service record
# ==========================================================================


@dataclass
class Service:
    id: str
    argv: list[str]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    restart: str = "always"
    restart_sec: float = DEFAULT_RESTART_SEC
    created_at: float = 0.0

    @property
    def command(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    def path(self) -> Path:
        return STATE_DIR / f"{self.id}.json"

    def runtime_path(self) -> Path:
        return STATE_DIR / f"{self.id}.runtime.json"

    def log_path(self) -> Path:
        return STATE_DIR / "logs" / f"{self.id}.log"

    def lock_path(self) -> Path:
        return STATE_DIR / f"{self.id}.lock"

    def working_dir(self) -> Path:
        return Path(self.cwd) if self.cwd else Path.home()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env": dict(self.env),
            "description": self.description,
            "restart": self.restart,
            "restart_sec": self.restart_sec,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Service":
        argv = raw.get("argv") or []
        if isinstance(argv, str):  # tolerate a hand-edited state file
            argv = shlex.split(argv)
        return cls(
            id=str(raw.get("id") or ""),
            argv=[str(a) for a in argv],
            cwd=str(raw.get("cwd") or ""),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            description=str(raw.get("description") or ""),
            restart=str(raw.get("restart") or "always"),
            restart_sec=float(raw.get("restart_sec") or DEFAULT_RESTART_SEC),
            created_at=float(raw.get("created_at") or 0.0),
        )


def save(service: Service) -> None:
    atomic_write(service.path(), json.dumps(service.to_dict(), indent=2))


def load(ident: str) -> Service | None:
    raw = read_json(STATE_DIR / f"{ident}.json")
    if not isinstance(raw, dict):
        return None
    service = Service.from_dict(raw)
    return service if service.id else None


def load_all() -> list[Service]:
    if not STATE_DIR.exists():
        return []
    out = []
    for path in sorted(STATE_DIR.glob("*.json")):
        if path.name.endswith(".runtime.json"):
            continue
        raw = read_json(path)
        if isinstance(raw, dict) and raw.get("id"):
            out.append(Service.from_dict(raw))
    return out


def require(ident: str) -> Service:
    service = load(ident)
    if service is None:
        known = [s.id for s in load_all()]
        hint = f" Known: {', '.join(known)}." if known else ""
        raise click.ClickException(f"No service named {ident!r}.{hint}")
    return service


# ==========================================================================
# Runtime state (written by the supervisor, read by everything else)
# ==========================================================================


def read_runtime(service: Service) -> dict:
    raw = read_json(service.runtime_path())
    return raw if isinstance(raw, dict) else {}


def write_runtime(service: Service, **fields) -> None:
    state = read_runtime(service)
    state.update(fields)
    atomic_write(service.runtime_path(), json.dumps(state, indent=2))


def supervisor_alive(service: Service) -> bool:
    return pid_alive(read_runtime(service).get("supervisor_pid"))


def child_alive(service: Service) -> bool:
    return pid_alive(read_runtime(service).get("pid"))


def running(service: Service) -> bool:
    return supervisor_alive(service) or child_alive(service)


# ==========================================================================
# The supervisor
# ==========================================================================


def _should_restart(policy: str, code: int | None) -> bool:
    if policy == "always":
        return True
    if policy == "on-failure":
        return code is None or code != 0
    return False


def _open_log(service: Service):
    path = service.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # One rotation is enough to keep a crash loop from filling the disk while
    # still leaving the previous run readable.
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:  # pragma: no cover - racing rotation
        pass
    return open(path, "a", buffering=1)


def supervise(
    service: Service, *, stop_event=None, max_restarts: int | None = None
) -> int:
    """Run the command, restarting per policy, until stopped.

    Returns the last exit code. *max_restarts* exists so tests can bound the
    loop; in production the loop ends only when the process is asked to stop.
    """
    import threading

    stop_event = stop_event or threading.Event()
    lock = FileLock(service.lock_path())
    if not lock.acquire():
        raise click.ClickException(f"{service.id} is already being supervised.")

    write_runtime(
        service,
        supervisor_pid=os.getpid(),
        started_at=time.time(),
        restarts=0,
        last_exit=None,
    )
    if not service.argv:
        # A hand-edited state file can leave this empty; Popen([]) raises
        # IndexError, which would surface as a traceback from the daemon.
        write_runtime(service, pid=None, last_error="no command configured")
        lock.release()
        return 127

    log = _open_log(service)
    env = dict(os.environ)
    env.update(service.env)
    delay = service.restart_sec
    restarts = 0
    code: int | None = None
    proc: subprocess.Popen | None = None

    def _terminate(*_args) -> None:
        stop_event.set()
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:  # pragma: no cover - already gone
                    pass

    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[sig] = signal.signal(sig, _terminate)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    try:
        while not stop_event.is_set():
            started = time.time()
            log.write(f"\n--- usm svc: starting {service.command}\n")
            try:
                proc = subprocess.Popen(
                    service.argv,
                    cwd=str(service.working_dir()),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, ValueError, IndexError) as exc:
                log.write(f"--- usm svc: cannot start: {exc}\n")
                write_runtime(service, pid=None, last_error=str(exc))
                return 127

            write_runtime(service, pid=proc.pid, last_error=None)
            while not stop_event.is_set():
                try:
                    code = proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if stop_event.is_set():
                _terminate()
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - stubborn
                    proc.kill()
                    code = proc.wait()
                write_runtime(service, pid=None, last_exit=code)
                break

            write_runtime(service, pid=None, last_exit=code)
            log.write(f"--- usm svc: exited with {code}\n")
            if not _should_restart(service.restart, code):
                break
            restarts += 1
            write_runtime(service, restarts=restarts)
            if max_restarts is not None and restarts >= max_restarts:
                break
            # Back off only for genuine crash loops; a long-lived process that
            # exits should come straight back.
            if time.time() - started < CRASH_WINDOW:
                delay = min(delay * 2, MAX_RESTART_SEC)
            else:
                delay = service.restart_sec
            if sleep_until(time.time() + delay, stop_event):
                break
    finally:
        # Leaving our handlers installed would hijack SIGINT for whatever
        # called us -- including the test runner.
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass
        write_runtime(service, supervisor_pid=None, pid=None, stopped_at=time.time())
        try:
            log.close()
        except OSError:  # pragma: no cover
            pass
        lock.release()
    return code if code is not None else 0


def supervisor_argv() -> list[str]:
    """How to re-enter this script as a supervisor.

    Spawning uses the interpreter already running us: we are inside the
    script's virtualenv, so this needs no catalog lookup and works from a
    local checkout. The boot units use `usm svc run` instead, because that
    stays correct across upgrades.
    """
    return [sys.executable, str(Path(__file__).resolve())]


def spawn_supervisor(service: Service) -> int:
    """Start the supervisor detached, so it outlives this invocation."""
    log = service.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", buffering=1) as fh:
        proc = subprocess.Popen(
            [*supervisor_argv(), "run", service.id],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc.pid


def stop_service(service: Service, *, timeout: float = 15.0) -> bool:
    """Ask the supervisor (or bare child) to stop. True if it was running."""
    state = read_runtime(service)
    was_running = running(service)
    for key in ("supervisor_pid", "pid"):
        pid = state.get(key)
        if not pid_alive(pid):
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:  # pragma: no cover - vanished
                pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running(service):
            return was_running
        time.sleep(0.1)
    for key in ("supervisor_pid", "pid"):
        pid = state.get(key)
        if pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:  # pragma: no cover - vanished
                pass
    return was_running


# ==========================================================================
# Presentation
# ==========================================================================


def _uptime(service: Service) -> str:
    state = read_runtime(service)
    started = state.get("started_at")
    if not running(service) or not started:
        return "—"
    return ui.compact_duration(max(0.0, time.time() - float(started)))


def _autostart(service: Service) -> str:
    kind = SERVICE.enabled_kind(service.id)
    return kind or "—"


def print_services(services: list[Service], *, title: str = "Services") -> None:
    if not services:
        ui.hint("No services. Define one with `usm svc add <id> -- <command>`.")
        return
    columns = [
        ui.Column("", justify="center", min_width=1),
        ui.Column("id", style=ui.STYLE_ID, min_width=4),
        ui.Column("command", min_width=16, ratio=1),
        ui.Column("uptime", justify="right", min_width=6, hide_below=64),
        ui.Column("boot", min_width=4, hide_below=76),
    ]
    built = ui.table(*columns, title=title)
    for service in services:
        built.add_row(
            *ui.row_for(
                columns,
                {
                    "": ui.state(running(service)),
                    "id": service.id,
                    "command": service.command,
                    "uptime": _uptime(service),
                    "boot": _autostart(service),
                },
            )
        )
    ui.print(built)
    live = sum(1 for s in services if running(s))
    ui.hint(
        ui.joined(
            ui.legend((ui.RUNNING, "running"), (ui.STOPPED, "stopped")),
            f"{live}/{len(services)} up",
            "usm svc logs <id> to look inside",
        )
    )


# ==========================================================================
# CLI
# ==========================================================================


SVC_SECTIONS = (
    ("Define", ("add", "rm")),
    ("Inspect", ("ls", "status", "logs")),
    ("Lifecycle", ("start", "stop", "restart")),
    ("Boot", ("enable", "disable")),
    ("Internal", ("run", "which")),
)
SvcGroup = grouped_class(SVC_SECTIONS, name="SvcGroup")


@click.group(
    cls=SvcGroup,
    context_settings=CONTEXT_SETTINGS,
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx):
    """Run any command as a managed service."""
    if ctx.invoked_subcommand is None:
        print_services(load_all())


@cli.command("add", context_settings={"ignore_unknown_options": True})
@click.argument("ident")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option("--cwd", type=click.Path(file_okay=False), help="Working directory.")
@click.option("--env", "envs", multiple=True, metavar="K=V", help="Extra environment.")
@click.option("--description", default="", help="What this service is for.")
@click.option(
    "--restart",
    type=click.Choice(RESTART_POLICIES),
    default="always",
    show_default=True,
)
@click.option(
    "--restart-sec", type=float, default=DEFAULT_RESTART_SEC, show_default=True
)
@click.option("--enable", is_flag=True, help="Also start it at boot.")
@click.option("--no-start", is_flag=True, help="Define it without starting it.")
@click.option("--force", is_flag=True, help="Replace an existing service.")
def cmd_add(
    ident,
    command,
    cwd,
    envs,
    description,
    restart,
    restart_sec,
    enable,
    no_start,
    force,
):
    """Define a service. Everything after `--` is the command to run."""
    ident = slugify(ident)
    if not ident:
        raise click.ClickException("The id must contain a letter or digit.")
    existing = load(ident)
    if existing and not force:
        raise click.ClickException(f"{ident} already exists (use --force to replace).")
    if existing and running(existing):
        stop_service(existing)

    argv = list(command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise click.ClickException("No command given.")

    env = {}
    for item in envs:
        key, _, value = item.partition("=")
        if not key or not _:
            raise click.ClickException(f"--env expects K=V, got {item!r}.")
        env[key] = value

    service = Service(
        id=ident,
        argv=argv,
        cwd=str(Path(cwd).expanduser().resolve()) if cwd else "",
        env=env,
        description=description,
        restart=restart,
        restart_sec=restart_sec,
        created_at=time.time(),
    )
    save(service)
    ui.ok(f"Defined {ui.identifier(ident)} · {service.command}")

    if enable:
        _enable(service)
    elif not no_start:
        spawn_supervisor(service)
        time.sleep(0.3)
        if running(service):
            ui.ok(f"Started {ui.identifier(ident)}.")
        else:
            ui.warn(f"{ident} did not stay up; see `usm svc logs {ident}`.")


@cli.command("ls")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def cmd_ls(as_json):
    """List defined services."""
    services = load_all()
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        **s.to_dict(),
                        "running": running(s),
                        "autostart": SERVICE.enabled_kind(s.id),
                        "runtime": read_runtime(s),
                    }
                    for s in services
                ],
                indent=2,
            )
        )
        return
    print_services(services)


@cli.command("status")
@click.argument("ident")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def cmd_status(ident, as_json):
    """Show one service in detail."""
    service = require(ident)
    state = read_runtime(service)
    if as_json:
        click.echo(
            json.dumps(
                {
                    **service.to_dict(),
                    "running": running(service),
                    "autostart": SERVICE.enabled_kind(service.id),
                    "runtime": state,
                },
                indent=2,
            )
        )
        return

    rows = [
        (ui.SECTION, "Service"),
        ("id", service.id),
        ("command", service.command),
        ("state", ui.status_text(running(service), "running", "stopped")),
        ("description", service.description or "—"),
        ("directory", ui.shorten_path(service.working_dir())),
        (ui.SECTION, "Policy"),
        ("restart", service.restart),
        ("delay", ui.compact_duration(service.restart_sec)),
        ("at boot", _autostart(service)),
        (ui.SECTION, "Runtime"),
        ("pid", str(state.get("pid") or "—")),
        ("uptime", _uptime(service)),
        ("restarts", str(state.get("restarts", 0))),
        (
            "last exit",
            str(state.get("last_exit")) if state.get("last_exit") is not None else "—",
        ),
        ("log", ui.shorten_path(service.log_path())),
    ]
    if service.env:
        rows.append((ui.SECTION, "Environment"))
        rows.extend((k, ui.redact(v)) for k, v in sorted(service.env.items()))
    if state.get("last_error"):
        rows.append(("error", str(state["last_error"])))
    ui.print_detail(rows)


@cli.command("start")
@click.argument("ident")
def cmd_start(ident):
    """Start a stopped service."""
    service = require(ident)
    if running(service):
        ui.hint(f"{ident} is already running.")
        return
    if SERVICE.enabled_kind(ident):
        SERVICE.start(ident)
    else:
        spawn_supervisor(service)
    time.sleep(0.3)
    if running(service):
        ui.ok(f"Started {ui.identifier(ident)}.")
    else:
        ui.warn(f"{ident} did not come up; see `usm svc logs {ident}`.")


@cli.command("stop")
@click.argument("ident")
def cmd_stop(ident):
    """Stop a service (keeps the definition)."""
    service = require(ident)
    if SERVICE.enabled_kind(ident):
        SERVICE.stop(ident)
    if stop_service(service):
        ui.ok(f"Stopped {ui.identifier(ident)}.")
    else:
        ui.hint(f"{ident} was not running.")


@cli.command("restart")
@click.argument("ident")
def cmd_restart(ident):
    """Stop and start a service."""
    service = require(ident)
    stop_service(service)
    if SERVICE.enabled_kind(ident):
        SERVICE.start(ident)
    else:
        spawn_supervisor(service)
    time.sleep(0.3)
    if running(service):
        ui.ok(f"Restarted {ui.identifier(ident)}.")
    else:
        ui.warn(f"{ident} did not come back; see `usm svc logs {ident}`.")


@cli.command("logs")
@click.argument("ident")
@click.option("-n", "--lines", type=int, default=40, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Keep printing new output.")
def cmd_logs(ident, lines, follow):
    """Show a service's output."""
    service = require(ident)
    path = service.log_path()
    if not path.exists():
        ui.hint(f"No log yet for {ident}.")
        return
    with open(path, errors="replace") as fh:
        tail = fh.readlines()[-lines:]
    for line in tail:
        click.echo(line.rstrip("\n"))
    if not follow:
        return
    ui.hint(f"following {ui.shorten_path(path)} — ctrl-c to stop")
    with open(path, errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                line = fh.readline()
                if line:
                    click.echo(line.rstrip("\n"))
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            pass


def _enable(service: Service) -> None:
    try:
        kind = SERVICE.enable(
            service.id,
            [usm_bin(), "svc", "run", service.id],
            description=service.description or f"usm svc {service.id}",
            log_path=service.log_path(),
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    ui.ok(f"{ui.identifier(service.id)} starts at boot via {kind}.")


@cli.command("enable")
@click.argument("ident")
def cmd_enable(ident):
    """Start this service at boot."""
    service = require(ident)
    if running(service) and not SERVICE.enabled_kind(ident):
        # Hand the process over rather than ending up with two supervisors.
        stop_service(service)
    _enable(service)


@cli.command("disable")
@click.argument("ident")
def cmd_disable(ident):
    """Don't start this service at boot."""
    require(ident)
    kind = SERVICE.disable(ident)
    if kind:
        ui.ok(f"{ui.identifier(ident)} no longer starts at boot ({kind}).")
    else:
        ui.hint(f"{ident} was not enabled.")


@cli.command("rm")
@click.argument("ident")
@click.option("--keep-logs", is_flag=True, help="Leave the log file behind.")
def cmd_rm(ident, keep_logs):
    """Stop, disable, and forget a service."""
    service = require(ident)
    SERVICE.disable(ident)
    stop_service(service)
    service.path().unlink(missing_ok=True)
    service.runtime_path().unlink(missing_ok=True)
    service.lock_path().unlink(missing_ok=True)
    if not keep_logs:
        service.log_path().unlink(missing_ok=True)
        service.log_path().with_name(service.log_path().name + ".1").unlink(
            missing_ok=True
        )
    ui.ok(f"Removed {ui.identifier(ident)}.")


@cli.command("run", hidden=True)
@click.argument("ident")
def cmd_run(ident):
    """Supervise a service in the foreground (used by systemd/launchd)."""
    service = require(ident)
    sys.exit(supervise(service))


@cli.command("which", hidden=True)
def cmd_which():
    """Print where state lives (useful when debugging)."""
    ui.print_detail(
        [
            ("state", ui.shorten_path(STATE_DIR)),
            ("logs", ui.shorten_path(STATE_DIR / "logs")),
            ("backend", default_service_kind()),
        ]
    )


if __name__ == "__main__":
    cli()
