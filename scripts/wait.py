#!/usr/bin/env python3
"""Wait until one or more endpoints become reachable.

Targets:
  host:port            TCP connect
  tcp://host:port      same
  http://...           HTTP GET, any non-5xx counts as up
  https://...          same

All targets are checked concurrently; the command exits 0 once they're ALL up
(or non-zero on timeout).

Examples:
  usm wait db:5432
  usm wait http://api.local/health redis:6379
  usm wait https://example.com/ --timeout 60 --interval 2
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.parse

import click
import httpx
from rich.console import Console

console = Console(stderr=True)


def _parse(target: str) -> tuple[str, str, int | None]:
    if target.startswith(("http://", "https://")):
        return ("http", target, None)
    if target.startswith("tcp://"):
        target = target[len("tcp://") :]
    if ":" not in target:
        raise click.BadParameter(f"Invalid target {target!r}: need host:port or URL.")
    host, _, port = target.rpartition(":")
    try:
        return ("tcp", host, int(port))
    except ValueError:
        raise click.BadParameter(f"Invalid port in {target!r}.") from None


def _probe_tcp(host: str, port: int, connect_timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=connect_timeout):
            return True
    except OSError:
        return False


def _probe_http(url: str, connect_timeout: float) -> bool:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect_timeout), follow_redirects=True
        ) as c:
            r = c.get(url)
        return r.status_code < 500
    except (httpx.HTTPError, OSError):
        return False


def _label(kind: str, host: str, port: int | None) -> str:
    if kind == "http":
        return urllib.parse.urlparse(host).netloc + urllib.parse.urlparse(host).path
    return f"{host}:{port}"


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Wait until every TARGET is reachable.\n\n"
        "TARGET is host:port, tcp://host:port, or http(s)://url."
    ),
)
@click.argument("targets", nargs=-1, required=True)
@click.option(
    "-t",
    "--timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Total seconds to wait before giving up.",
)
@click.option(
    "-i",
    "--interval",
    type=float,
    default=1.0,
    show_default=True,
    help="Seconds between retries per target.",
)
@click.option(
    "--connect-timeout",
    type=float,
    default=3.0,
    show_default=True,
    help="Per-attempt connect timeout.",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress per-target progress lines.")
def cli(targets, timeout, interval, connect_timeout, quiet):
    parsed = [_parse(t) for t in targets]
    state: dict[int, bool] = {i: False for i in range(len(parsed))}
    lock = threading.Lock()
    deadline = time.monotonic() + timeout

    def worker(idx: int, kind: str, host: str, port: int | None):
        while time.monotonic() < deadline:
            ok = (
                _probe_http(host, connect_timeout)
                if kind == "http"
                else _probe_tcp(host, port, connect_timeout)
            )
            if ok:
                with lock:
                    state[idx] = True
                if not quiet:
                    console.print(f"[green]✓[/green] {_label(kind, host, port)}")
                return
            time.sleep(interval)

    threads = [
        threading.Thread(target=worker, args=(i, *spec), daemon=True)
        for i, spec in enumerate(parsed)
    ]
    for t in threads:
        t.start()
    while time.monotonic() < deadline:
        if all(state.values()):
            return
        time.sleep(0.1)
    missing = [
        _label(kind, host, port)
        for i, (kind, host, port) in enumerate(parsed)
        if not state[i]
    ]
    if missing:
        console.print(
            f"[red]✗[/red] timeout after {timeout}s; not reachable: {', '.join(missing)}"
        )
        sys.exit(1)


if __name__ == "__main__":
    cli()
