#!/usr/bin/env python3
"""Quick file/directory sharing via a local HTTP server, optionally exposed
to a remote SSH host through `usm tunnel remote`.

  usm share ./build.tar.gz                 # serve the file's parent dir on a random local port
  usm share ./reports --port 8000
  usm share file.zip --tunnel user@bastion # also opens server:RPORT -> here:PORT
  usm share dir --tunnel user@host:9000    # pin the remote port

Files older than 24h or larger than RAM should probably use a real upload
service. This exists for the "I have something on this VM and want a teammate
to grab it RIGHT NOW" case.
"""

from __future__ import annotations

import hashlib
import os
import random
import socket
import subprocess
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import click
from rich.console import Console

console = Console()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _looks_taken(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _serve(root: Path, port: int, bind: str) -> HTTPServer:
    handler = type(
        "QuietHandler",
        (SimpleHTTPRequestHandler,),
        {
            "log_message": lambda self, fmt, *args: console.print(
                f"  [dim]{self.address_string()} → {fmt % args}[/dim]"
            ),
        },
    )
    os.chdir(root)
    httpd = HTTPServer((bind, port), handler)
    return httpd


def _open_tunnel(spec: str, lport: int) -> tuple[subprocess.Popen, int, str]:
    """Spawn `usm tunnel remote RPORT:LPORT ssh_target`. Returns (proc, rport, ssh_target)."""
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
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-R",
        f"{rport}:localhost:{lport}",
        ssh_target,
    ]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, rport, ssh_target


def _digest(path: Path, max_bytes: int = 50 * 1024 * 1024) -> str | None:
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > max_bytes:
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Serve a file or directory over HTTP (and optionally tunnel it).",
)
@click.argument("path", type=click.Path(exists=True, path_type=Path))
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
        "GatewayPorts=yes)."
    ),
)
def cli(path, port, bind, tunnel):
    path = path.resolve()
    root = path if path.is_dir() else path.parent
    suffix = "" if path.is_dir() else f"/{path.name}"
    if port is None:
        port = _free_port()
    elif _looks_taken(port):
        raise click.ClickException(
            f"Port {port} is in use. Pick a different --port or omit to auto-pick."
        )
    httpd = _serve(root, port, bind)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    digest = _digest(path) if path.is_file() else None
    console.print(
        f"[green]✓[/green] serving [bold]{path}[/bold]\n"
        f"  local:  http://{bind}:{port}{suffix}"
        + (f"  [dim](sha256: {digest}…)[/dim]" if digest else "")
    )

    tunnel_proc = None
    if tunnel:
        try:
            tunnel_proc, rport, ssh_target = _open_tunnel(tunnel, port)
        except OSError as e:
            httpd.shutdown()
            raise click.ClickException(f"failed to start tunnel: {e}") from e
        host = ssh_target.split("@", 1)[-1]
        console.print(
            f"  remote: http://{host}:{rport}{suffix}  "
            f"[dim](via ssh -R; reachable on {ssh_target}'s localhost)[/dim]"
        )
        time.sleep(1.0)
        if tunnel_proc.poll() is not None:
            httpd.shutdown()
            raise click.ClickException(
                f"tunnel ssh exited immediately (code {tunnel_proc.returncode})."
            )

    console.print("[dim]ctrl-c to stop[/dim]")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        if tunnel_proc:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()
        console.print("[dim]stopped.[/dim]")


if __name__ == "__main__":
    cli()
