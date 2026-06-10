#!/usr/bin/env python3
"""Quick file/directory sharing via a local HTTP server, optionally exposed
to a remote SSH host through `usm tunnel remote`.

  usm share ./build.tar.gz                 # serve local path on a random port
  usm share ./reports --port 8000
  usm share file.zip --tunnel user@bastion # also opens server:RPORT -> here:PORT
  usm share dir --tunnel user@host:9000    # pin the remote port
  usm share user@host:/srv/logs            # pull remote: ssh -L + python3 -m http.server on host

Files older than 24h or larger than RAM should probably use a real upload
service. This exists for the "I have something on this VM and want a teammate
to grab it RIGHT NOW" case.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import random
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable, Protocol

import click
from rich.console import Console

console = Console()


# ---- low-level utilities ----------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def short_sha256(path: Path, max_bytes: int = 50 * 1024 * 1024) -> str | None:
    if not path.is_file() or path.stat().st_size > max_bytes:
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def resolve_port(port: int | None) -> int:
    if port is None:
        return free_port()
    if port_in_use(port):
        raise click.ClickException(
            f"Port {port} is in use. Pick a different --port or omit to auto-pick."
        )
    return port


_HOST_RE = re.compile(r"^(?:[^@\s:]+@)?[^@\s:/]+$")


def parse_remote(spec: str) -> tuple[str, str] | None:
    """scp-style ``[user@]host:[path]``. Returns ``(target, path)`` or ``None``.

    Heuristic matches scp/rsync: the segment before the first ``:`` is the
    host (with optional ``user@``) only if it has no ``/`` — that disambiguates
    ``./foo:bar`` (local) from ``host:path`` (remote). Empty path defaults to
    the remote ``~``.
    """
    head, sep, path = spec.partition(":")
    if not sep or "/" in head or not _HOST_RE.match(head):
        return None
    return head, path or "~"


def _quote_remote_path(p: str) -> str:
    """shlex.quote that preserves leading ``~`` / ``~user`` expansion."""
    if p in ("~",) or re.fullmatch(r"~[^/]+", p):
        return p
    m = re.match(r"^(~[^/]*)/(.*)$", p)
    if m:
        prefix, rest = m.group(1), m.group(2)
        return f"{prefix}/{shlex.quote(rest)}" if rest else f"{prefix}/"
    return shlex.quote(p)


# ---- ssh primitives ---------------------------------------------------------

_SSH_KEEPALIVE: tuple[str, ...] = (
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "StrictHostKeyChecking=accept-new",
)


def _spawn_ssh(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)


def open_reverse_tunnel(spec: str, lport: int) -> tuple[subprocess.Popen, int, str]:
    """ssh -R RPORT:localhost:LPORT user@host (push)."""
    if ":" in spec and "@" in spec and spec.rfind(":") > spec.rfind("@"):
        ssh_target, _, rport_s = spec.rpartition(":")
        try:
            rport = int(rport_s)
        except ValueError as e:
            raise click.BadParameter(f"Invalid remote port in {spec!r}.") from e
    else:
        ssh_target = spec
        rport = random.randint(20000, 65000)
    argv = [
        "ssh",
        "-N",
        "-T",
        *_SSH_KEEPALIVE,
        "-R",
        f"{rport}:localhost:{lport}",
        ssh_target,
    ]
    return _spawn_ssh(argv), rport, ssh_target


_RUNTIME_PREFIX: dict[str, str] = {
    "uv": "uv run --no-project --quiet python",
    "python3": "python3",
}


@dataclass(frozen=True)
class RemoteProbe:
    kind: str
    runtime: str
    label: str


def probe_remote(ssh_target: str, path: str) -> RemoteProbe:
    """Probe host for path kind + a usable Python runtime (uv preferred)."""
    quoted = _quote_remote_path(path)
    snippet = (
        "if command -v uv >/dev/null 2>&1; then RT=uv; "
        "elif command -v python3 >/dev/null 2>&1; then RT=python3; "
        "else echo no-runtime; exit 0; fi; "
        f"if [ -d {quoted} ]; then K=dir; "
        f"elif [ -f {quoted} ]; then K=file; "
        "else echo missing; exit 0; fi; "
        'echo "$RT $K"'
    )
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=10",
                ssh_target,
                snippet,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise click.ClickException(f"ssh probe to {ssh_target} failed: {e}") from e
    if r.returncode != 0:
        msg = r.stderr.strip() or f"exit {r.returncode}"
        raise click.ClickException(f"ssh {ssh_target} probe failed: {msg}")
    out = (r.stdout.strip().splitlines() or [""])[-1]
    if out == "no-runtime":
        raise click.ClickException(
            f"neither uv nor python3 found on {ssh_target}; can't run remote http.server."
        )
    if out == "missing":
        raise click.ClickException(f"path not found on {ssh_target}: {path}")
    parts = out.split()
    if (
        len(parts) != 2
        or parts[0] not in _RUNTIME_PREFIX
        or parts[1] not in ("dir", "file")
    ):
        raise click.ClickException(f"unexpected probe output: {r.stdout!r}")
    rt, kind = parts
    return RemoteProbe(kind=kind, runtime=_RUNTIME_PREFIX[rt], label=rt)


def open_forward_serve(
    ssh_target: str, remote_dir: str, runtime: str, lport: int, bind: str
) -> tuple[subprocess.Popen, int]:
    """ssh -L LPORT:127.0.0.1:RPORT + `<runtime> -m http.server` on the remote (pull)."""
    rport = random.randint(20000, 65000)
    remote_cmd = (
        f"exec {runtime} -m http.server {rport} "
        f"--bind 127.0.0.1 --directory {_quote_remote_path(remote_dir)}"
    )
    forward = (
        f"{lport}:127.0.0.1:{rport}"
        if bind == "127.0.0.1"
        else f"{bind}:{lport}:127.0.0.1:{rport}"
    )
    argv = ["ssh", "-T", *_SSH_KEEPALIVE]
    if bind not in ("127.0.0.1", "localhost"):
        argv.append("-g")
    argv += ["-L", forward, ssh_target, remote_cmd]
    return _spawn_ssh(argv), rport


# ---- session: owns the lifetime of a running share --------------------------


@dataclass
class Session:
    headline: str
    lines: list[str] = field(default_factory=list)
    _procs: list[subprocess.Popen] = field(default_factory=list)
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def add_proc(self, proc: subprocess.Popen) -> None:
        self._procs.append(proc)

    def add_closer(self, closer: Callable[[], None]) -> None:
        self._closers.append(closer)

    def add_line(self, line: str) -> None:
        self.lines.append(line)

    def banner(self) -> str:
        return "\n".join(
            [f"[green]✓[/green] serving [bold]{self.headline}[/bold]", *self.lines]
        )

    def healthy(self) -> bool:
        return all(p.poll() is None for p in self._procs)

    def close(self) -> None:
        for c in self._closers:
            with contextlib.suppress(Exception):
                c()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _quiet_http_server(root: Path, port: int, bind: str) -> HTTPServer:
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            console.print(f"  [dim]{self.address_string()} → {fmt % args}[/dim]")

    handler = functools.partial(QuietHandler, directory=str(root))
    return HTTPServer((bind, port), handler)


def _wait_for_ssh_or_die(proc: subprocess.Popen, sess: Session, hint: str) -> None:
    time.sleep(1.0)
    if proc.poll() is not None:
        rc = proc.returncode
        sess.close()
        raise click.ClickException(f"{hint} (exit {rc}).")


# ---- sources ----------------------------------------------------------------


class Source(Protocol):
    def open(self, port: int, bind: str) -> Session: ...


@dataclass
class LocalSource:
    path: Path
    tunnel: str | None = None

    def open(self, port: int, bind: str) -> Session:
        path = self.path.resolve()
        root = path if path.is_dir() else path.parent
        suffix = "" if path.is_dir() else f"/{path.name}"

        httpd = _quiet_http_server(root, port, bind)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        sess = Session(headline=str(path))
        sess.add_closer(httpd.shutdown)

        digest = short_sha256(path) if path.is_file() else None
        url_line = f"  local:  http://{bind}:{port}{suffix}"
        if digest:
            url_line += f"  [dim](sha256: {digest}…)[/dim]"
        sess.add_line(url_line)

        if self.tunnel:
            self._attach_push_tunnel(sess, port, suffix)
        return sess

    def _attach_push_tunnel(self, sess: Session, lport: int, suffix: str) -> None:
        try:
            proc, rport, ssh_target = open_reverse_tunnel(self.tunnel, lport)
        except OSError as e:
            sess.close()
            raise click.ClickException(f"failed to start tunnel: {e}") from e
        sess.add_proc(proc)
        host = ssh_target.split("@", 1)[-1]
        sess.add_line(
            f"  remote: http://{host}:{rport}{suffix}  "
            f"[dim](via ssh -R; reachable on {ssh_target}'s localhost)[/dim]"
        )
        _wait_for_ssh_or_die(proc, sess, "tunnel ssh exited immediately")


@dataclass
class RemoteSource:
    ssh_target: str
    remote_path: str

    def open(self, port: int, bind: str) -> Session:
        probe = probe_remote(self.ssh_target, self.remote_path)
        if probe.kind == "dir":
            remote_dir, suffix = self.remote_path, "/"
        else:
            parent = os.path.dirname(self.remote_path)
            remote_dir = parent if parent else "~"
            suffix = "/" + os.path.basename(self.remote_path)

        proc, rport = open_forward_serve(
            self.ssh_target, remote_dir, probe.runtime, port, bind
        )
        sess = Session(
            headline=f"{self.ssh_target}:{self.remote_path} [dim]({probe.kind})[/dim]",
            lines=[
                f"  local:  http://{bind}:{port}{suffix}  "
                f"[dim](ssh -L {port}->{rport}; {probe.label} -m http.server on remote)[/dim]",
            ],
        )
        sess.add_proc(proc)
        _wait_for_ssh_or_die(
            proc,
            sess,
            "ssh exited immediately; remote port may have been taken",
        )
        return sess


def make_source(spec: str, tunnel: str | None) -> Source:
    remote = parse_remote(spec)
    if remote is not None:
        if tunnel:
            raise click.ClickException(
                "--tunnel can't be combined with a remote source (user@host:/path)."
            )
        ssh_target, remote_path = remote
        return RemoteSource(ssh_target, remote_path)
    path = Path(spec)
    if not path.exists():
        raise click.ClickException(f"path not found: {spec}")
    return LocalSource(path, tunnel=tunnel)


# ---- runner & CLI -----------------------------------------------------------


def run_until_done(sess: Session) -> None:
    console.print(sess.banner())
    console.print("[dim]ctrl-c to stop[/dim]")
    try:
        with sess:
            while sess.healthy():
                time.sleep(0.5)
            console.print("[yellow]child process exited; stopping.[/yellow]")
    except KeyboardInterrupt:
        pass
    console.print("[dim]stopped.[/dim]")


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Serve a file/dir over HTTP. Source is a local path or user@host:/remote/path.",
)
@click.argument("path", type=str)
@click.option(
    "-p",
    "--port",
    type=int,
    default=None,
    help="Local port to bind (default: random free port).",
)
@click.option(
    "--bind",
    default="127.0.0.1",
    show_default=True,
    help="Local bind address (set to 0.0.0.0 to expose on the LAN).",
)
@click.option(
    "--tunnel",
    default=None,
    help=(
        "SSH target (user@host[:remote_port]); opens a reverse tunnel so the "
        "URL is reachable on that host's localhost (or its public bind if "
        "GatewayPorts=yes). Only valid with a local source path."
    ),
)
def cli(path, port, bind, tunnel):
    source = make_source(path, tunnel)
    session = source.open(resolve_port(port), bind)
    run_until_done(session)


if __name__ == "__main__":
    cli()
