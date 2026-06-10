#!/usr/bin/env python3
"""GPU inventory + picker + watch + kill, on top of nvidia-smi.

Examples:
  usm gpu                                 # one-shot summary
  usm gpu free                            # print index of the least-loaded GPU
  usm gpu free 2                          # CUDA_VISIBLE_DEVICES=$(usm gpu free 2)
  usm gpu watch                           # rich live refresh
  usm gpu kill 12345                      # SIGTERM a CUDA process
  usm gpu kill --user alice               # everything that user is running on any GPU
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
from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()
NVSMI = "nvidia-smi"
GPU_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit"
)
PROC_QUERY = "gpu_uuid,pid,process_name,used_memory"

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM


@dataclass
class Gpu:
    idx: int
    name: str
    util: int  # %
    mem_used: int  # MiB
    mem_total: int  # MiB
    temp: int  # C
    power: float  # W
    power_cap: float  # W

    def mem_pct(self) -> float:
        return 100.0 * self.mem_used / max(self.mem_total, 1)


@dataclass
class GpuProc:
    gpu_uuid: str
    pid: int
    name: str
    used_mem: int  # MiB
    user: str = "-"


def _require_nvsmi():
    if not shutil.which(NVSMI):
        raise click.ClickException(
            "nvidia-smi not found on PATH. Is the NVIDIA driver installed?"
        )


def _query_gpus() -> list[Gpu]:
    _require_nvsmi()
    out = subprocess.check_output(
        [NVSMI, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
        text=True,
    )
    gpus: list[Gpu] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 8:
            continue
        try:
            gpus.append(
                Gpu(
                    idx=int(parts[0]),
                    name=parts[1],
                    util=_int(parts[2]),
                    mem_used=_int(parts[3]),
                    mem_total=_int(parts[4]),
                    temp=_int(parts[5]),
                    power=_float(parts[6]),
                    power_cap=_float(parts[7]),
                )
            )
        except ValueError:
            continue
    return gpus


def _int(s: str) -> int:
    try:
        return int(float(s))
    except ValueError:
        return 0


def _float(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


def _query_procs() -> list[GpuProc]:
    _require_nvsmi()
    try:
        out = subprocess.check_output(
            [
                NVSMI,
                "--query-compute-apps=" + PROC_QUERY,
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    procs: list[GpuProc] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        user = "-"
        try:
            user = (
                subprocess.check_output(
                    ["ps", "-o", "user=", "-p", str(pid)], text=True
                ).strip()
                or "-"
            )
        except (subprocess.CalledProcessError, OSError):
            pass
        procs.append(GpuProc(parts[0], pid, parts[2], _int(parts[3]), user))
    return procs


def _uuid_to_index() -> dict[str, int]:
    out = subprocess.check_output(
        [NVSMI, "--query-gpu=index,uuid", "--format=csv,noheader"], text=True
    )
    m: dict[str, int] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            try:
                m[parts[1]] = int(parts[0])
            except ValueError:
                continue
    return m


def _color_util(v: int) -> str:
    if v >= 80:
        return f"[red]{v:>3}%[/red]"
    if v >= 30:
        return f"[yellow]{v:>3}%[/yellow]"
    return f"[green]{v:>3}%[/green]"


def _color_mem(used: int, total: int) -> str:
    pct = 100.0 * used / max(total, 1)
    txt = f"{used:>5}/{total} MiB ({pct:>3.0f}%)"
    if pct >= 80:
        return f"[red]{txt}[/red]"
    if pct >= 50:
        return f"[yellow]{txt}[/yellow]"
    return f"[green]{txt}[/green]"


def _render_table(gpus: list[Gpu], procs: list[GpuProc]) -> Table:
    table = Table(show_header=True, header_style="bold", title="GPU summary")
    table.add_column("idx", justify="right")
    table.add_column("name", overflow="fold")
    table.add_column("util", justify="right")
    table.add_column("memory", justify="right")
    table.add_column("temp", justify="right")
    table.add_column("power", justify="right")
    table.add_column("users", overflow="fold")
    uuid_to_idx = {}
    try:
        uuid_to_idx = _uuid_to_index()
    except subprocess.CalledProcessError:
        pass
    by_gpu: dict[int, list[GpuProc]] = {}
    for p in procs:
        idx = uuid_to_idx.get(p.gpu_uuid)
        if idx is None:
            continue
        by_gpu.setdefault(idx, []).append(p)
    for g in gpus:
        users = (
            ", ".join(
                f"{p.user}/pid{p.pid}({p.used_mem}MiB)" for p in by_gpu.get(g.idx, [])
            )
            or "[dim]free[/dim]"
        )
        table.add_row(
            str(g.idx),
            g.name,
            _color_util(g.util),
            _color_mem(g.mem_used, g.mem_total),
            f"{g.temp}°C",
            f"{g.power:.0f}/{g.power_cap:.0f} W",
            users,
        )
    return table


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="GPU inventory, free-picker, watch, and kill.",
)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        gpus = _query_gpus()
        procs = _query_procs()
        console.print(_render_table(gpus, procs))


@cli.command(
    "free",
    help=(
        "Print the index/indices of the least-loaded GPU(s). "
        "Use as CUDA_VISIBLE_DEVICES=$(usm gpu free 2)."
    ),
)
@click.argument("n", type=int, default=1)
@click.option(
    "--mem-threshold",
    type=int,
    default=500,
    show_default=True,
    help="Consider a GPU 'free' if memory used is below this many MiB.",
)
@click.option(
    "--util-threshold",
    type=int,
    default=10,
    show_default=True,
    help="…and utilization is below this %.",
)
def cmd_free(n, mem_threshold, util_threshold):
    gpus = _query_gpus()
    candidates = sorted(gpus, key=lambda g: (g.mem_used, g.util))
    free = [
        g for g in candidates if g.mem_used < mem_threshold and g.util < util_threshold
    ]
    chosen = free[:n] if free else candidates[:n]
    if not chosen:
        raise click.ClickException("No GPUs detected.")
    click.echo(",".join(str(g.idx) for g in chosen))


@cli.command("watch", help="Live-refresh the summary.")
@click.option(
    "-n",
    "--interval",
    type=float,
    default=1.0,
    show_default=True,
    help="Refresh interval in seconds.",
)
def cmd_watch(interval):
    try:
        with Live(
            _render_table(_query_gpus(), _query_procs()), refresh_per_second=4
        ) as live:
            while True:
                time.sleep(interval)
                live.update(_render_table(_query_gpus(), _query_procs()))
    except KeyboardInterrupt:
        pass


@cli.command("kill", help="Kill CUDA processes by PID or by user.")
@click.argument("target", required=False)
@click.option("--user", "user_filter", help="Kill every CUDA process owned by USER.")
@click.option("--force", is_flag=True, help="SIGKILL immediately.")
def cmd_kill(target, user_filter, force):
    procs = _query_procs()
    if not procs:
        console.print("[dim]No CUDA processes running.[/dim]")
        return
    sig = _SIGKILL if force else signal.SIGTERM
    victims: list[GpuProc] = []
    if user_filter:
        victims = [p for p in procs if p.user == user_filter]
    elif target:
        try:
            pid = int(target)
        except ValueError:
            victims = [p for p in procs if p.user == target]
        else:
            victims = [p for p in procs if p.pid == pid]
    else:
        raise click.UsageError("Pass a PID or --user NAME.")
    if not victims:
        console.print("[dim]No matching CUDA processes.[/dim]")
        return
    for v in victims:
        try:
            os.kill(v.pid, sig)
            console.print(
                f"[green]✓[/green] {sig.name} → pid {v.pid} ({v.user}, {v.name})"
            )
        except OSError as e:
            console.print(f"[red]✗[/red] pid {v.pid}: {e}", file=sys.stderr)


if __name__ == "__main__":
    cli()
