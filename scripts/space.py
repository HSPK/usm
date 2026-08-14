#!/usr/bin/env python3
"""Find what is eating disk space and reclaim safe caches.

Examples:
  usm space                         # filesystem + reclaimable cache summary
  usm space --json                  # scriptable summary
  usm space top . -n 20 --depth 2   # largest entries under the current tree
  usm space caches                  # known caches and reclaim commands
  usm space reclaim --only pip      # dry-run unless confirmed or --yes
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import click
import psutil
from usmo import ui

CONTEXT = {"help_option_names": ["-h", "--help"]}
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
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "rpc_pipefs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


@dataclass(frozen=True)
class SizeResult:
    bytes: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReclaimResult:
    freed: int
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cache:
    name: str
    description: str
    size: Callable[[], SizeResult]
    reclaim: Callable[[bool], ReclaimResult]
    paths: Callable[[], list[Path]] = lambda: []
    command: tuple[str, ...] | None = None
    requires_sudo: bool = False


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)


def _home() -> Path:
    return Path.home()


def _expand(path: Path | str) -> Path:
    return Path(path).expanduser()


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def path_size(path: Path) -> SizeResult:
    path = _expand(path)
    root_stat = _safe_lstat(path)
    if root_stat is None:
        return SizeResult(0)
    root_dev = root_stat.st_dev
    errors: list[str] = []
    seen: set[tuple[int, int]] = set()

    def walk(item: Path, stat: os.stat_result) -> int:
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            return 0
        seen.add(key)
        total = stat.st_size
        if not os.path.isdir(item) or os.path.islink(item):
            return total
        if stat.st_dev != root_dev:
            return 0
        try:
            with os.scandir(item) as entries:
                for entry in entries:
                    try:
                        child_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except (PermissionError, OSError) as exc:
                        errors.append(f"{entry.path}: {exc}")
                        continue
                    total += walk(Path(entry.path), child_stat)
        except FileNotFoundError:
            return 0
        except (PermissionError, OSError) as exc:
            errors.append(f"{item}: {exc}")
        return total

    return SizeResult(walk(path, root_stat), tuple(errors))


def _paths_size(paths: Iterable[Path]) -> SizeResult:
    total = 0
    errors: list[str] = []
    for path in paths:
        result = path_size(path)
        total += result.bytes
        errors.extend(result.errors)
    return SizeResult(total, tuple(errors))


def _existing(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if p.exists() or p.is_symlink()]


def _is_dangerous(path: Path) -> bool:
    if not str(path) or not path.is_absolute() or ".." in path.parts:
        return True
    home = _home().resolve()
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser().absolute()
    if resolved in {Path("/"), home}:
        return True
    protected = (Path("/etc"), Path("/usr"), Path("/var"))
    return any(resolved == root or resolved.is_relative_to(root) for root in protected)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path_resolved = path.expanduser().resolve(strict=False)
        root_resolved = root.expanduser().resolve(strict=False)
    except OSError:
        return False
    return path_resolved == root_resolved or path_resolved.is_relative_to(root_resolved)


def _safe_to_delete(path: Path, roots: Iterable[Path]) -> bool:
    if path.is_symlink():
        return False
    return not _is_dangerous(path) and any(_is_under(path, root) for root in roots)


def _delete_path(path: Path) -> tuple[int, list[str]]:
    before = path_size(path).bytes
    failures: list[str] = []

    def unlink_tree(item: Path) -> None:
        try:
            item.lstat()
        except FileNotFoundError:
            return
        except (PermissionError, OSError) as exc:
            failures.append(f"{item}: {exc}")
            return
        if not os.path.isdir(item) or os.path.islink(item):
            try:
                item.unlink()
            except FileNotFoundError:
                pass
            except (PermissionError, OSError) as exc:
                failures.append(f"{item}: {exc}")
            return
        try:
            children = list(item.iterdir())
        except FileNotFoundError:
            return
        except (PermissionError, OSError) as exc:
            failures.append(f"{item}: {exc}")
            return
        for child in children:
            unlink_tree(child)
        try:
            item.rmdir()
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            failures.append(f"{item}: {exc}")

    unlink_tree(path)
    after = path_size(path).bytes
    return max(0, before - after), failures


def remove_paths(
    paths_func: Callable[[], list[Path]],
) -> Callable[[bool], ReclaimResult]:
    def reclaim(apply: bool) -> ReclaimResult:
        paths = _existing(paths_func())
        if not apply:
            return ReclaimResult(0)
        roots = list(paths)
        freed = 0
        failures: list[str] = []
        for path in paths:
            if not _safe_to_delete(path, roots):
                failures.append(f"refused dangerous path: {path}")
                continue
            gained, failed = _delete_path(path)
            freed += gained
            failures.extend(failed)
        return ReclaimResult(freed, tuple(failures))

    return reclaim


def command_size(argv: list[str], parser: Callable[[str], int]) -> SizeResult:
    if not shutil.which(argv[0]):
        return SizeResult(0, (f"{argv[0]} not on PATH",))
    try:
        proc = _run(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SizeResult(0, (str(exc),))
    if proc.returncode != 0:
        return SizeResult(
            0, ((proc.stderr or proc.stdout or "command failed").strip(),)
        )
    return SizeResult(parser(proc.stdout))


def run_command(
    argv: list[str], size_func: Callable[[], SizeResult]
) -> Callable[[bool], ReclaimResult]:
    def reclaim(apply: bool) -> ReclaimResult:
        if not apply:
            return ReclaimResult(0)
        if not shutil.which(argv[0]):
            return ReclaimResult(0, (f"{argv[0]} not on PATH",))
        before = size_func().bytes
        try:
            proc = _run(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ReclaimResult(0, (str(exc),))
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "command failed").strip()
            return ReclaimResult(0, (msg,))
        after = size_func().bytes
        return ReclaimResult(max(0, before - after))

    return reclaim


def _parse_first_size(text: str) -> int:
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
    }
    import re

    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B?|B)", text, re.I)
    if not match:
        return 0
    return int(float(match.group(1)) * units.get(match.group(2).lower(), 1))


def _docker_size(text: str) -> int:
    import re

    total = 0
    for line in text.splitlines():
        if "reclaimable" not in line.lower():
            continue
        matches = re.findall(
            r"([0-9]+(?:\.[0-9]+)?\s*[KMGT]i?B?|[0-9]+\s*B)", line, re.I
        )
        if matches:
            total += _parse_first_size(matches[-1])
    return total


def _go_cache_path() -> Path | None:
    if not shutil.which("go"):
        return None
    try:
        proc = _run(["go", "env", "GOCACHE"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return Path(text) if text else None


def _conda_pkg_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("CONDA_PKGS_DIRS")
    if env:
        paths.extend(Path(p) for p in env.split(os.pathsep) if p)
    home = _home()
    paths.extend(
        [
            home / "miniconda3" / "pkgs",
            home / "anaconda3" / "pkgs",
            home / "mambaforge" / "pkgs",
            home / ".conda" / "pkgs",
            home / ".mamba" / "pkgs",
        ]
    )
    seen: set[Path] = set()
    uniq: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            uniq.append(path)
    return uniq


def _hf_hub_path() -> Path:
    hf_home = os.environ.get("HF_HOME")
    if hf_home and Path(hf_home).expanduser().resolve(strict=False) == Path("/"):
        return Path("/")
    return (Path(hf_home) if hf_home else _home() / ".cache" / "huggingface") / "hub"


def _pycache_targets(root: Path) -> list[Path]:
    root = root.expanduser()
    if _is_dangerous(root):
        return []
    targets: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            if current.name == "__pycache__":
                targets.append(current)
                dirnames[:] = []
                continue
            for filename in filenames:
                if filename.endswith(".pyc"):
                    targets.append(current / filename)
    except (PermissionError, OSError):
        return targets
    return targets


def cache_registry(pycache_root: Path | None = None) -> list[Cache]:
    home = _home()
    entries: list[tuple[str, str, Callable[[], list[Path]], bool]] = [
        ("pip", "pip download/build cache", lambda: [home / ".cache" / "pip"], False),
        ("uv", "uv package and build cache", lambda: [home / ".cache" / "uv"], False),
        (
            "npm",
            "npm content-addressed cache",
            lambda: [home / ".npm" / "_cacache"],
            False,
        ),
        (
            "yarn",
            "yarn package caches",
            lambda: [home / ".cache" / "yarn", home / ".yarn" / "cache"],
            False,
        ),
        (
            "cargo",
            "cargo registry cache",
            lambda: [home / ".cargo" / "registry"],
            False,
        ),
        ("huggingface", "HuggingFace hub cache", lambda: [_hf_hub_path()], False),
        (
            "torch",
            "torch hub/checkpoint cache",
            lambda: [home / ".cache" / "torch"],
            False,
        ),
        ("conda", "conda/mamba package tarballs", _conda_pkg_paths, False),
        (
            "apt",
            "apt package archives",
            lambda: [Path("/var/cache/apt/archives")],
            True,
        ),
        (
            "usm-envs",
            "usm per-script virtualenvs",
            lambda: [home / ".cache" / "usm" / "envs"],
            False,
        ),
        (
            "trash",
            "desktop trash",
            lambda: [home / ".local" / "share" / "Trash"],
            False,
        ),
    ]
    caches = [
        Cache(
            name=name,
            description=desc,
            size=lambda pf=paths: _paths_size(_existing(pf())),
            reclaim=remove_paths(paths),
            paths=paths,
            requires_sudo=sudo,
        )
        for name, desc, paths, sudo in entries
    ]
    go_path = _go_cache_path()
    if go_path is not None:

        def go_paths(p: Path = go_path) -> list[Path]:
            return [p]

        caches.append(
            Cache(
                "go",
                "Go build cache",
                lambda: _paths_size(_existing(go_paths())),
                remove_paths(go_paths),
                go_paths,
            )
        )
    caches.extend(
        [
            Cache(
                "journald",
                "systemd journal logs",
                lambda: command_size(["journalctl", "--disk-usage"], _parse_first_size),
                run_command(
                    ["journalctl", "--vacuum-size=100M"],
                    lambda: command_size(
                        ["journalctl", "--disk-usage"], _parse_first_size
                    ),
                ),
                command=("journalctl", "--vacuum-size=100M"),
            ),
            Cache(
                "docker",
                "unused Docker images, containers, networks, and build cache",
                lambda: command_size(["docker", "system", "df"], _docker_size),
                run_command(
                    ["docker", "system", "prune", "-af"],
                    lambda: command_size(["docker", "system", "df"], _docker_size),
                ),
                command=("docker", "system", "prune", "-af"),
            ),
        ]
    )
    if pycache_root is not None:

        def pycache_paths(root: Path = pycache_root) -> list[Path]:
            return _pycache_targets(root)

        caches.append(
            Cache(
                "pycache",
                "__pycache__ and .pyc under requested root",
                lambda: _paths_size(_existing(pycache_paths())),
                remove_paths(pycache_paths),
                pycache_paths,
            )
        )
    return caches


def _cache_rows(caches: list[Cache]) -> list[dict[str, object]]:
    rows = []
    for cache in caches:
        paths = _existing(cache.paths())
        sized = cache.size()
        command_exists = cache.command is not None and shutil.which(cache.command[0])
        if sized.bytes <= 0 and not paths and not command_exists:
            continue
        reclaim = "sudo required" if cache.requires_sudo else "remove paths"
        if cache.command:
            reclaim = " ".join(cache.command)
        rows.append(
            {
                "name": cache.name,
                "description": cache.description,
                "bytes": sized.bytes,
                "size": ui.human_bytes(sized.bytes),
                "paths": [str(p) for p in paths],
                "reclaim": reclaim,
                "requires_sudo": cache.requires_sudo,
                "errors": list(sized.errors),
            }
        )
    rows.sort(key=lambda row: int(row["bytes"]), reverse=True)
    return rows


def _print_cache_table(
    rows: list[dict[str, object]], *, title: str = "Reclaimable caches"
) -> None:
    if not rows:
        ui.hint("No known reclaimable caches found.")
        return
    columns = [
        ui.Column("name", style="bold cyan"),
        ui.Column("size", justify="right"),
        ui.Column("reclaim", hide_below=72),
        ui.Column("path", ratio=1, hide_below=56),
    ]
    table = ui.table(*columns, title=title)
    for row in rows:
        paths = row["paths"] or []
        path_text = ", ".join(ui.shorten_path(p) for p in paths) if paths else "-"
        table.add_row(
            *ui.row_for(
                columns,
                {
                    "name": row["name"],
                    "size": row["size"],
                    "reclaim": row["reclaim"],
                    "path": path_text,
                },
            )
        )
    ui.print(table)


def _filesystem_rows(show_all: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for part in psutil.disk_partitions(all=show_all):
        if not show_all and (
            part.fstype in PSEUDO_FS
            or part.device.startswith("/dev/loop")
            or "snap" in part.mountpoint
        ):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        rows.append(
            {
                "filesystem": part.device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            }
        )
    rows.sort(key=lambda row: str(row["mount"]))
    return rows


def _print_filesystems(rows: list[dict[str, object]]) -> None:
    if not rows:
        ui.hint("No filesystems to show.")
        return
    columns = [
        ui.Column("mount", ratio=1),
        ui.Column("used", justify="right"),
        ui.Column("free", justify="right"),
        ui.Column("Use%", justify="right"),
        ui.Column("FS", hide_below=76),
        ui.Column("device", hide_below=92),
    ]
    table = ui.table(*columns, title="Filesystems")
    for row in rows:
        table.add_row(
            *ui.row_for(
                columns,
                {
                    "mount": ui.shorten_path(row["mount"]),
                    "used": ui.human_bytes(row["used"]),
                    "free": ui.human_bytes(row["free"]),
                    "use%": f"{row['percent']:.0f}%",
                    "FS": row["fstype"],
                    "device": row["filesystem"],
                },
            )
        )
    ui.print(table)


def _top_entries(root: Path, max_depth: int) -> list[tuple[Path, int]]:
    root = root.expanduser()
    root_stat = _safe_lstat(root)
    if root_stat is None:
        return []
    root_dev = root_stat.st_dev
    rows: list[tuple[Path, int]] = []

    def walk(path: Path, depth: int) -> int:
        stat = _safe_lstat(path)
        if stat is None:
            return 0
        total = stat.st_size
        if os.path.isdir(path) and not os.path.islink(path) and stat.st_dev == root_dev:
            try:
                children = list(path.iterdir())
            except (FileNotFoundError, PermissionError, OSError):
                children = []
            for child in children:
                total += walk(child, depth + 1)
        if depth > 0 and depth <= max_depth:
            rows.append((path, total))
        return total

    if not os.path.isdir(root) or os.path.islink(root):
        rows.append((root, root_stat.st_size))
    else:
        walk(root, 0)
    rows.sort(key=lambda item: (-item[1], str(item[0])))
    return rows


@click.group(
    invoke_without_command=True,
    context_settings=CONTEXT,
    help="Find and reclaim disk space.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include pseudo, tmpfs, loop, and snap filesystems.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit scriptable JSON.")
@click.pass_context
def cli(ctx: click.Context, show_all: bool, json_output: bool) -> None:
    if ctx.invoked_subcommand is not None:
        return
    fs_rows = _filesystem_rows(show_all)
    cache_rows = _cache_rows(cache_registry())
    total = sum(int(row["bytes"]) for row in cache_rows)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "filesystems": fs_rows,
                    "caches": cache_rows,
                    "reclaimable_bytes": total,
                },
                default=str,
            )
        )
        return
    _print_filesystems(fs_rows)
    _print_cache_table(cache_rows)
    ui.ok(f"reclaimable: {ui.human_bytes(total)}")


@cli.command(
    "top", context_settings=CONTEXT, help="Show largest directories/files under PATH."
)
@click.argument(
    "path", type=click.Path(path_type=Path), default=Path("."), required=False
)
@click.option(
    "-n", "limit", type=int, default=20, show_default=True, help="Number of rows."
)
@click.option(
    "--depth", type=int, default=2, show_default=True, help="Maximum depth to list."
)
def cmd_top(path: Path, limit: int, depth: int) -> None:
    if not path.expanduser().exists() and not path.expanduser().is_symlink():
        raise click.ClickException(f"path does not exist: {path}")
    rows = _top_entries(path, max(1, depth))[: max(0, limit)]
    if not rows:
        ui.hint("No entries found.")
        return
    columns = [ui.Column("size", justify="right"), ui.Column("path", ratio=1)]
    table = ui.table(*columns, title=f"Largest under {ui.shorten_path(path)}")
    for entry, size in rows:
        try:
            shown = entry.relative_to(path.expanduser())
        except ValueError:
            shown = entry
        table.add_row(
            *ui.row_for(
                columns, {"size": ui.human_bytes(size), "path": ui.shorten_path(shown)}
            )
        )
    ui.print(table)


@cli.command("caches", context_settings=CONTEXT, help="List known reclaimable caches.")
@click.option("--json", "json_output", is_flag=True, help="Emit scriptable JSON.")
@click.option(
    "--pycache-root",
    type=click.Path(path_type=Path),
    help="Include __pycache__/.pyc under this root.",
)
def cmd_caches(json_output: bool, pycache_root: Path | None) -> None:
    rows = _cache_rows(cache_registry(pycache_root))
    if json_output:
        click.echo(
            json.dumps(
                {
                    "caches": rows,
                    "reclaimable_bytes": sum(int(row["bytes"]) for row in rows),
                },
                default=str,
            )
        )
        return
    _print_cache_table(rows, title="Known caches")


@cli.command(
    "reclaim",
    context_settings=CONTEXT,
    help="Reclaim selected caches; dry-run unless confirmed or --yes.",
)
@click.option(
    "--only", "only", multiple=True, help="Cache name to reclaim; may be repeated."
)
@click.option("--yes", is_flag=True, help="Delete without interactive confirmation.")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be removed; delete nothing."
)
@click.option(
    "--pycache-root",
    type=click.Path(path_type=Path),
    help="Include __pycache__/.pyc under this root.",
)
def cmd_reclaim(
    only: tuple[str, ...], yes: bool, dry_run: bool, pycache_root: Path | None
) -> None:
    caches = cache_registry(pycache_root)
    selected = [cache for cache in caches if not only or cache.name in set(only)]
    unknown = sorted(set(only) - {cache.name for cache in caches})
    if unknown:
        raise click.ClickException(f"unknown cache: {', '.join(unknown)}")
    plan = [(cache, cache.size(), _existing(cache.paths())) for cache in selected]
    plan = [
        (cache, size, paths)
        for cache, size, paths in plan
        if size.bytes > 0 or cache.command
    ]
    if not plan:
        ui.hint("Nothing reclaimable selected.")
        return
    rows = [
        {
            "name": cache.name,
            "bytes": size.bytes,
            "size": ui.human_bytes(size.bytes),
            "paths": [str(p) for p in paths],
            "reclaim": " ".join(cache.command)
            if cache.command
            else ("sudo required" if cache.requires_sudo else "remove paths"),
        }
        for cache, size, paths in plan
    ]
    _print_cache_table(rows, title="Reclaim plan")
    total = sum(size.bytes for _, size, _ in plan)
    if dry_run:
        ui.ok(f"dry-run: would reclaim up to {ui.human_bytes(total)}")
        return
    if not yes and not click.confirm(
        f"Remove these caches and reclaim up to {ui.human_bytes(total)}?", default=False
    ):
        ui.warn("aborted; nothing deleted")
        return
    freed = 0
    failures: list[str] = []
    for cache, _, _ in plan:
        if cache.requires_sudo and os.geteuid() != 0:
            failures.append(f"{cache.name}: requires sudo")
            continue
        result = cache.reclaim(True)
        freed += result.freed
        failures.extend(f"{cache.name}: {msg}" for msg in result.failed)
    for failure in failures:
        ui.warn(failure)
    ui.ok(f"freed {ui.human_bytes(freed)}")


if __name__ == "__main__":
    cli()
