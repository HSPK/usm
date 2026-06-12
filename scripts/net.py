#!/usr/bin/env python3
"""Inspect, diagnose, and monitor this host's networking (read-only).

Examples:
  usm net                # dashboard: interfaces, gateway, DNS
  usm net addr eth0      # addresses on one interface
  usm net conns -w       # live table of established connections
  usm net ping 1.1.1.1   # ping with loss / RTT summary
  usm net lookup gnu.org # DNS resolution + timing
  usm net pubip          # public IP + geo (the only command that calls out)
  usm net speed -w       # live per-interface throughput

Read-only by design: nothing here changes interfaces, routes, DNS, or
firewall rules. The bare dashboard never makes a network request.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

import click
import psutil
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()


# Formatting helpers --------------------------------------------------------


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}EiB"


def fmt_rate(bytes_per_sec: float) -> str:
    return f"{fmt_bytes(bytes_per_sec)}/s"


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(argv: list[str], *, timeout: int = 15):
    """Run a command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# Interfaces ----------------------------------------------------------------


@dataclass
class Iface:
    name: str
    up: bool
    speed: int  # Mbps, 0 = unknown
    mtu: int
    mac: str
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)
    rx: int = 0
    tx: int = 0


def interfaces() -> list[Iface]:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    io = psutil.net_io_counters(pernic=True)
    out: list[Iface] = []
    for name, addr_list in addrs.items():
        mac = ""
        v4: list[str] = []
        v6: list[str] = []
        for a in addr_list:
            if a.family == socket.AF_INET:
                v4.append(a.address)
            elif a.family == socket.AF_INET6:
                v6.append(a.address.split("%")[0])
            elif a.family == psutil.AF_LINK:
                mac = a.address
        st = stats.get(name)
        c = io.get(name)
        out.append(
            Iface(
                name=name,
                up=st.isup if st else False,
                speed=st.speed if st else 0,
                mtu=st.mtu if st else 0,
                mac=mac,
                ipv4=v4,
                ipv6=v6,
                rx=c.bytes_recv if c else 0,
                tx=c.bytes_sent if c else 0,
            )
        )
    out.sort(key=lambda i: (i.name == "lo", not i.up, i.name))
    return out


def gateways() -> list[tuple[str, str]]:
    """Return (gateway, iface) pairs for default routes (via `ip`)."""
    res: list[tuple[str, str]] = []
    if not _have("ip"):
        return res
    for args in (
        ["ip", "route", "show", "default"],
        ["ip", "-6", "route", "show", "default"],
    ):
        rc, out, _ = _run(args)
        if rc != 0:
            continue
        for line in out.splitlines():
            parts = line.split()
            if "via" in parts and "dev" in parts:
                res.append(
                    (parts[parts.index("via") + 1], parts[parts.index("dev") + 1])
                )
    return res


def dns_servers() -> list[str]:
    servers: list[str] = []
    if _have("resolvectl"):
        rc, out, _ = _run(["resolvectl", "status"])
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith(("Current DNS Server:", "DNS Servers:")):
                    servers += line.split(":", 1)[1].split()
    if not servers:
        try:
            with open("/etc/resolv.conf") as fh:
                for line in fh:
                    if line.startswith("nameserver"):
                        servers.append(line.split()[1])
        except OSError:
            pass
    seen: set[str] = set()
    return [s for s in servers if not (s in seen or seen.add(s))]


# Rendering -----------------------------------------------------------------


def _ifaces_table(rows: list[Iface]) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="dim",
        pad_edge=False,
        padding=(0, 2, 0, 0),
    )
    table.add_column("iface", style="bold cyan", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("ipv4", no_wrap=True, overflow="fold")
    table.add_column("mac", no_wrap=True, style="dim")
    table.add_column("mtu", justify="right", no_wrap=True)
    table.add_column("speed", justify="right", no_wrap=True)
    table.add_column("rx", justify="right", no_wrap=True)
    table.add_column("tx", justify="right", no_wrap=True)
    for i in rows:
        state = "[green]up[/green]" if i.up else "[dim]down[/dim]"
        speed = f"{i.speed}M" if i.speed else "[dim]—[/dim]"
        table.add_row(
            i.name,
            state,
            ", ".join(i.ipv4) or "[dim]—[/dim]",
            i.mac or "[dim]—[/dim]",
            str(i.mtu or "—"),
            speed,
            fmt_bytes(i.rx),
            fmt_bytes(i.tx),
        )
    return table


def _render_dashboard() -> None:
    rows = interfaces()
    console.print(_ifaces_table(rows))
    gws = gateways()
    dns = dns_servers()
    meta = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
    meta.add_column(justify="right", style="dim", no_wrap=True)
    meta.add_column(overflow="fold")
    if gws:
        meta.add_row(
            "gateway", "  ".join(f"{gw} [dim]({dev})[/dim]" for gw, dev in gws)
        )
    if dns:
        meta.add_row("dns", "  ".join(dns))
    if gws or dns:
        console.print()
        console.print(meta)
    console.print(
        "\n[dim]Run [bold]usm net -h[/bold] for diagnostics "
        "(ping, trace, lookup, pubip, speed).[/dim]"
    )


# CLI -----------------------------------------------------------------------

COMMAND_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Inspect", ("ls", "addr", "routes", "dns", "neigh", "conns", "fw")),
    ("Diagnose", ("ping", "trace", "lookup", "mtu", "pubip")),
    ("Monitor", ("speed",)),
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


@cli.command("ls")
def cmd_ls() -> None:
    """Show the interface / gateway / DNS dashboard (the default view)."""
    _render_dashboard()


@cli.command("addr")
@click.argument("iface", required=False)
def cmd_addr(iface: str | None) -> None:
    """Show addresses (IPv4/IPv6/MAC) for all interfaces or just IFACE."""
    rows = interfaces()
    if iface:
        rows = [i for i in rows if i.name == iface]
        if not rows:
            raise click.ClickException(f"no such interface: {iface}")
    for i in rows:
        state = "[green]up[/green]" if i.up else "[dim]down[/dim]"
        console.print(
            f"[bold cyan]{i.name}[/bold cyan] {state}  [dim]mtu {i.mtu}[/dim]"
        )
        if i.mac:
            console.print(f"    mac  {i.mac}")
        for a in i.ipv4:
            console.print(f"    v4   {a}")
        for a in i.ipv6:
            console.print(f"    v6   [dim]{a}[/dim]")


@cli.command("routes")
def cmd_routes() -> None:
    """Show the IPv4/IPv6 routing tables (ip route)."""
    if not _have("ip"):
        raise click.ClickException("'ip' (iproute2) is not available.")
    for label, args in (("IPv4", ["ip", "route"]), ("IPv6", ["ip", "-6", "route"])):
        rc, out, _ = _run(args)
        text = out.strip()
        if rc == 0 and text:
            console.print(f"[bold]{label}[/bold]")
            console.print(text)


@cli.command("dns")
def cmd_dns() -> None:
    """Show configured DNS servers and search domains."""
    servers = dns_servers()
    if servers:
        console.print("[bold]DNS servers[/bold]")
        for s in servers:
            console.print(f"  {s}")
    else:
        console.print("[dim]No DNS servers found.[/dim]")
    if _have("resolvectl"):
        rc, out, _ = _run(["resolvectl", "domain"])
        if rc == 0 and out.strip():
            console.print("\n[bold]Search domains[/bold]")
            console.print(out.strip())


@cli.command("neigh")
def cmd_neigh() -> None:
    """Show the ARP / neighbor table (ip neigh)."""
    if not _have("ip"):
        raise click.ClickException("'ip' (iproute2) is not available.")
    rc, out, err = _run(["ip", "neigh"])
    if rc != 0:
        raise click.ClickException(err.strip() or "ip neigh failed.")
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("address", style="bold", no_wrap=True)
    table.add_column("dev", no_wrap=True)
    table.add_column("lladdr", no_wrap=True)
    table.add_column("state", no_wrap=True)
    count = 0
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        dev = parts[parts.index("dev") + 1] if "dev" in parts else ""
        lladdr = parts[parts.index("lladdr") + 1] if "lladdr" in parts else "—"
        state = parts[-1]
        table.add_row(ip, dev, lladdr, state)
        count += 1
    if not count:
        console.print("[dim]Neighbor table is empty.[/dim]")
        return
    console.print(table)


def _connections() -> list:
    try:
        return psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        console.print(
            "[yellow]note:[/yellow] some sockets hidden — try sudo for full visibility."
        )
        conns = []
        for proc in psutil.process_iter(["pid"]):
            try:
                conns.extend(proc.net_connections(kind="inet"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return conns


def _conns_table() -> Table:
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("proto", no_wrap=True)
    table.add_column("local", no_wrap=True)
    table.add_column("remote", no_wrap=True)
    table.add_column("pid", justify="right", style="dim", no_wrap=True)
    table.add_column("process", overflow="ellipsis", max_width=28, no_wrap=True)
    rows = 0
    for c in _connections():
        if c.status != psutil.CONN_ESTABLISHED or not c.raddr:
            continue
        proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "—"
        remote = f"{c.raddr.ip}:{c.raddr.port}"
        name = ""
        if c.pid:
            try:
                name = psutil.Process(c.pid).name()
            except psutil.Error:
                name = ""
        table.add_row(proto, local, remote, str(c.pid or "—"), name or "[dim]—[/dim]")
        rows += 1
    table.title = f"Established connections ({rows})"
    table.title_style = "bold"
    table.title_justify = "left"
    return table


@cli.command("conns")
@click.option("--watch", "-w", is_flag=True, help="Refresh continuously.")
@click.option("--interval", "-i", default=2.0, show_default=True)
def cmd_conns(watch: bool, interval: float) -> None:
    """List established connections and their owning process."""
    if not watch:
        console.print(_conns_table())
        return
    try:
        with Live(
            _conns_table(), console=console, screen=True, refresh_per_second=4
        ) as live:
            while True:
                time.sleep(max(0.5, interval))
                live.update(_conns_table())
    except KeyboardInterrupt:
        pass


@cli.command("fw")
def cmd_fw() -> None:
    """Show firewall status (ufw / nftables / iptables; read-only)."""
    if _have("ufw"):
        rc, out, err = _run(["ufw", "status", "verbose"])
        if rc == 0:
            console.print("[bold]ufw[/bold]")
            console.print(out.strip() or "[dim](no output)[/dim]")
            return
        if "permission" in (err + out).lower() or _needs_root():
            raise click.ClickException("ufw needs root — try: sudo usm net fw")
    if _have("nft"):
        rc, out, err = _run(["nft", "list", "ruleset"])
        if rc == 0:
            console.print("[bold]nftables[/bold]")
            console.print(out.strip() or "[dim](empty ruleset)[/dim]")
            return
        raise click.ClickException("nft needs root — try: sudo usm net fw")
    if _have("iptables"):
        rc, out, err = _run(["iptables", "-S"])
        if rc == 0:
            console.print("[bold]iptables[/bold]")
            console.print(out.strip() or "[dim](no rules)[/dim]")
            return
        raise click.ClickException("iptables needs root — try: sudo usm net fw")
    raise click.ClickException("no firewall tool found (ufw / nft / iptables).")


def _needs_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() != 0


# Diagnose ------------------------------------------------------------------


@cli.command("ping")
@click.argument("host")
@click.option("-c", "--count", default=4, show_default=True, help="Packets to send.")
def cmd_ping(host: str, count: int) -> None:
    """Ping HOST and summarise packet loss and RTT."""
    if not _have("ping"):
        raise click.ClickException("'ping' is not available.")
    with console.status(f"pinging {host} ({count})…"):
        rc, out, err = _run(
            ["ping", "-c", str(count), "-W", "2", host], timeout=count * 3 + 10
        )
    if not out.strip():
        raise click.ClickException(err.strip() or f"ping {host} failed.")
    loss = rtt = ""
    for line in out.splitlines():
        if "packet loss" in line:
            loss = next(
                (p.strip().split()[0] for p in line.split(",") if "loss" in p), ""
            )
        if "rtt" in line or "round-trip" in line:
            rtt = line.split("=", 1)[1].strip() if "=" in line else ""
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="dim")
    table.add_column()
    table.add_row("host", host)
    if loss:
        color = "green" if loss.startswith("0") else "yellow"
        table.add_row("loss", f"[{color}]{loss}[/{color}]")
    if rtt:
        table.add_row("rtt", rtt)
    console.print(table)
    if not rtt and not loss:
        console.print(out.strip())


@cli.command("trace")
@click.argument("host")
def cmd_trace(host: str) -> None:
    """Trace the route to HOST (mtr if present, else traceroute)."""
    if _have("mtr"):
        with console.status(f"tracing {host} via mtr…"):
            rc, out, err = _run(
                ["mtr", "--report", "--report-cycles", "3", host], timeout=60
            )
        if rc == 0 and out.strip():
            console.print(out.strip())
            return
        console.print(
            f"[yellow]mtr:[/yellow] {err.strip() or 'failed'} — trying traceroute."
        )
    if _have("traceroute"):
        rc, out, err = _run(["traceroute", host], timeout=60)
        if rc == 0:
            console.print(out.strip())
            return
        raise click.ClickException(err.strip() or "traceroute failed.")
    raise click.ClickException("neither 'mtr' nor 'traceroute' is available.")


@cli.command("lookup")
@click.argument("name")
@click.argument("server", required=False)
def cmd_lookup(name: str, server: str | None) -> None:
    """Resolve NAME (optionally via @SERVER) and show timing."""
    if server:
        server = server.lstrip("@")
        if _have("dig"):
            start = time.perf_counter()
            rc, out, err = _run(["dig", f"@{server}", name, "+short", "+time=3"])
            elapsed = (time.perf_counter() - start) * 1000
            if rc != 0:
                raise click.ClickException(err.strip() or "dig failed.")
            answers = [ln for ln in out.splitlines() if ln.strip()]
            console.print(
                f"[bold]{name}[/bold] via {server}  [dim]{elapsed:.0f}ms[/dim]"
            )
            for a in answers or ["[dim](no records)[/dim]"]:
                console.print(f"  {a}")
            return
        raise click.ClickException("'dig' is required to query a specific server.")
    start = time.perf_counter()
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror as exc:
        raise click.ClickException(f"resolution failed: {exc.strerror or exc}")
    elapsed = (time.perf_counter() - start) * 1000
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    console.print(f"[bold]{name}[/bold]  [dim]{elapsed:.0f}ms (system resolver)[/dim]")
    for a in addrs:
        console.print(f"  {a}")


@cli.command("mtu")
@click.argument("host")
def cmd_mtu(host: str) -> None:
    """Probe the path MTU to HOST (DF-bit binary search)."""
    if not _have("ping"):
        raise click.ClickException("'ping' is not available.")

    def reaches(payload: int) -> bool:
        rc, out, _ = _run(
            ["ping", "-c", "1", "-W", "2", "-M", "do", "-s", str(payload), host],
            timeout=8,
        )
        return rc == 0 and " 0% packet loss" in out

    if not reaches(0):
        raise click.ClickException(f"{host} is not reachable (or blocks ping).")
    lo, hi = 0, 9000
    with console.status(f"probing path MTU to {host}…"):
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if reaches(mid):
                lo = mid
            else:
                hi = mid - 1
    console.print(
        f"[bold]{host}[/bold] path MTU ≈ [bold green]{lo + 28}[/bold green] "
        f"[dim](payload {lo} + 28 IP/ICMP)[/dim]"
    )


@cli.command("pubip")
@click.option(
    "--direct", "-d", is_flag=True, help="Ignore proxy env vars; query directly."
)
def cmd_pubip(direct: bool) -> None:
    """Show this host's public IP and geo/ASN (makes one HTTP request).

    Honours proxy environment variables (HTTPS_PROXY / ALL_PROXY) by default,
    so behind a proxy it reports the egress IP; pass --direct to bypass them.
    """
    import httpx

    try:
        with console.status("querying ipinfo.io…"):
            r = httpx.get("https://ipinfo.io/json", timeout=8.0, trust_env=not direct)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001 - report any network/parse failure
        msg = f"could not reach the IP service: {exc}"
        if not direct and any(k in str(exc).lower() for k in ("proxy", "socks")):
            msg += "\nA proxy is set in your environment — try: usm net pubip --direct"
        raise click.ClickException(msg)
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="dim")
    table.add_column(style="bold")
    for key in ("ip", "hostname", "city", "region", "country", "org", "timezone"):
        if data.get(key):
            table.add_row(key, str(data[key]))
    console.print(table)


# Monitor -------------------------------------------------------------------


def _io_snapshot() -> dict[str, tuple[int, int]]:
    return {
        name: (c.bytes_recv, c.bytes_sent)
        for name, c in psutil.net_io_counters(pernic=True).items()
    }


def _rate_table(prev: dict, cur: dict, only: str | None) -> Table:
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("iface", style="bold cyan", no_wrap=True)
    table.add_column("rx/s", justify="right", no_wrap=True)
    table.add_column("tx/s", justify="right", no_wrap=True)
    table.add_column("rx total", justify="right", no_wrap=True, style="dim")
    table.add_column("tx total", justify="right", no_wrap=True, style="dim")
    for name in sorted(cur):
        if only and name != only:
            continue
        if name == "lo" and not only:
            continue
        rx_now, tx_now = cur[name]
        rx_old, tx_old = prev.get(name, (rx_now, tx_now))
        table.add_row(
            name,
            fmt_rate(max(0, rx_now - rx_old)),
            fmt_rate(max(0, tx_now - tx_old)),
            fmt_bytes(rx_now),
            fmt_bytes(tx_now),
        )
    return table


@cli.command("speed")
@click.argument("iface", required=False)
@click.option("--watch", "-w", is_flag=True, help="Refresh continuously.")
@click.option("--interval", "-i", default=1.0, show_default=True)
def cmd_speed(iface: str | None, watch: bool, interval: float) -> None:
    """Show per-interface throughput (one sample, or live with -w)."""
    interval = max(0.3, interval)
    if iface and iface not in psutil.net_io_counters(pernic=True):
        raise click.ClickException(f"no such interface: {iface}")
    if not watch:
        prev = _io_snapshot()
        time.sleep(interval)
        console.print(_rate_table(prev, _io_snapshot(), iface))
        return
    try:
        prev = _io_snapshot()
        with Live(console=console, screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                cur = _io_snapshot()
                live.update(_rate_table(prev, cur, iface))
                prev = cur
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
