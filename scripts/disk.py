#!/usr/bin/env python3
"""Friendlier disk management: inspect, partition, format, and mount.

Examples:
  usm disk                       # tree of disks + partitions (prettier lsblk)
  usm disk info sdb              # everything about one disk/partition
  usm disk usage                # mounted filesystems with usage bars
  usm disk fstab                # parsed /etc/fstab

  usm disk setup sdb            # raw disk -> GPT + ext4 + mounted at /mnt/sdb
  usm disk partition sdb        # one whole-disk partition (GPT) and nothing else
  usm disk format sdb1 -l data  # mkfs.ext4 with label 'data'
  usm disk mount sdb1 /data --fstab   # mount now and persist in /etc/fstab
  usm disk unmount /data --fstab      # umount and drop the fstab entry

Destructive actions (partition/format/wipe/setup) need root and prompt for
confirmation; pass -y to skip. Devices backing / or /boot are never touched,
and a device that already holds data needs --force to overwrite.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import click
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

FSTAB = "/etc/fstab"
FSTAB_MARK = "# usm disk"
PROTECTED_MOUNTS = ("/", "/boot", "/boot/efi", "/boot/firmware", "/usr", "/var")

# fs name -> (mkfs binary, label flag, extra non-interactive args)
FS_SPEC: dict[str, tuple[str, str, list[str]]] = {
    "ext2": ("mkfs.ext2", "-L", ["-F"]),
    "ext3": ("mkfs.ext3", "-L", ["-F"]),
    "ext4": ("mkfs.ext4", "-L", ["-F"]),
    "xfs": ("mkfs.xfs", "-L", ["-f"]),
    "btrfs": ("mkfs.btrfs", "-L", ["-f"]),
    "vfat": ("mkfs.vfat", "-n", []),
    "fat32": ("mkfs.vfat", "-n", ["-F", "32"]),
    "exfat": ("mkfs.exfat", "-n", []),
    "ntfs": ("mkfs.ntfs", "-L", ["-Q", "-F"]),
}
FS_ALIASES = {"ext": "ext4", "fat": "vfat", "msdos": "vfat", "fat16": "vfat"}

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,FSTYPE,FSSIZE,FSUSED,FSAVAIL,FSUSE%,MOUNTPOINT,"
    "LABEL,PARTLABEL,UUID,MODEL,VENDOR,SERIAL,ROTA,TRAN,PTTYPE,RM,RO,HOTPLUG,PKNAME"
)


# Formatting helpers --------------------------------------------------------


def fmt_bytes(n: float | None) -> str:
    """Human-readable size: 0B / 512B / 1.0KiB / 3.6TiB."""
    if not n:
        return "0B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}EiB"


def _int(v: object) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return 0


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(argv: list[str], *, timeout: int | None = 60, text_in: str | None = None):
    """Run a command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, input=text_in
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _needs_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() != 0


def _require_root(action: str) -> None:
    if _needs_root():
        raise click.ClickException(
            f"'{action}' needs root — try: sudo usm disk {action} ..."
        )


def _must_run(argv: list[str], *, timeout: int | None = 60) -> str:
    """Run a command, raising a clean ClickException on failure."""
    rc, out, err = _run(argv, timeout=timeout)
    if rc != 0:
        msg = err.strip() or out.strip() or f"{argv[0]} failed (exit {rc})."
        low = msg.lower()
        if "permission" in low or "not permitted" in low or _needs_root():
            msg += "  (try again under sudo)"
        raise click.ClickException(msg)
    return out


# Model ---------------------------------------------------------------------


@dataclass
class Dev:
    name: str
    path: str
    type: str  # disk | part | loop | rom | lvm | crypt ...
    size: int  # bytes
    fstype: str | None
    fssize: int | None
    fsused: int | None
    fsavail: int | None
    fsusep: str | None  # "12%"
    mountpoint: str | None
    label: str | None
    partlabel: str | None
    uuid: str | None
    model: str | None
    vendor: str | None
    serial: str | None
    rota: bool | None
    tran: str | None
    pttype: str | None
    rm: bool
    ro: bool
    hotplug: bool
    pkname: str | None
    children: list["Dev"] = field(default_factory=list)


def _to_dev(o: dict) -> Dev:
    return Dev(
        name=o.get("name") or "",
        path=o.get("path") or "",
        type=o.get("type") or "",
        size=_int(o.get("size")),
        fstype=o.get("fstype"),
        fssize=_int(o.get("fssize")) or None,
        fsused=_int(o.get("fsused")) or None,
        fsavail=_int(o.get("fsavail")) or None,
        fsusep=o.get("fsuse%"),
        mountpoint=o.get("mountpoint"),
        label=o.get("label"),
        partlabel=o.get("partlabel"),
        uuid=o.get("uuid"),
        model=(o.get("model") or "").strip() or None,
        vendor=(o.get("vendor") or "").strip() or None,
        serial=o.get("serial"),
        rota=o.get("rota"),
        tran=o.get("tran"),
        pttype=o.get("pttype"),
        rm=bool(o.get("rm")),
        ro=bool(o.get("ro")),
        hotplug=bool(o.get("hotplug")),
        pkname=o.get("pkname"),
        children=[_to_dev(c) for c in o.get("children", [])],
    )


def _lsblk(path: str | None = None) -> list[Dev]:
    if not _have("lsblk"):
        raise click.ClickException("'lsblk' is not available on this host.")
    argv = ["lsblk", "--json", "-b", "-o", LSBLK_COLUMNS]
    if path:
        argv.append(path)
    rc, out, err = _run(argv)
    if rc != 0:
        raise click.ClickException(err.strip() or "lsblk failed.")
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"could not parse lsblk output: {exc}") from exc
    return [_to_dev(x) for x in data.get("blockdevices", [])]


def _iter(devs: list[Dev]):
    for d in devs:
        yield d
        yield from _iter(d.children)


def _resolve(token: str) -> Dev:
    """Find a Dev by '/dev/sdb', 'sdb', or 'sdb1' (searches partitions too)."""
    t = token.strip()
    name = t.removeprefix("/dev/")
    cand = t if t.startswith("/dev/") else f"/dev/{name}"
    for d in _iter(_lsblk()):
        if d.path in (t, cand) or d.name == name:
            return d
    raise click.ClickException(f"no such block device: {token}")


def _first_partition(disk_path: str) -> Dev:
    devs = _lsblk(disk_path)
    if devs:
        for c in devs[0].children:
            if c.type == "part":
                return c
    raise click.ClickException(
        f"partition table written but no partition appeared — "
        f"try: sudo partprobe {disk_path}"
    )


# Safety guards -------------------------------------------------------------


def _is_swap(d: Dev) -> bool:
    return d.fstype == "swap" or (d.mountpoint or "").upper() == "[SWAP]"


def _protected_mounts(d: Dev) -> list[str]:
    hits: list[str] = []
    for x in _iter([d]):
        if (x.mountpoint in PROTECTED_MOUNTS) or _is_swap(x):
            hits.append(x.mountpoint or "[SWAP]")
    return hits


def _mounted_targets(d: Dev) -> list[tuple[str, str]]:
    return [(x.name, x.mountpoint) for x in _iter([d]) if x.mountpoint]


def _guard_destructive(dev: Dev, action: str, *, force: bool) -> None:
    """Refuse system disks / mounted devices; gate non-empty disks behind --force."""
    prot = _protected_mounts(dev)
    if prot:
        raise click.ClickException(
            f"refusing to {action} {dev.path}: it backs "
            f"{', '.join(sorted(set(prot)))} (system disk)."
        )
    mounted = _mounted_targets(dev)
    if mounted:
        where = "; ".join(f"{n} → {m}" for n, m in mounted)
        raise click.ClickException(
            f"{dev.path} is in use ({where}). Unmount it first: "
            f"usm disk unmount {dev.path}"
        )
    if not force:
        if dev.children:
            raise click.ClickException(
                f"{dev.path} already has {len(dev.children)} partition(s); "
                f"re-run with --force to overwrite."
            )
        if dev.fstype:
            raise click.ClickException(
                f"{dev.path} already holds a {dev.fstype} filesystem; "
                f"re-run with --force to overwrite."
            )


def _confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    if not click.confirm(prompt, default=False):
        raise click.Abort()


# Filesystem helpers --------------------------------------------------------


def _resolve_fs(fs: str) -> str:
    key = FS_ALIASES.get(fs.lower(), fs.lower())
    if key not in FS_SPEC:
        raise click.ClickException(
            f"unsupported filesystem '{fs}'. Choose from: {', '.join(sorted(FS_SPEC))}."
        )
    binary = FS_SPEC[key][0]
    if not _have(binary):
        avail = sorted(k for k, v in FS_SPEC.items() if _have(v[0]))
        raise click.ClickException(
            f"'{binary}' is not installed. Available here: "
            f"{', '.join(avail) or 'none'}."
        )
    return key


def _mkfs_argv(fs: str, path: str, label: str | None) -> list[str]:
    binary, label_opt, extra = FS_SPEC[fs]
    argv = [binary, *extra]
    if label:
        lab = label.upper()[:11] if binary == "mkfs.vfat" else label
        argv += [label_opt, lab]
    argv.append(path)
    return argv


def _default_mountpoint(dev: Dev) -> str:
    base = dev.label or dev.partlabel or dev.name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    return f"/mnt/{safe}"


# /etc/fstab ----------------------------------------------------------------


def _fstab_spec(dev: Dev) -> str:
    return f"UUID={dev.uuid}" if dev.uuid else dev.path


def _read_fstab() -> list[str]:
    try:
        return open(FSTAB, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return []


def _write_fstab(lines: list[str]) -> None:
    text = "\n".join(lines).rstrip("\n") + "\n"
    tmp = f"{FSTAB}.usm.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, FSTAB)
    except PermissionError as exc:
        raise click.ClickException(
            f"cannot write {FSTAB} ({exc}) — run under sudo."
        ) from exc


def _fstab_without(lines: list[str], *, spec: str, mountpoint: str) -> list[str]:
    out = []
    for ln in lines:
        fields = ln.split()
        if len(fields) >= 2 and (fields[0] == spec or fields[1] == mountpoint):
            continue
        out.append(ln)
    return out


def _add_fstab(dev: Dev, mountpoint: str, options: str | None) -> None:
    spec = _fstab_spec(dev)
    opts = options or "defaults"
    passno = "0" if dev.fstype == "swap" else "2"
    entry = f"{spec}\t{mountpoint}\t{dev.fstype or 'auto'}\t{opts}\t0\t{passno}\t{FSTAB_MARK}"
    lines = _fstab_without(_read_fstab(), spec=spec, mountpoint=mountpoint)
    lines.append(entry)
    _write_fstab(lines)
    console.print(f"[green]✓[/green] persisted in {FSTAB} [dim]({spec})[/dim]")


def _remove_fstab(spec: str, mountpoint: str) -> None:
    before = _read_fstab()
    after = _fstab_without(before, spec=spec, mountpoint=mountpoint)
    if len(after) == len(before):
        console.print(f"[dim]no {FSTAB} entry for {mountpoint}.[/dim]")
        return
    _write_fstab(after)
    console.print(f"[green]✓[/green] removed {FSTAB} entry for {mountpoint}")


# Rendering -----------------------------------------------------------------

_TYPE_STYLE = {
    "disk": "bold",
    "part": "cyan",
    "lvm": "magenta",
    "crypt": "yellow",
    "loop": "dim",
    "rom": "dim",
}


def _use_style(pct: int) -> str:
    if pct >= 90:
        return "red"
    if pct >= 70:
        return "yellow"
    return "green"


def _pct(dev: Dev) -> int | None:
    if dev.fsusep and dev.fsusep.endswith("%"):
        try:
            return int(dev.fsusep[:-1])
        except ValueError:
            return None
    if dev.fssize and dev.fsused is not None:
        return round(100 * dev.fsused / max(dev.fssize, 1))
    return None


def _bar(pct: int, width: int = 12) -> str:
    fill = max(0, min(width, round(pct / 100 * width)))
    style = _use_style(pct)
    return f"[{style}]{'█' * fill}[/{style}][dim]{'░' * (width - fill)}[/dim]"


def _visible(dev: Dev, show_all: bool) -> bool:
    if show_all:
        return True
    if dev.type in ("loop", "rom"):
        return False
    return dev.size > 0


def _add_tree_rows(table: Table, dev: Dev, prefix: str, last: bool, depth: int):
    branch = "" if depth == 0 else prefix + ("└─" if last else "├─")
    style = _TYPE_STYLE.get(dev.type, "")
    name = f"[{style}]{dev.name}[/{style}]" if style else dev.name
    pct = _pct(dev)
    use = f"[{_use_style(pct)}]{pct}%[/{_use_style(pct)}]" if pct is not None else ""
    table.add_row(
        branch + name,
        fmt_bytes(dev.size),
        dev.type,
        dev.fstype or "[dim]—[/dim]",
        dev.label or dev.partlabel or "[dim]—[/dim]",
        dev.mountpoint or "[dim]—[/dim]",
        use or "[dim]—[/dim]",
    )
    kids = dev.children
    child_prefix = prefix + ("  " if last else "│ ")
    for i, c in enumerate(kids):
        _add_tree_rows(table, c, child_prefix, i == len(kids) - 1, depth + 1)


def _tree_table(devs: list[Dev], show_all: bool) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="dim",
        pad_edge=False,
        padding=(0, 2, 0, 0),
        expand=False,
    )
    table.add_column("name", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("type", no_wrap=True, style="dim")
    table.add_column("fstype", no_wrap=True)
    table.add_column("label", no_wrap=True, overflow="ellipsis", max_width=20)
    table.add_column("mount", overflow="fold")
    table.add_column("use", justify="right", no_wrap=True)
    tops = [d for d in devs if _visible(d, show_all)]
    for d in tops:
        _add_tree_rows(table, d, "", True, 0)
    return table


def _render_dashboard(show_all: bool = False) -> None:
    devs = _lsblk()
    table = _tree_table(devs, show_all)
    if not table.rows:
        console.print("[dim]No block devices found.[/dim]")
        return
    console.print(table)
    disks = [d for d in devs if d.type == "disk"]
    total = sum(d.size for d in disks)
    mounted = sum(1 for d in _iter(devs) if d.mountpoint and d.type != "loop")
    console.print(
        f"\n[dim]{len(disks)} disk(s) · {fmt_bytes(total)} total · "
        f"{mounted} mounted filesystem(s)[/dim]"
    )


# CLI -----------------------------------------------------------------------

COMMAND_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Inspect", ("ls", "info", "usage", "fstab")),
    ("Manage", ("partition", "format", "wipe")),
    ("Mount", ("mount", "unmount", "setup")),
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
        console.print("[dim]Run [bold]usm disk -h[/bold] for all commands.[/dim]")


# Inspect -------------------------------------------------------------------


@cli.command("ls")
@click.option("--all", "-a", "show_all", is_flag=True, help="Include loop/rom devices.")
def cmd_ls(show_all: bool) -> None:
    """Tree of disks and partitions (the default view)."""
    _render_dashboard(show_all)


@cli.command("info")
@click.argument("device")
def cmd_info(device: str) -> None:
    """Detailed report for one disk or partition."""
    dev = _resolve(device)
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(overflow="fold")

    def row(k: str, v: str | None):
        if v:
            table.add_row(k, v)

    row("path", dev.path)
    row("type", dev.type)
    table.add_row("size", f"{fmt_bytes(dev.size)} [dim]({dev.size:,} bytes)[/dim]")
    if dev.type == "disk":
        row("model", dev.model)
        row("vendor", dev.vendor)
        row("serial", dev.serial)
        row("transport", dev.tran)
        if dev.rota is not None:
            table.add_row("rotational", "HDD (spinning)" if dev.rota else "SSD / flash")
        table.add_row("removable", "yes" if dev.rm else "no")
        row(
            "part table", {"gpt": "GPT", "dos": "MBR (dos)"}.get(dev.pttype, dev.pttype)
        )
    row("fstype", dev.fstype)
    row("label", dev.label)
    row("part label", dev.partlabel)
    row("uuid", dev.uuid)
    table.add_row("read-only", "yes" if dev.ro else "no")
    row("mountpoint", dev.mountpoint or "[dim]not mounted[/dim]")
    pct = _pct(dev)
    if dev.fssize:
        used = fmt_bytes(dev.fsused)
        table.add_row(
            "usage",
            f"{used} / {fmt_bytes(dev.fssize)} ({pct}%)  {_bar(pct)}"
            if pct is not None
            else used,
        )
    console.print(table)
    if dev.children:
        console.print(f"\n[dim]{len(dev.children)} partition(s):[/dim]")
        console.print(_tree_table([dev], show_all=True))


@cli.command("usage")
def cmd_usage() -> None:
    """Mounted filesystems with usage bars (real block devices only)."""
    rows = [
        d
        for d in _iter(_lsblk())
        if d.mountpoint and d.fssize and d.type not in ("loop", "rom")
    ]
    if not rows:
        console.print("[dim]No mounted block filesystems.[/dim]")
        return
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("device", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("used", justify="right", no_wrap=True)
    table.add_column("avail", justify="right", no_wrap=True)
    table.add_column("use%", no_wrap=True)
    table.add_column("mount", overflow="fold")
    rows.sort(key=lambda d: d.mountpoint or "")
    for d in rows:
        pct = _pct(d) or 0
        table.add_row(
            d.name,
            fmt_bytes(d.fssize),
            fmt_bytes(d.fsused),
            fmt_bytes(d.fsavail),
            f"{_bar(pct)} [{_use_style(pct)}]{pct:>3}%[/{_use_style(pct)}]",
            d.mountpoint,
        )
    console.print(table)


@cli.command("fstab")
def cmd_fstab() -> None:
    """Show /etc/fstab entries in a table."""
    lines = _read_fstab()
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("spec", "mount", "type", "options", "dump", "pass"):
        table.add_column(col, overflow="fold", no_wrap=col in ("dump", "pass"))
    seen = False
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        f = s.split()
        if len(f) < 2:
            continue
        f += ["", "", "", "", ""]
        table.add_row(f[0], f[1], f[2] or "auto", f[3] or "defaults", f[4], f[5])
        seen = True
    if not seen:
        console.print(f"[dim]No entries in {FSTAB}.[/dim]")
        return
    console.print(table)


# Manage --------------------------------------------------------------------


@cli.command("partition")
@click.argument("disk")
@click.option(
    "--table",
    "-t",
    "table_kind",
    type=click.Choice(["gpt", "mbr"]),
    default="gpt",
    show_default=True,
    help="Partition table type.",
)
@click.option("--name", "-n", help="Partition name (GPT) / label hint.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", "-f", is_flag=True, help="Overwrite a disk that holds data.")
def cmd_partition(
    disk: str, table_kind: str, name: str | None, yes: bool, force: bool
) -> None:
    """Create one partition spanning a whole DISK (wipes it)."""
    if not _have("parted"):
        raise click.ClickException(
            "'parted' is required — install the 'parted' package."
        )
    dev = _resolve(disk)
    if dev.type != "disk":
        raise click.ClickException(
            f"'partition' expects a whole disk, not a {dev.type}."
        )
    _guard_destructive(dev, "partition", force=force)
    _require_root("partition")
    _confirm(
        f"Wipe {dev.path} ({fmt_bytes(dev.size)}) and create a "
        f"{table_kind.upper()} table with one full-disk partition?",
        yes,
    )
    _make_single_partition(dev, table_kind, name)
    part = _first_partition(dev.path)
    console.print(
        f"[green]✓[/green] created [bold]{part.path}[/bold] "
        f"({fmt_bytes(part.size)}) on a {table_kind.upper()} table"
    )
    console.print(f"[dim]next: usm disk format {part.name}[/dim]")


def _make_single_partition(dev: Dev, table_kind: str, name: str | None) -> None:
    label_kind = "gpt" if table_kind == "gpt" else "msdos"
    # MBR/msdos needs a part-type (primary/logical); only GPT accepts a name.
    part_spec = (name or "primary") if table_kind == "gpt" else "primary"
    if _have("wipefs"):
        _must_run(["wipefs", "-a", dev.path])
    _must_run(["parted", "-s", "-a", "optimal", dev.path, "mklabel", label_kind])
    _must_run(
        ["parted", "-s", "-a", "optimal", dev.path, "mkpart", part_spec, "0%", "100%"]
    )
    if _have("partprobe"):
        _run(["partprobe", dev.path])
    if _have("udevadm"):
        _run(["udevadm", "settle"], timeout=10)


@cli.command("format")
@click.argument("device")
@click.option("--fs", default="ext4", show_default=True, help="Filesystem type.")
@click.option("--label", "-l", help="Filesystem label.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", "-f", is_flag=True, help="Overwrite an existing filesystem.")
def cmd_format(device: str, fs: str, label: str | None, yes: bool, force: bool) -> None:
    """Create a filesystem on DEVICE (mkfs)."""
    dev = _resolve(device)
    if dev.type not in ("part", "disk", "lvm", "crypt"):
        raise click.ClickException(f"refusing to format a {dev.type}.")
    fs = _resolve_fs(fs)
    _guard_destructive(dev, "format", force=force)
    _require_root("format")
    current = f"currently {dev.fstype}" if dev.fstype else "no filesystem yet"
    _confirm(
        f"Create a {fs} filesystem on {dev.path} "
        f"({fmt_bytes(dev.size)}, {current})? This erases all data on it.",
        yes,
    )
    with console.status(f"running mkfs.{fs} on {dev.path}…"):
        _must_run(_mkfs_argv(fs, dev.path, label), timeout=None)
    suffix = f" [dim](label {label})[/dim]" if label else ""
    console.print(f"[green]✓[/green] {dev.path} is now [bold]{fs}[/bold]{suffix}")
    console.print(f"[dim]next: usm disk mount {dev.name}[/dim]")


@cli.command("wipe")
@click.argument("device")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", "-f", is_flag=True, help="Wipe a device that holds data.")
def cmd_wipe(device: str, yes: bool, force: bool) -> None:
    """Erase all filesystem/partition signatures on DEVICE (wipefs)."""
    if not _have("wipefs"):
        raise click.ClickException("'wipefs' is not available on this host.")
    dev = _resolve(device)
    _guard_destructive(dev, "wipe", force=force)
    _require_root("wipe")
    _confirm(
        f"Erase all signatures on {dev.path} ({fmt_bytes(dev.size)})?",
        yes,
    )
    _must_run(["wipefs", "-a", dev.path])
    console.print(f"[green]✓[/green] wiped signatures on [bold]{dev.path}[/bold]")


# Mount ---------------------------------------------------------------------


@cli.command("mount")
@click.argument("device")
@click.argument("mountpoint", required=False)
@click.option("--options", "-o", help="Mount options (comma-separated).")
@click.option(
    "--fstab", is_flag=True, help="Persist the mount in /etc/fstab (by UUID)."
)
@click.option(
    "--mkdir/--no-mkdir",
    default=True,
    show_default=True,
    help="Create the mountpoint directory if missing.",
)
def cmd_mount(
    device: str, mountpoint: str | None, options: str | None, fstab: bool, mkdir: bool
) -> None:
    """Mount DEVICE at MOUNTPOINT (default /mnt/<label-or-name>)."""
    dev = _resolve(device)
    if not dev.fstype:
        hint = (
            "format it first (usm disk format …)"
            if not dev.children
            else "mount one of its partitions instead"
        )
        raise click.ClickException(f"{dev.path} has no filesystem — {hint}.")
    if dev.mountpoint:
        raise click.ClickException(
            f"{dev.path} is already mounted at {dev.mountpoint}."
        )
    mp = mountpoint or _default_mountpoint(dev)
    _require_root("mount")
    if mkdir and not os.path.isdir(mp):
        try:
            os.makedirs(mp, exist_ok=True)
        except OSError as exc:
            raise click.ClickException(f"cannot create {mp}: {exc}") from exc
    argv = ["mount"]
    if options:
        argv += ["-o", options]
    argv += [dev.path, mp]
    _must_run(argv)
    console.print(
        f"[green]✓[/green] mounted [bold]{dev.path}[/bold] at [bold]{mp}[/bold]"
    )
    if fstab:
        _add_fstab(_resolve(dev.path), mp, options)


@cli.command("unmount")
@click.argument("target")
@click.option("--lazy", "-l", is_flag=True, help="Lazy unmount (detach when free).")
@click.option("--force", "-f", is_flag=True, help="Force unmount.")
@click.option("--fstab", is_flag=True, help="Also remove its /etc/fstab entry.")
def cmd_unmount(target: str, lazy: bool, force: bool, fstab: bool) -> None:
    """Unmount a DEVICE or a MOUNTPOINT."""
    name = target.removeprefix("/dev/")
    cand = target if target.startswith("/dev/") else f"/dev/{name}"
    all_devs = list(_iter(_lsblk()))
    by_dev = next(
        (d for d in all_devs if d.path in (target, cand) or d.name == name), None
    )
    by_mp = next((d for d in all_devs if d.mountpoint == target), None)
    if by_dev is not None:
        if not by_dev.mountpoint:
            raise click.ClickException(f"{by_dev.path} is not mounted.")
        mp, dev = by_dev.mountpoint, by_dev
    elif by_mp is not None:
        mp, dev = target, by_mp
    else:
        mp, dev = target, None
    _require_root("unmount")
    argv = ["umount"]
    if lazy:
        argv.append("-l")
    if force:
        argv.append("-f")
    argv.append(mp)
    _must_run(argv)
    console.print(f"[green]✓[/green] unmounted [bold]{mp}[/bold]")
    if fstab:
        spec = _fstab_spec(dev) if dev else mp
        _remove_fstab(spec, mp)


cli.add_command(cmd_unmount, "umount")


@cli.command("setup")
@click.argument("disk")
@click.option("--fs", default="ext4", show_default=True, help="Filesystem type.")
@click.option("--mountpoint", "-m", help="Where to mount (default /mnt/<disk>).")
@click.option("--label", "-l", help="Filesystem label.")
@click.option(
    "--table",
    "-t",
    "table_kind",
    type=click.Choice(["gpt", "mbr"]),
    default="gpt",
    show_default=True,
    help="Partition table type.",
)
@click.option("--fstab", is_flag=True, help="Persist the mount in /etc/fstab.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", "-f", is_flag=True, help="Overwrite a disk that holds data.")
def cmd_setup(
    disk: str,
    fs: str,
    mountpoint: str | None,
    label: str | None,
    table_kind: str,
    fstab: bool,
    yes: bool,
    force: bool,
) -> None:
    """Take a raw DISK all the way to a mounted filesystem in one step."""
    if not _have("parted"):
        raise click.ClickException(
            "'parted' is required — install the 'parted' package."
        )
    dev = _resolve(disk)
    if dev.type != "disk":
        raise click.ClickException(f"'setup' expects a whole disk, not a {dev.type}.")
    fs = _resolve_fs(fs)
    _guard_destructive(dev, "setup", force=force)
    _require_root("setup")
    mp = mountpoint or f"/mnt/{dev.name}"
    _confirm(
        f"Set up {dev.path} ({fmt_bytes(dev.size)}): "
        f"{table_kind.upper()} partition -> {fs} -> mount at {mp}"
        f"{' (+fstab)' if fstab else ''}? This erases the disk.",
        yes,
    )
    _make_single_partition(dev, table_kind, label)
    part = _first_partition(dev.path)
    console.print(f"[green]✓[/green] partition [bold]{part.path}[/bold]")
    with console.status(f"running mkfs.{fs} on {part.path}…"):
        _must_run(_mkfs_argv(fs, part.path, label), timeout=None)
    console.print(f"[green]✓[/green] formatted [bold]{fs}[/bold]")
    if not os.path.isdir(mp):
        os.makedirs(mp, exist_ok=True)
    _must_run(["mount", part.path, mp])
    console.print(f"[green]✓[/green] mounted at [bold]{mp}[/bold]")
    if fstab:
        _add_fstab(_resolve(part.path), mp, None)
    console.print(
        f"\n[bold green]Ready.[/bold green] [bold]{part.path}[/bold] "
        f"is {fs} and mounted at [bold]{mp}[/bold]."
    )


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
