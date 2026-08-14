#!/usr/bin/env python3
"""One scannable health report for a machine.

Examples:
  usm doctor                 # offline, cheap checks
  usm doctor --only disk -v  # one check with details
  usm doctor --online --json # permit network probes and emit JSON
  usm doctor ls              # list available checks
"""

from __future__ import annotations

import errno
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import click
import psutil
from usmo import ui

Status = Literal["ok", "warn", "fail", "skip"]
CheckFunc = Callable[["DoctorContext"], "CheckResult"]

STATUS_RANK: dict[Status, int] = {"skip": 0, "ok": 1, "warn": 2, "fail": 3}
STATUS_STYLE = {"ok": "ok", "warn": "warn", "fail": "fail", "skip": "muted"}
PSEUDO_FS = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "overlay",
    "proc",
    "pstore",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


@dataclass(frozen=True)
class CheckResult:
    status: Status
    summary: str
    details: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    category: str
    func: CheckFunc
    slow: bool = False
    online_only: bool = False


@dataclass(frozen=True)
class DoctorContext:
    online: bool
    timeout: float


def result(
    status: Status, summary: str, details: Iterable[str] = (), **data
) -> CheckResult:
    return CheckResult(status, summary, list(details), data)


def worst(*statuses: Status) -> Status:
    return max(statuses or ("ok",), key=lambda s: STATUS_RANK[s])


def run(argv: list[str], timeout: float = 1.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _float(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def _int(text: str, default: int = 0) -> int:
    return int(_float(text, default))


def _pct_status(pct: float) -> Status:
    if pct >= 95:
        return "fail"
    if pct >= 85:
        return "warn"
    return "ok"


def check_disk(_ctx: DoctorContext) -> CheckResult:
    rows: list[dict[str, Any]] = []
    details: list[str] = []
    status: Status = "ok"
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:
        return result("skip", f"disk partitions unavailable: {exc}")
    seen: set[str] = set()
    for part in parts:
        if part.mountpoint in seen or part.fstype in PSEUDO_FS:
            continue
        seen.add(part.mountpoint)
        try:
            usage = shutil.disk_usage(part.mountpoint)
            used_pct = 100.0 * usage.used / max(usage.total, 1)
            stat = os.statvfs(part.mountpoint)
            inode_total = stat.f_files
            inode_used = max(0, stat.f_files - stat.f_ffree)
            inode_pct = 100.0 * inode_used / inode_total if inode_total else 0.0
        except OSError as exc:
            status = worst(status, "warn")
            details.append(f"{part.mountpoint}: unavailable ({exc.strerror or exc})")
            continue
        mount_status = worst(_pct_status(used_pct), _pct_status(inode_pct))
        status = worst(status, mount_status)
        rows.append(
            {
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "used_pct": round(used_pct, 1),
                "inode_pct": round(inode_pct, 1),
                "total": usage.total,
            }
        )
        details.append(
            f"{part.mountpoint} ({part.fstype}): {used_pct:.0f}% blocks, "
            f"{inode_pct:.0f}% inodes, {ui.human_bytes(usage.total)} total"
        )
    if not rows and not details:
        return result("skip", "no real filesystems found")
    bad = [r for r in rows if _pct_status(max(r["used_pct"], r["inode_pct"])) != "ok"]
    summary = f"{len(rows)} filesystem(s) checked"
    if bad:
        summary = ", ".join(
            f"{r['mount']} {max(r['used_pct'], r['inode_pct']):.0f}%" for r in bad[:3]
        )
    return result(status, summary, details, filesystems=rows)


def check_memory(ctx: DoctorContext) -> CheckResult:
    try:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
    except Exception as exc:
        return result("skip", f"memory stats unavailable: {exc}")
    avail_pct = 100.0 * vm.available / max(vm.total, 1)
    status: Status = "ok"
    if vm.available < 128 * 1024**2 or avail_pct < 2:
        status = "fail"
    elif vm.available < 512 * 1024**2 or avail_pct < 10:
        status = "warn"
    if swap.total and swap.percent >= 80:
        status = worst(status, "warn")
    details = [
        f"total {ui.human_bytes(vm.total)}, available {ui.human_bytes(vm.available)} ({avail_pct:.0f}%)",
        f"swap used {ui.human_bytes(swap.used)} / {ui.human_bytes(swap.total)}",
    ]
    oom_lines: list[str] = []
    if shutil.which("dmesg"):
        try:
            proc = run(
                ["dmesg", "--ctime", "--level=err,warn"], timeout=min(ctx.timeout, 1.0)
            )
            if proc.returncode == 0:
                oom_lines = [
                    line
                    for line in proc.stdout.splitlines()
                    if "oom" in line.lower() or "killed process" in line.lower()
                ][-5:]
            else:
                details.append("dmesg unreadable")
        except (OSError, subprocess.SubprocessError):
            details.append("dmesg unreadable")
    else:
        details.append("dmesg not installed")
    if oom_lines:
        status = worst(status, "warn")
        details.extend(oom_lines)
    return result(
        status,
        f"{ui.human_bytes(vm.available)} available of {ui.human_bytes(vm.total)}",
        details,
        total=vm.total,
        available=vm.available,
        swap_total=swap.total,
        swap_used=swap.used,
        oom_events=oom_lines,
    )


def check_load(_ctx: DoctorContext) -> CheckResult:
    if not hasattr(os, "getloadavg"):
        return result("skip", "load average unavailable on this platform")
    one, five, fifteen = os.getloadavg()
    cpus = os.cpu_count() or 1
    status: Status = "ok"
    if one > cpus * 3:
        status = "fail"
    elif one > cpus * 1.5:
        status = "warn"
    return result(
        status,
        f"{one:.2f} load on {cpus} CPU(s)",
        [f"1m {one:.2f}, 5m {five:.2f}, 15m {fifteen:.2f}"],
        load1=one,
        load5=five,
        load15=fifteen,
        cpus=cpus,
    )


def check_uptime(_ctx: DoctorContext) -> CheckResult:
    details: list[str] = []
    seconds: float | None = None
    try:
        seconds = float(Path("/proc/uptime").read_text().split()[0])
        details.append(f"uptime {ui.human_duration(seconds)}")
    except (OSError, ValueError, IndexError):
        try:
            seconds = time.time() - psutil.boot_time()
            details.append(f"uptime {ui.human_duration(seconds)}")
        except Exception:
            details.append("uptime unavailable")
    reboot = Path("/var/run/reboot-required").exists()
    if reboot:
        details.append("/var/run/reboot-required is present")
    return result(
        "warn" if reboot else "ok",
        "reboot required" if reboot else f"up {ui.human_duration(seconds)}",
        details,
        uptime_seconds=seconds,
        reboot_required=reboot,
    )


def _parse_timedatectl(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip().lower()] = value.strip()
    return out


def _http_date_skew(timeout: float) -> tuple[float | None, str | None]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request("https://www.google.com", method="HEAD"),
            timeout=timeout,
        ) as resp:
            date = resp.headers.get("Date")
    except Exception as exc:
        return None, f"HTTP Date unavailable: {exc}"
    if not date:
        return None, "HTTP Date header missing"
    from email.utils import parsedate_to_datetime

    try:
        skew = abs(parsedate_to_datetime(date).timestamp() - time.time())
    except (TypeError, ValueError) as exc:
        return None, f"HTTP Date unparsable: {exc}"
    return skew, None


def check_clock(ctx: DoctorContext) -> CheckResult:
    details: list[str] = []
    data: dict[str, Any] = {}
    status: Status = "ok"
    if shutil.which("timedatectl"):
        try:
            proc = run(["timedatectl"], timeout=min(ctx.timeout, 1.0))
            if proc.returncode == 0:
                fields = _parse_timedatectl(proc.stdout)
                synced = fields.get("system clock synchronized")
                details.extend(
                    f"{k}: {v}"
                    for k, v in fields.items()
                    if "synchronized" in k or "ntp" in k or k == "time zone"
                )
                data.update(fields)
                if synced is None:
                    status = "warn"
                    details.append("timedatectl output did not include sync state")
                elif synced.lower() not in {"yes", "true"}:
                    status = "warn"
            else:
                status = "skip"
                details.append((proc.stderr or "timedatectl failed").strip())
        except (OSError, subprocess.SubprocessError) as exc:
            return result("skip", f"timedatectl unavailable: {exc}")
    else:
        return result("skip", "timedatectl not installed")
    if ctx.online:
        skew, err = _http_date_skew(min(ctx.timeout, 2.0))
        data["http_date_skew_seconds"] = skew
        if err:
            details.append(err)
            status = worst(status, "warn")
        elif skew is not None:
            details.append(f"HTTP Date skew {skew:.1f}s")
            if skew > 5:
                status = worst(status, "warn")
    return result(
        status,
        "clock synchronized" if status == "ok" else "clock needs attention",
        details,
        **data,
    )


def _read_resolvers(path: Path = Path("/etc/resolv.conf")) -> list[str]:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return []
    return [
        line.split()[1]
        for line in lines
        if line.strip().startswith("nameserver") and len(line.split()) >= 2
    ]


def check_dns(ctx: DoctorContext) -> CheckResult:
    resolvers = _read_resolvers()
    details = [f"resolver {r}" for r in resolvers]
    if not ctx.online:
        return result(
            "ok" if resolvers else "warn",
            f"{len(resolvers)} resolver(s) configured",
            details,
            resolvers=resolvers,
            online=False,
        )
    failures: list[str] = []
    answers: dict[str, list[str]] = {}
    for name in ("github.com", "pypi.org"):
        try:
            infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
            addrs = sorted({info[4][0] for info in infos})
            answers[name] = addrs
            details.append(f"{name}: {', '.join(addrs[:3])}")
        except OSError as exc:
            failures.append(f"{name}: {exc}")
    details.extend(failures)
    return result(
        "fail" if failures else "ok",
        "DNS resolution failed" if failures else "DNS resolves test names",
        details,
        resolvers=resolvers,
        answers=answers,
        failures=failures,
        online=True,
    )


def _default_iface() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                return parts[0]
    except OSError:
        return None
    return None


def check_network(_ctx: DoctorContext) -> CheckResult:
    iface = _default_iface()
    if not iface:
        return result("fail", "no default route", default_interface=None)
    state_path = Path("/sys/class/net") / iface / "operstate"
    try:
        state = state_path.read_text().strip()
    except OSError:
        state = "unknown"
    status: Status = "ok" if state in {"up", "unknown"} else "fail"
    return result(
        status,
        f"default route via {iface} ({state})",
        [f"{iface} operstate {state}"],
        default_interface=iface,
        operstate=state,
    )


def _csv_lines(text: str) -> list[list[str]]:
    return [
        [p.strip() for p in line.split(",")]
        for line in text.splitlines()
        if line.strip()
    ]


def check_gpu(ctx: DoctorContext) -> CheckResult:
    if not shutil.which("nvidia-smi"):
        return result("skip", "nvidia-smi not found")
    try:
        gpu_proc = run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,driver_version,temperature.gpu,ecc.errors.uncorrected.volatile.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=min(ctx.timeout, 2.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return result("skip", f"nvidia-smi unavailable: {exc}")
    if gpu_proc.returncode != 0 or not gpu_proc.stdout.strip():
        return result("skip", (gpu_proc.stderr or "no NVIDIA GPUs found").strip())
    gpus: list[dict[str, Any]] = []
    details: list[str] = []
    status: Status = "ok"
    for row in _csv_lines(gpu_proc.stdout):
        if len(row) < 5:
            continue
        temp = _int(row[3])
        ecc = _int(row[4])
        gpu_status: Status = (
            "fail" if ecc > 0 or temp >= 90 else "warn" if temp >= 80 else "ok"
        )
        status = worst(status, gpu_status)
        item = {
            "index": _int(row[0]),
            "name": row[1],
            "driver": row[2],
            "temperature_c": temp,
            "ecc_uncorrectable": ecc,
        }
        gpus.append(item)
        details.append(
            f"GPU {row[0]} {row[1]}: driver {row[2]}, {temp}°C, ECC uncorrectable {ecc}"
        )
    processes: list[dict[str, Any]] = []
    try:
        proc_proc = run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=min(ctx.timeout, 2.0),
        )
        if proc_proc.returncode == 0:
            for row in _csv_lines(proc_proc.stdout):
                if len(row) >= 4:
                    processes.append(
                        {
                            "gpu_uuid": row[0],
                            "pid": _int(row[1]),
                            "name": row[2],
                            "used_memory_mib": _int(row[3]),
                        }
                    )
            if processes:
                details.append(f"{len(processes)} compute process(es) using GPU memory")
    except (OSError, subprocess.SubprocessError):
        details.append("GPU process query unavailable")
    if not gpus:
        return result("skip", "no NVIDIA GPUs found")
    return result(
        status, f"{len(gpus)} NVIDIA GPU(s)", details, gpus=gpus, processes=processes
    )


def _mounts_from_mountinfo(
    path: Path = Path("/proc/self/mountinfo"),
) -> list[tuple[str, str]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    mounts: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split()
        if "-" not in parts or len(parts) < 10:
            continue
        sep = parts.index("-")
        mountpoint = parts[4].replace("\\040", " ")
        fstype = parts[sep + 1]
        if fstype not in PSEUDO_FS:
            mounts.append((mountpoint, fstype))
    return mounts


def _bounded_stat(path: str, timeout: float) -> tuple[bool, BaseException | None]:
    q: queue.Queue[tuple[bool, BaseException | None]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            os.stat(path)
            q.put((True, None))
        except BaseException as exc:  # noqa: BLE001 - returned as check data
            q.put((False, exc))

    # A dead network filesystem can block in the kernel; the daemon thread lets
    # the report continue even though Python cannot cancel that syscall.
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return False, TimeoutError("stat timed out")


def check_mounts(ctx: DoctorContext) -> CheckResult:
    mounts = _mounts_from_mountinfo()
    if not mounts:
        return result("skip", "mount table unavailable")
    details: list[str] = []
    broken: list[dict[str, str]] = []
    status: Status = "ok"
    for mountpoint, fstype in mounts:
        ok, exc = _bounded_stat(mountpoint, min(0.2, max(ctx.timeout, 0.01)))
        if ok:
            continue
        code = getattr(exc, "errno", None)
        if isinstance(exc, TimeoutError):
            status = worst(status, "warn")
            reason = "stat timed out"
        elif code == errno.ENOTCONN:
            status = worst(status, "fail")
            reason = "transport endpoint not connected"
        elif code == errno.EACCES:
            status = worst(status, "warn")
            reason = "permission denied"
        else:
            status = worst(status, "warn")
            reason = str(exc)
        broken.append({"mountpoint": mountpoint, "fstype": fstype, "reason": reason})
        details.append(f"{mountpoint} ({fstype}): {reason}")
    return result(
        status,
        "mount probes clean"
        if not broken
        else f"{len(broken)} mount(s) need attention",
        details,
        checked=len(mounts),
        broken=broken,
    )


def _failed_systemctl(
    args: list[str], timeout: float
) -> tuple[str, list[str], str | None]:
    try:
        proc = run(
            ["systemctl", *args, "--failed", "--no-legend", "--plain"], timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "skip", [], str(exc)
    text = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode not in {0, 1} and not text:
        return "skip", [], err or "systemctl failed"
    lines = [line for line in text.splitlines() if line.strip()]
    return "fail" if lines else "ok", lines, None


def check_services(ctx: DoctorContext) -> CheckResult:
    if not shutil.which("systemctl"):
        return result("skip", "systemctl not found")
    details: list[str] = []
    data: dict[str, Any] = {}
    status: Status = "ok"
    user_status, user_lines, user_err = _failed_systemctl(
        ["--user"], min(ctx.timeout, 2.0)
    )
    data["user_failed"] = user_lines
    if user_err:
        details.append(f"user units skipped: {user_err}")
    else:
        details.extend(f"user: {line}" for line in user_lines)
    if user_status == "fail":
        status = "fail"
    system_status, system_lines, system_err = _failed_systemctl(
        [], min(ctx.timeout, 2.0)
    )
    data["system_failed"] = system_lines
    if system_err:
        details.append(f"system units skipped: {system_err}")
    else:
        details.extend(f"system: {line}" for line in system_lines)
    if system_status == "fail":
        status = "fail"
    if user_status == "skip" and system_status == "skip":
        return result("skip", "systemd unavailable", details, **data)
    count = len(user_lines) + len(system_lines)
    return result(
        status,
        "no failed units" if count == 0 else f"{count} failed unit(s)",
        details,
        **data,
    )


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def check_usm(_ctx: DoctorContext) -> CheckResult:
    try:
        from usmo.core import catalog, constants
    except Exception as exc:
        return result("skip", f"usmo core unavailable: {exc}")
    cache = constants.CACHE_DIR
    scripts_dir = constants.CACHE_SCRIPT_DIR
    envs_dir = constants.CACHE_ENV_DIR
    size = _dir_size(cache) if cache.exists() else 0
    try:
        venvs = (
            [p.name for p in envs_dir.iterdir() if p.is_dir()]
            if envs_dir.exists()
            else []
        )
    except OSError:
        venvs = []
    if not catalog.has_cached_config():
        return result(
            "skip",
            "usm catalog is not cached",
            [f"cache {ui.shorten_path(cache)}", f"cache size {ui.human_bytes(size)}"],
            cache_size=size,
            venv_count=len(venvs),
            mismatches=[],
        )
    mismatches: list[str] = []
    try:
        scripts = catalog.load_scripts()
        for name, script in scripts.items():
            if not catalog.script_files_match(script):
                mismatches.append(name)
    except Exception as exc:
        return result(
            "warn",
            f"could not verify catalog hashes: {exc}",
            cache_size=size,
            venv_count=len(venvs),
            mismatches=[],
        )
    details = [
        f"cache {ui.shorten_path(cache)}",
        f"scripts {ui.shorten_path(scripts_dir)}",
        f"cache size {ui.human_bytes(size)}",
        f"{len(venvs)} per-script venv(s)",
    ]
    if mismatches:
        details.append("hash mismatches: " + ", ".join(mismatches))
    return result(
        "fail" if mismatches else "ok",
        f"cache {ui.human_bytes(size)}, {len(venvs)} venv(s)",
        details,
        cache_size=size,
        venv_count=len(venvs),
        mismatches=mismatches,
    )


def check_python(_ctx: DoctorContext) -> CheckResult:
    local_bin = str(Path.home() / ".local" / "bin")
    on_path = local_bin in os.environ.get("PATH", "").split(os.pathsep)
    marker = Path(sysconfig.get_paths().get("stdlib", "")) / "EXTERNALLY-MANAGED"
    externally_managed = marker.exists()
    details = [f"executable {sys.executable}", f"version {platform.python_version()}"]
    if externally_managed:
        details.append(f"externally managed marker {marker}")
    details.append(f"{local_bin} {'is' if on_path else 'is not'} on PATH")
    return result(
        "ok" if on_path else "warn",
        f"Python {platform.python_version()}",
        details,
        version=platform.python_version(),
        executable=sys.executable,
        externally_managed=externally_managed,
        local_bin_on_path=on_path,
    )


CHECKS: tuple[Check, ...] = (
    Check("disk", "Filesystem usage", "storage", check_disk),
    Check("memory", "Memory and OOM", "system", check_memory),
    Check("load", "Load average", "system", check_load),
    Check("uptime", "Uptime / reboot required", "system", check_uptime),
    Check("clock", "Clock sync", "system", check_clock),
    Check("dns", "DNS", "network", check_dns),
    Check("network", "Default route", "network", check_network),
    Check("gpu", "NVIDIA GPU", "hardware", check_gpu),
    Check("mounts", "Mount health", "storage", check_mounts),
    Check("services", "Failed systemd units", "services", check_services),
    Check("usm", "usm cache", "usm", check_usm),
    Check("python", "Python environment", "usm", check_python),
)
CHECK_BY_ID = {check.id: check for check in CHECKS}


def select_checks(
    only: tuple[str, ...], skip: tuple[str, ...], category: str | None
) -> list[Check]:
    unknown = sorted((set(only) | set(skip)) - set(CHECK_BY_ID))
    if unknown:
        raise click.ClickException(f"unknown check id: {unknown[0]}")
    categories = {check.category for check in CHECKS}
    if category and category not in categories:
        raise click.ClickException(f"unknown category: {category}")
    selected = [CHECK_BY_ID[item] for item in only] if only else list(CHECKS)
    if category:
        selected = [check for check in selected if check.category == category]
    if skip:
        skipped = set(skip)
        selected = [check for check in selected if check.id not in skipped]
    return selected


def run_one(check: Check, ctx: DoctorContext) -> CheckResult:
    if check.online_only and not ctx.online:
        return result("skip", "requires --online")
    q: queue.Queue[CheckResult] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            q.put(check.func(ctx))
        except Exception as exc:  # noqa: BLE001 - one failed check must not abort the report
            q.put(result("fail", f"check raised: {exc.__class__.__name__}: {exc}"))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        return q.get(timeout=max(ctx.timeout, 0.01))
    except queue.Empty:
        return result("warn", f"timed out after {ctx.timeout:g}s")


def run_checks(
    checks: Iterable[Check], ctx: DoctorContext
) -> list[tuple[Check, CheckResult]]:
    return [(check, run_one(check, ctx)) for check in checks]


def _status_text(status: Status) -> str:
    label = "err" if status == "fail" else status
    return ui.status(STATUS_STYLE[status], label)


#: One glyph per status, from the shared vocabulary. A report is meant to be
#: scanned down the left edge, not read.
STATUS_GLYPH = {
    "ok": ui.OK,
    "warn": ui.WARN,
    "fail": ui.FAIL,
    "skip": ui.STOPPED,
}


def _glyph(status: Status) -> str:
    return ui.status(STATUS_STYLE[status], STATUS_GLYPH.get(status, "?"))


def render_report(results: list[tuple[Check, CheckResult]], *, verbose: bool) -> None:
    """One table for the whole machine, grouped but not fragmented.

    A table per category repeats the header four times and turns a health
    report into a wall; the category earns a column instead, printed only
    when it changes.
    """
    if not results:
        ui.hint("No checks selected.")
        return

    ordered = sorted(results, key=lambda pair: (pair[0].category, pair[0].id))
    columns = [
        ui.Column("", justify="center", min_width=1),
        ui.Column("check", style=ui.STYLE_ID, min_width=7),
        ui.Column("area", style=ui.STYLE_MUTED, min_width=7, hide_below=64),
        ui.Column("summary", min_width=20, ratio=1),
    ]
    table = ui.table(*columns, title="Health")
    previous = None
    for check, result in ordered:
        area = check.category if check.category != previous else ""
        previous = check.category
        table.add_row(
            *ui.row_for(
                columns,
                {
                    "": _glyph(result.status),
                    "check": check.id,
                    "area": area,
                    "summary": result.summary,
                },
            )
        )
    ui.print(table)

    detail_rows: list[tuple[str, str] | object] = []
    for check, result in ordered:
        if result.details and (verbose or result.status in {"warn", "fail"}):
            detail_rows.append((ui.SECTION, check.id))
            detail_rows.extend(("", line) for line in result.details)
    if detail_rows:
        ui.print_detail(detail_rows)

    counts = {}
    for _, result in ordered:
        counts[result.status] = counts.get(result.status, 0) + 1
    parts = [
        f"{counts[name]} {label}"
        for name, label in (
            ("fail", "failed"),
            ("warn", "warning"),
            ("ok", "ok"),
            ("skip", "skipped"),
        )
        if counts.get(name)
    ]
    ui.hint(ui.joined(*parts))


def results_json(results: list[tuple[Check, CheckResult]]) -> dict[str, Any]:
    return {
        "checks": [
            {
                "id": check.id,
                "title": check.title,
                "category": check.category,
                "slow": check.slow,
                "online_only": check.online_only,
                "status": check_result.status,
                "summary": check_result.summary,
                "details": check_result.details,
                "data": check_result.data,
            }
            for check, check_result in results
        ]
    }


def exit_code(results: list[tuple[Check, CheckResult]], *, strict: bool) -> int:
    statuses = [check_result.status for _, check_result in results]
    if "fail" in statuses or (strict and "warn" in statuses):
        return 1
    return 0


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Run cheap, read-only machine health checks.",
)
@click.option(
    "--only", "only", multiple=True, help="Run only this check id (repeatable)."
)
@click.option("--skip", "skip", multiple=True, help="Skip this check id (repeatable).")
@click.option("--category", help="Run checks in this category only.")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--online", is_flag=True, help="Permit network probes.")
@click.option(
    "--timeout",
    type=float,
    default=2.0,
    show_default=True,
    help="Per-check timeout in seconds.",
)
@click.option("--strict", is_flag=True, help="Exit non-zero on warnings too.")
@click.option(
    "-v", "--verbose", is_flag=True, help="Show details for passing checks too."
)
@click.pass_context
def cli(
    ctx: click.Context,
    only: tuple[str, ...],
    skip: tuple[str, ...],
    category: str | None,
    json_output: bool,
    online: bool,
    timeout: float,
    strict: bool,
    verbose: bool,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    checks = select_checks(only, skip, category)
    results = run_checks(checks, DoctorContext(online=online, timeout=timeout))
    if json_output:
        ui.print(json.dumps(results_json(results), sort_keys=True), soft_wrap=True)
    else:
        render_report(results, verbose=verbose)
    raise SystemExit(exit_code(results, strict=strict))


@cli.command(
    "ls",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="List available doctor checks.",
)
def ls_checks() -> None:
    table = ui.table(
        ui.Column("ID", min_width=8, style="bold cyan"),
        ui.Column("category", min_width=10),
        ui.Column("slow", justify="center"),
        ui.Column("online", justify="center"),
        ui.Column("title", min_width=20, ratio=1),
    )
    for check in CHECKS:
        table.add_row(
            check.id,
            check.category,
            ui.state(check.slow),
            ui.state(check.online_only),
            check.title,
        )
    ui.print(table)


if __name__ == "__main__":  # pragma: no cover
    cli()
