#!/usr/bin/env python3
"""Quick / full machine benchmark — CPU, memory, disk, network, optional GPU.

  usm bench               # the default ~30s "quick" battery
  usm bench --quick
  usm bench --full        # adds longer disk + network tests (~2min)
  usm bench --no-net      # skip network (offline machines)
  usm bench --no-gpu      # skip GPU even if torch present

All numbers are best-effort. Real benchmarking is hard — treat these as smoke
tests for new VMs, not science.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click
import psutil
from rich.console import Console
from rich.table import Table

console = Console()


def _section(title: str, rows: list[tuple[str, str]]) -> Table:
    t = Table(title=title, header_style="bold", show_header=True, show_lines=False)
    t.add_column("metric")
    t.add_column("value", justify="right")
    for name, value in rows:
        t.add_row(name, value)
    return t


# CPU ---------------------------------------------------------------------


def bench_cpu(seconds: float = 3.0) -> list[tuple[str, str]]:
    iters = 0
    end = time.monotonic() + seconds
    x = 0.0
    while time.monotonic() < end:
        for _ in range(10_000):
            x = (x + 1.0) * 1.0000001
        iters += 10_000
    rate = iters / seconds / 1e6
    return [
        ("cpu model", platform.processor() or "?"),
        ("physical cores", str(psutil.cpu_count(logical=False))),
        ("logical cores", str(psutil.cpu_count(logical=True))),
        ("single-thread float ops", f"{rate:6.2f} Mops/s"),
        (
            "load 1m / 5m / 15m",
            " / ".join(f"{x:.2f}" for x in os.getloadavg())
            if hasattr(os, "getloadavg")
            else "n/a (windows)",
        ),
    ]


# Memory ------------------------------------------------------------------


def bench_mem(size_mb: int = 256) -> list[tuple[str, str]]:
    vm = psutil.virtual_memory()
    block = bytearray(size_mb * 1024 * 1024)
    start = time.perf_counter()
    copy = bytearray(block)  # noqa: F841
    dt = time.perf_counter() - start
    bw = size_mb / dt
    return [
        ("total", f"{vm.total / 1e9:6.2f} GB"),
        ("available", f"{vm.available / 1e9:6.2f} GB"),
        ("used / %", f"{vm.used / 1e9:5.2f} GB / {vm.percent:.0f}%"),
        (f"bytearray copy ({size_mb} MiB)", f"{bw:7.1f} MiB/s"),
    ]


# Disk --------------------------------------------------------------------


def bench_disk(size_mb: int = 256, sync_each: bool = True) -> list[tuple[str, str]]:
    tmp = Path(tempfile.gettempdir()) / "usm-bench.dat"
    size_bytes = size_mb * 1024 * 1024
    block = os.urandom(1024 * 1024)  # 1 MiB random
    # write
    start = time.perf_counter()
    with open(tmp, "wb", buffering=0) as f:
        for _ in range(size_mb):
            f.write(block)
        if sync_each:
            os.fsync(f.fileno())
    write_dt = time.perf_counter() - start
    # drop the page cache for this file so the read isn't just a RAM hit
    cache_dropped = False
    if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED"):
        try:
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                cache_dropped = True
            finally:
                os.close(fd)
        except OSError:
            pass
    # read
    start = time.perf_counter()
    with open(tmp, "rb", buffering=0) as f:
        while f.read(1024 * 1024):
            pass
    read_dt = time.perf_counter() - start
    tmp.unlink(missing_ok=True)
    read_label = (
        f"read  {size_mb} MiB" if cache_dropped else f"read  {size_mb} MiB (cached)"
    )
    return [
        ("path", str(tmp.parent)),
        (f"write {size_mb} MiB", f"{size_bytes / write_dt / 1e6:7.1f} MB/s"),
        (read_label, f"{size_bytes / read_dt / 1e6:7.1f} MB/s"),
    ]


# Network -----------------------------------------------------------------


def bench_net(samples: int = 3, host: str = "1.1.1.1") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if shutil.which("ping"):
        # Linux iputils -W is seconds; BSD/macOS -W is milliseconds.
        wait_flag = ["-W", "2000"] if sys.platform == "darwin" else ["-W", "2"]
        try:
            out = subprocess.check_output(
                ["ping", "-c", str(samples), *wait_flag, host],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            for line in out.splitlines():
                if "min/avg/max" in line:
                    rows.append((f"ping {host}", line.split("=")[1].strip()))
                    break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            rows.append((f"ping {host}", f"failed: {e}"))
    else:
        rows.append(("ping", "skipped (no ping binary)"))
    if shutil.which("curl"):
        url = "https://speed.cloudflare.com/__down?bytes=10000000"  # 10 MB
        try:
            out = subprocess.check_output(
                ["curl", "-o", "/dev/null", "-s", "-w", "%{speed_download}", url],
                text=True,
                timeout=20,
            )
            bps = float(out.strip() or 0)
            rows.append(("HTTPS download (10 MB)", f"{bps / 1e6:6.1f} MB/s"))
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
            OSError,
        ) as e:
            rows.append(("HTTPS download", f"failed: {e}"))
    else:
        rows.append(("HTTPS download", "skipped (no curl)"))
    return rows


# GPU ---------------------------------------------------------------------


def bench_gpu(size: int = 4096) -> list[tuple[str, str]]:
    try:
        import torch  # type: ignore
    except ImportError:
        return [("torch", "not installed")]
    if not torch.cuda.is_available():
        return [("torch", "installed, but no CUDA device")]
    dev = torch.device("cuda")
    name = torch.cuda.get_device_name(0)
    rows: list[tuple[str, str]] = [("device", name)]
    for dtype, label in ((torch.float32, "fp32"), (torch.float16, "fp16")):
        try:
            a = torch.randn((size, size), device=dev, dtype=dtype)
            b = torch.randn((size, size), device=dev, dtype=dtype)
            # warmup
            for _ in range(3):
                (a @ b).sum().item()
            torch.cuda.synchronize()
            iters = 5
            start = time.perf_counter()
            for _ in range(iters):
                c = a @ b  # noqa: F841
            torch.cuda.synchronize()
            dt = (time.perf_counter() - start) / iters
            flops = 2 * size**3 / dt
            rows.append(
                (f"matmul {size}x{size} {label}", f"{flops / 1e12:6.2f} TFLOPS")
            )
        except RuntimeError as e:
            rows.append((f"matmul {size}x{size} {label}", f"failed: {e}"))
    return rows


# CLI ---------------------------------------------------------------------


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Quick smoke benchmark of CPU/memory/disk/network/GPU.",
)
@click.option("--quick/--full", "quick", default=True, show_default=True)
@click.option("--no-cpu", is_flag=True)
@click.option("--no-mem", is_flag=True)
@click.option("--no-disk", is_flag=True)
@click.option("--no-net", is_flag=True)
@click.option("--no-gpu", is_flag=True)
def cli(quick, no_cpu, no_mem, no_disk, no_net, no_gpu):
    cpu_secs = 3.0 if quick else 8.0
    mem_mb = 256 if quick else 1024
    disk_mb = 256 if quick else 1024
    net_samples = 3 if quick else 10
    gpu_size = 4096 if quick else 8192

    sections: list[tuple[str, list[tuple[str, str]]]] = []
    if not no_cpu:
        console.print("[dim]benching cpu…[/dim]")
        sections.append(("CPU", bench_cpu(cpu_secs)))
    if not no_mem:
        console.print("[dim]benching memory…[/dim]")
        sections.append(("Memory", bench_mem(mem_mb)))
    if not no_disk:
        console.print(f"[dim]benching disk ({tempfile.gettempdir()})…[/dim]")
        sections.append(("Disk", bench_disk(disk_mb)))
    if not no_net:
        console.print("[dim]benching network…[/dim]")
        sections.append(("Network", bench_net(net_samples)))
    if not no_gpu:
        console.print("[dim]benching gpu (if torch+cuda)…[/dim]")
        sections.append(("GPU", bench_gpu(gpu_size)))
    for title, rows in sections:
        console.print(_section(title, rows))


if __name__ == "__main__":
    cli()
